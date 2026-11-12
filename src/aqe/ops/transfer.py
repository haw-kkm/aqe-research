"""Verified, resumable, never-overwriting transfer of data/artifact roots (docs/CLOUD_DEPLOYMENT.md §데이터 이전).

Directions: Windows -> Oracle, Windows -> Google, Oracle -> Google (pull, when the Google VM is up), Google -> Oracle
(model releases use aqe.ops.release instead). Exactly one side may be remote (`ssh://user@host/abs/root`).

Protocol (same for local and remote destinations):
  1. manifest  source files of class `transfer` (aqe.ops.inventory) with size + SHA-256; SQLite DBs as online-backup
               snapshots; live/host-local/temp files are excluded and listed.
  2. plan      compare with the destination by relative path: absent -> COPY, same SHA-256 -> SKIP (duplicate),
               different SHA-256 -> CONFLICT (never overwritten: the incoming copy is kept under
               `_transfer/<id>/conflicts/<rel>` for review — e.g. the same hourly partition written by two collectors).
  3. copy      into `<dest>/_transfer/<id>/incoming/<rel>` (atomic temp name), re-hash, retry once on mismatch.
               Already-verified incoming files are not copied again (resume after interruption).
  4. finalize  move verified files into place only if the target is still absent (a race with a writer becomes a
               conflict); journal every action; receipt with bytes/files/conflicts/failures + code commit.
DRY-RUN unless `execute=True`. The source is only read. Nothing is deleted: rollback moves this transfer's placed
files into `_transfer/<id>/rolled_back/` after checking they are unchanged.
Modes: `safe` (default) never replaces; `mirror` (downstream replica) updates a file only if the destination still holds
the version it last received from the same source (`_transfer/received_index.json`), keeping the old version.
Conflicting collector partitions can be unioned by key with `aqe.ops.merge` (explicit, verified, backed up).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from aqe.ops.inventory import scan, sqlite_backup
from aqe.storage import sha256_file

TRANSFER_DIR = "_transfer"
Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]


def default_runner(argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=3600)


@dataclass
class Endpoint:
    root: str  # absolute path on that side
    host: str | None = None  # user@host for ssh; None = local
    python: str = "python3"  # remote interpreter with aqe importable (e.g. /opt/aqe/current/.venv/bin/python)

    @property
    def remote(self) -> bool:
        return self.host is not None

    @classmethod
    def parse(cls, text: str, python: str | None = None) -> "Endpoint":
        if text.startswith("ssh://"):
            rest = text[len("ssh://"):]
            host, _, path = rest.partition("/")
            if not host or not path:
                raise ValueError(f"ssh endpoint must be ssh://user@host/absolute/path, got {text}")
            return cls(root="/" + path, host=host, python=python or "python3")
        return cls(root=str(Path(text)), python=python or "python3")

    def describe(self) -> str:
        return f"ssh://{self.host}{self.root}" if self.remote else self.root


@dataclass
class Plan:
    transfer_id: str
    kind: str
    source: str
    dest: str
    created_at: str
    entries: list[dict[str, Any]] = field(default_factory=list)  # rel, bytes, sha256, action, class
    excluded: list[dict[str, Any]] = field(default_factory=list)
    snapshots: list[dict[str, Any]] = field(default_factory=list)

    def totals(self) -> dict[str, Any]:
        t: dict[str, Any] = {}
        for e in self.entries:
            a = t.setdefault(e["action"], {"files": 0, "bytes": 0})
            a["files"] += 1
            a["bytes"] += e["bytes"]
        return t

    def to_json(self) -> dict[str, Any]:
        return {"transfer_id": self.transfer_id, "kind": self.kind, "source": self.source, "dest": self.dest,
                "created_at": self.created_at, "totals": self.totals(), "entries": self.entries, "excluded": self.excluded,
                "snapshots": self.snapshots}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Plan":
        return cls(**{k: d[k] for k in ("transfer_id", "kind", "source", "dest", "created_at", "entries", "excluded", "snapshots")})


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_transfer_id() -> str:
    return datetime.now(timezone.utc).strftime("tx-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]


def code_commit() -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(Path(__file__).resolve().parents[3]), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------- manifest / index (run on the side that holds the files)
def build_manifest(root: Path, kind: str, *, settle_seconds: int = 900, snapshot_dir: Path | None = None) -> dict[str, Any]:
    """Transferable files of `root` with SHA-256. SQLite DBs are snapshotted (online backup) into `snapshot_dir` and
    listed with rel `_snapshots/<rel>`; without snapshot_dir they are excluded. Read-only for `root`."""
    inv = scan(root, kind, hash_files=True, settle_seconds=settle_seconds)
    entries, excluded, snaps = [], [], []
    for f in inv["files"]:
        if f["class"] == "transfer" and f.get("parquet_ok") is False:
            excluded.append({"rel": f["rel"], "class": "corrupt", "reason": f"unreadable Parquet: {f.get('parquet_error')}", "bytes": f["bytes"]})
        elif f["class"] == "transfer":
            entries.append({"rel": f["rel"], "bytes": f["bytes"], "sha256": f["sha256"], "class": f["class"],
                            **({"schema_hash": f["schema_hash"]} if f.get("schema_hash") else {}),
                            **({"partition_problem": f["partition_problem"]} if f.get("partition_problem") else {})})
        elif f["class"] == "snapshot" and snapshot_dir is not None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            rel = f"_snapshots/{PurePosixPath(f['rel']).with_suffix('')}.{stamp}.sqlite"
            out = Path(snapshot_dir) / rel
            info = sqlite_backup(Path(root) / f["rel"], out)
            if info["integrity"] != "ok":
                raise RuntimeError(f"snapshot of {f['rel']} failed integrity_check: {info['integrity']}")
            entries.append({"rel": rel, "bytes": info["bytes"], "sha256": info["sha256"], "class": "snapshot",
                            "snapshot_of": f["rel"], "local_path": str(out)})
            snaps.append({"of": f["rel"], "rel": rel, "integrity": info["integrity"], "tables": info["tables"]})
        else:
            excluded.append({"rel": f["rel"], "class": f["class"], "reason": f["reason"], "bytes": f["bytes"]})
    return {"root": str(root), "kind": kind, "built_at": _utcnow(), "entries": entries, "excluded": excluded, "snapshots": snaps}


def dest_index(root: Path, rels: list[str]) -> dict[str, str | None]:
    """SHA-256 of `rels` that already exist under `root` (None = absent). Only the listed paths are hashed."""
    out: dict[str, str | None] = {}
    for rel in rels:
        p = Path(root) / rel
        out[rel] = sha256_file(p) if p.is_file() else None
    return out


MODES = ("safe", "mirror")


def received_index(root: Path) -> dict[str, dict[str, Any]]:
    """rel -> {sha256, source, transfer_id} of the last version this root RECEIVED (written by finalize)."""
    p = Path(root) / TRANSFER_DIR / "received_index.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def make_plan(manifest: dict[str, Any], index: dict[str, str | None], *, transfer_id: str, source: str, dest: str,
              mode: str = "safe", received: dict[str, dict[str, Any]] | None = None) -> Plan:
    """safe   (default; e.g. Windows -> Oracle): an existing file with other content is a CONFLICT, never replaced.
    mirror (a downstream replica, e.g. Oracle -> Google): a file that grew at the source (monthly/hourly partitions)
           is UPDATED only if the destination still holds exactly the version it last received from THIS source;
           a locally changed or foreign file stays a CONFLICT. The replaced version is kept (`_transfer/<id>/replaced/`)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    received = received or {}
    plan = Plan(transfer_id=transfer_id, kind=manifest["kind"], source=source, dest=dest, created_at=_utcnow(),
                excluded=manifest["excluded"], snapshots=manifest["snapshots"])
    for e in manifest["entries"]:
        have = index.get(e["rel"])
        last = received.get(e["rel"]) or {}
        if e["class"] == "snapshot":
            action = "COPY_SNAPSHOT"  # snapshots never go into the live DB path; placed under _transfer/<id>/sqlite/
        elif have is None:
            action = "COPY"
        elif have == e["sha256"]:
            action = "SKIP_SAME"
        elif mode == "mirror" and last.get("sha256") == have and last.get("source") == source:
            action = "UPDATE"
        else:
            action = "CONFLICT"
        plan.entries.append({**{k: v for k, v in e.items() if k != "local_path"}, "action": action,
                             **({"dest_sha256": have} if action in ("CONFLICT", "UPDATE") else {})})
    return plan


# ---------------------------------------------------------------- destination-side finalize (local filesystem)
def _tx_dir(root: Path, transfer_id: str) -> Path:
    return Path(root) / TRANSFER_DIR / transfer_id


def _journal(root: Path, transfer_id: str) -> dict[str, dict[str, Any]]:
    p = _tx_dir(root, transfer_id) / "journal.jsonl"
    done: dict[str, dict[str, Any]] = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                done[r["rel"]] = r
    return done


def _append_journal(root: Path, transfer_id: str, row: dict[str, Any]) -> None:
    p = _tx_dir(root, transfer_id) / "journal.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({**row, "at": _utcnow()}, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def incoming_path(root: Path, transfer_id: str, rel: str) -> Path:
    return _tx_dir(root, transfer_id) / "incoming" / rel


def _place_no_overwrite(src: Path, dst: Path) -> bool:
    """Move src to dst only if dst does not exist. Returns False (src kept) when dst appeared meanwhile."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)  # atomic create-if-absent on the same filesystem
    except FileExistsError:
        return False
    except OSError:
        if dst.exists():
            return False
        os.rename(src, dst)  # filesystems without hard links; Windows rename refuses an existing target
        return True
    src.unlink()
    return True


def finalize(root: Path, plan: Plan) -> dict[str, Any]:
    """Verify incoming files and place them (destination side). Idempotent: journaled entries are skipped."""
    root = Path(root)
    done = _journal(root, plan.transfer_id)
    received_updates: dict[str, str] = {}
    stats: dict[str, Any] = {"placed": 0, "updated": 0, "skipped_same": 0, "conflicts": [], "failed": [], "already_done": 0,
                             "bytes_placed": 0}
    for e in plan.entries:
        rel, sha = e["rel"], e["sha256"]
        if rel in done and done[rel]["status"] in ("placed", "updated", "skip_same", "conflict_kept", "snapshot_kept"):
            stats["already_done"] += 1
            continue
        if e["action"] == "SKIP_SAME":
            _append_journal(root, plan.transfer_id, {"rel": rel, "status": "skip_same", "sha256": sha})
            received_updates[rel] = sha
            stats["skipped_same"] += 1
            continue
        inc = incoming_path(root, plan.transfer_id, rel)
        if not inc.is_file():
            stats["failed"].append({"rel": rel, "reason": "incoming file missing (copy incomplete)"})
            continue
        got = sha256_file(inc)
        if got != sha:
            stats["failed"].append({"rel": rel, "reason": f"SHA-256 mismatch: expected {sha[:12]}, got {got[:12]}"})
            _append_journal(root, plan.transfer_id, {"rel": rel, "status": "sha_mismatch", "sha256": got})
            continue
        if rel.endswith(".parquet") and e.get("schema_hash"):
            from aqe.ops.inventory import validate_parquet

            v = validate_parquet(inc)
            if not v["ok"] or v.get("schema_hash") != e["schema_hash"]:
                stats["failed"].append({"rel": rel, "reason": f"Parquet check failed: {v.get('error') or 'schema differs'}"})
                continue
        if e["action"] == "COPY_SNAPSHOT":
            target = _tx_dir(root, plan.transfer_id) / "sqlite" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(inc, target)  # inside this transfer's own directory only
            _append_journal(root, plan.transfer_id, {"rel": rel, "status": "snapshot_kept", "sha256": sha, "path": str(target)})
            continue
        dst = root / rel
        if e["action"] == "COPY" and _place_no_overwrite(inc, dst):
            _append_journal(root, plan.transfer_id, {"rel": rel, "status": "placed", "sha256": sha})
            received_updates[rel] = sha
            stats["placed"] += 1
            stats["bytes_placed"] += e["bytes"]
            continue
        if e["action"] == "UPDATE" and dst.is_file() and sha256_file(dst) == e.get("dest_sha256"):
            # mirror mode: the destination still holds the version it last received from this source -> keep that
            # version under replaced/, then swap the new one in atomically
            old = _tx_dir(root, plan.transfer_id) / "replaced" / rel
            old.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, old)
            os.replace(inc, dst)
            _append_journal(root, plan.transfer_id, {"rel": rel, "status": "updated", "sha256": sha, "replaced_sha256": e["dest_sha256"],
                                                     "replaced_kept_at": str(old)})
            received_updates[rel] = sha
            stats["updated"] += 1
            stats["bytes_placed"] += e["bytes"]
            continue
        if dst.is_file() and sha256_file(dst) == sha:  # same content arrived meanwhile: duplicate, nothing to keep
            _append_journal(root, plan.transfer_id, {"rel": rel, "status": "skip_same", "sha256": sha})
            inc.unlink()
            stats["skipped_same"] += 1
            continue
        keep = _tx_dir(root, plan.transfer_id) / "conflicts" / rel
        keep.parent.mkdir(parents=True, exist_ok=True)
        os.replace(inc, keep)
        _append_journal(root, plan.transfer_id, {"rel": rel, "status": "conflict_kept", "sha256": sha, "path": str(keep)})
        stats["conflicts"].append({"rel": rel, "incoming_sha256": sha, "kept_at": str(keep)})
    receipt = {"transfer_id": plan.transfer_id, "kind": plan.kind, "source": plan.source, "dest": plan.dest,
               "finalized_at": _utcnow(), "plan_totals": plan.totals(), **stats,
               "complete": not stats["failed"], "code_commit": code_commit()}
    tx = _tx_dir(root, plan.transfer_id)
    tx.mkdir(parents=True, exist_ok=True)
    (tx / "receipt.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
    if received_updates:  # which version of each file this root last received, and from where (mirror mode needs it)
        idx = received_index(root)
        for rel, sha in received_updates.items():
            idx[rel] = {"sha256": sha, "source": plan.source, "transfer_id": plan.transfer_id}
        tmp = Path(root) / TRANSFER_DIR / "received_index.json.tmp"
        tmp.write_text(json.dumps(idx, indent=0, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, Path(root) / TRANSFER_DIR / "received_index.json")
    with open(Path(root) / TRANSFER_DIR / "transfers.jsonl", "a", encoding="utf-8", newline="\n") as fh:  # transfer volume log
        fh.write(json.dumps({k: receipt[k] for k in ("transfer_id", "kind", "source", "dest", "finalized_at", "placed", "updated",
                                                       "bytes_placed", "skipped_same", "complete")}) + "\n")
    return receipt


def rollback(root: Path, transfer_id: str, execute: bool = False) -> dict[str, Any]:
    """Move files PLACED by this transfer to `_transfer/<id>/rolled_back/` (only if unchanged since placement)."""
    root = Path(root)
    moves, changed, restore = [], [], {}
    for rel, r in _journal(root, transfer_id).items():
        if r["status"] not in ("placed", "updated"):
            continue
        p = root / rel
        if not p.is_file():
            continue
        if sha256_file(p) != r["sha256"]:
            changed.append(rel)
            continue
        moves.append(rel)
        if r["status"] == "updated":
            restore[rel] = r["replaced_kept_at"]  # mirror update: the previous version comes back
    if execute:
        for rel in moves:
            dst = _tx_dir(root, transfer_id) / "rolled_back" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(root / rel, dst)
            if rel in restore:
                shutil.copy2(restore[rel], root / rel)
            _append_journal(root, transfer_id, {"rel": rel, "status": "rolled_back", "restored_previous": rel in restore})
    return {"transfer_id": transfer_id, "execute": execute, "would_move" if not execute else "moved": moves,
            "changed_since_placement_not_moved": changed}


# ---------------------------------------------------------------- orchestration
def _ssh(ep: Endpoint, remote_argv: list[str], runner: Runner, stdin: str | None = None) -> subprocess.CompletedProcess:
    cmd = " ".join(shlex.quote(a) for a in remote_argv)
    r = runner(["ssh", "-o", "BatchMode=yes", ep.host, cmd], stdin)
    if r.returncode != 0:
        raise RuntimeError(f"ssh {ep.host} failed ({r.returncode}): {(r.stderr or '')[-400:]}")
    return r


def _scp(src: str, dst: str, runner: Runner) -> None:
    r = runner(["scp", "-B", "-p", "-q", src, dst], None)
    if r.returncode != 0:
        raise RuntimeError(f"scp {src} -> {dst} failed ({r.returncode}): {(r.stderr or '')[-300:]}")


def run_transfer(source: Endpoint, dest: Endpoint, kind: str, *, execute: bool = False, transfer_id: str | None = None,
                 settle_seconds: int = 900, runner: Runner = default_runner, max_attempts: int = 2,
                 work_dir: Path | None = None, mode: str = "safe") -> dict[str, Any]:
    """Plan (and with execute=True perform) one transfer. Returns the plan summary or the receipt."""
    if source.remote and dest.remote:
        raise ValueError("remote-to-remote is not supported: run the transfer on one of the two hosts")
    tid = transfer_id or new_transfer_id()
    work = Path(work_dir or tempfile.mkdtemp(prefix=f"aqe-{tid}-"))
    work.mkdir(parents=True, exist_ok=True)
    snap_dir = work / "snapshots"
    # 1. manifest on the source side (remote: python -m aqe.ops transfer-manifest; prints JSON)
    if source.remote:
        r = _ssh(source, [source.python, "-m", "aqe.ops", "transfer-manifest", "--root", source.root, "--kind", kind,
                          "--settle-seconds", str(settle_seconds), "--snapshot-dir", f"{source.root}/{TRANSFER_DIR}/{tid}/source_snapshots"], runner)
        manifest = json.loads(r.stdout)
    else:
        manifest = build_manifest(Path(source.root), kind, settle_seconds=settle_seconds, snapshot_dir=snap_dir)
    local_paths = {e["rel"]: e.get("local_path") for e in manifest["entries"]}
    # 2. destination index
    rels = [e["rel"] for e in manifest["entries"] if e["class"] != "snapshot"]
    if dest.remote:
        r = _ssh(dest, [dest.python, "-m", "aqe.ops", "transfer-index", "--root", dest.root], runner, stdin=json.dumps(rels))
        index = json.loads(r.stdout)
        received = json.loads(_ssh(dest, [dest.python, "-m", "aqe.ops", "transfer-received", "--root", dest.root], runner).stdout) \
            if mode == "mirror" else {}
    else:
        index = dest_index(Path(dest.root), rels)
        received = received_index(Path(dest.root)) if mode == "mirror" else {}
    plan = make_plan(manifest, index, transfer_id=tid, source=source.describe(), dest=dest.describe(), mode=mode, received=received)
    summary = {"transfer_id": tid, "execute": execute, "mode": mode, "source": plan.source, "dest": plan.dest, "kind": kind,
               "updates": [e["rel"] for e in plan.entries if e["action"] == "UPDATE"],
               "partition_problems": [e["partition_problem"] for e in plan.entries if e.get("partition_problem")],
               "totals": plan.totals(), "excluded": len(plan.excluded),
               "excluded_by_class": {c: sum(1 for x in plan.excluded if x["class"] == c) for c in {x["class"] for x in plan.excluded}},
               "conflicts": [e["rel"] for e in plan.entries if e["action"] == "CONFLICT"], "snapshots": plan.snapshots}
    plan_file = work / "plan.json"
    plan_file.write_text(json.dumps(plan.to_json(), indent=1, ensure_ascii=False), encoding="utf-8")
    summary["plan_file"] = str(plan_file)
    if not execute:
        summary["note"] = "DRY-RUN: nothing copied. Re-run with --execute to transfer."
        return summary
    # 3. copy into <dest>/_transfer/<id>/incoming (verify each file; resume skips verified files)
    to_copy = [e for e in plan.entries if e["action"] in ("COPY", "UPDATE", "CONFLICT", "COPY_SNAPSHOT")]
    failures = []
    for e in to_copy:
        rel = e["rel"]
        src_path = local_paths.get(rel) or (str(Path(source.root) / rel) if not source.remote else f"{source.root}/{rel}")
        for attempt in range(1, max_attempts + 1):
            try:
                _copy_one(source, dest, tid, rel, src_path, e["sha256"], runner)
                break
            except (OSError, RuntimeError, ValueError) as exc:
                if attempt == max_attempts:
                    failures.append({"rel": rel, "reason": f"{type(exc).__name__}: {exc}"[:300]})
    # 4. finalize on the destination side
    if dest.remote:
        _ssh(dest, ["mkdir", "-p", f"{dest.root}/{TRANSFER_DIR}/{tid}"], runner)
        _scp(str(plan_file), f"{dest.host}:{dest.root}/{TRANSFER_DIR}/{tid}/plan.json", runner)
        r = _ssh(dest, [dest.python, "-m", "aqe.ops", "transfer-finalize", "--root", dest.root, "--transfer-id", tid], runner)
        receipt = json.loads(r.stdout)
    else:
        tx = _tx_dir(Path(dest.root), tid)
        tx.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(plan_file, tx / "plan.json")
        receipt = finalize(Path(dest.root), plan)
    receipt["copy_failures"] = failures
    receipt["complete"] = receipt.get("complete", False) and not failures
    return receipt


def _copy_one(source: Endpoint, dest: Endpoint, tid: str, rel: str, src_path: str, sha: str, runner: Runner) -> None:
    if dest.remote:
        remote_inc = f"{dest.root}/{TRANSFER_DIR}/{tid}/incoming/{rel}"
        r = _ssh(dest, [dest.python, "-m", "aqe.ops", "transfer-index", "--root", f"{dest.root}/{TRANSFER_DIR}/{tid}/incoming"],
                 runner, stdin=json.dumps([rel]))
        if json.loads(r.stdout).get(rel) == sha:
            return  # already uploaded and verified (resume)
        _ssh(dest, ["mkdir", "-p", str(PurePosixPath(remote_inc).parent)], runner)
        _scp(src_path, f"{dest.host}:{remote_inc}.part", runner)
        _ssh(dest, ["mv", "-f", f"{remote_inc}.part", remote_inc], runner)  # own incoming dir only
        return  # verified by transfer-finalize on the destination
    inc = incoming_path(Path(dest.root), tid, rel)
    if inc.is_file() and sha256_file(inc) == sha:
        return  # resume: verified earlier
    inc.parent.mkdir(parents=True, exist_ok=True)
    part = inc.with_name(inc.name + ".part")
    if source.remote:
        _scp(f"{source.host}:{src_path}", str(part), runner)
    else:
        shutil.copy2(src_path, part)  # keeps mtime (like scp -p): placed files keep their original age
    got = sha256_file(part)
    if got != sha:
        part.unlink(missing_ok=True)
        raise ValueError(f"SHA-256 mismatch after copy of {rel}: expected {sha[:12]}, got {got[:12]}")
    os.replace(part, inc)


def sleep_backoff(attempt: int) -> None:  # pragma: no cover - only used by long-running callers
    time.sleep(min(60, 2 ** attempt))
