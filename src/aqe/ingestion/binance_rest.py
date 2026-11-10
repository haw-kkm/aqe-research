"""Binance USDⓈ-M public REST client (B05.2): timeout, exponential backoff + jitter, weight budget,
raw response logging. Public endpoints only — there is no signed/order endpoint here (T14)."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from aqe.config import BinanceConfig
from aqe.contracts.core import iso_utc, utc_now
from aqe.storage import RawLogWriter

ALLOWED_PATH_PREFIXES = ("/fapi/v1/klines", "/fapi/v1/time", "/fapi/v1/depth", "/fapi/v1/openInterest",
                         "/futures/data/openInterestHist", "/fapi/v1/fundingRate", "/fapi/v1/premiumIndex",
                         "/fapi/v1/exchangeInfo", "/fapi/v1/aggTrades", "/fapi/v1/ticker/24hr")


class BinanceRestError(RuntimeError):
    def __init__(self, status: int, body: str, path: str):
        super().__init__(f"HTTP {status} on {path}: {body[:300]}")
        self.status = status
        self.body = body


@dataclass
class ClockMeasurement:
    offset_ms: float  # server_time - local_midpoint (positive => local clock is behind)
    rtt_ms: float
    server_time_ms: int
    measured_at_local: str

    @property
    def local_minus_server_ms(self) -> float:
        return -self.offset_ms


class BinanceRest:
    def __init__(self, cfg: BinanceConfig, raw: RawLogWriter | None = None, client: httpx.Client | None = None):
        self.cfg = cfg
        self.raw = raw
        self.client = client or httpx.Client(base_url=cfg.rest_base_url, timeout=cfg.http_timeout_seconds,
                                             headers={"User-Agent": "aqe-research/0.1"})
        self._window_start = time.monotonic()
        self._weight_used = 0
        self.last_used_weight_header: int | None = None

    def close(self) -> None:
        self.client.close()

    def _budget(self, weight: int) -> None:
        now = time.monotonic()
        if now - self._window_start >= 60:
            self._window_start, self._weight_used = now, 0
        if self._weight_used + weight > self.cfg.max_weight_per_minute:
            time.sleep(max(0.0, 60 - (now - self._window_start)) + 0.5)
            self._window_start, self._weight_used = time.monotonic(), 0
        self._weight_used += weight

    def get(self, path: str, params: dict[str, Any] | None = None, weight: int = 1) -> Any:
        if not path.startswith(ALLOWED_PATH_PREFIXES):
            raise ValueError(f"path not in public allow-list: {path}")
        attempt = 0
        while True:
            self._budget(weight)
            sent_at = utc_now()
            try:
                resp = self.client.get(path, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                attempt += 1
                if attempt > self.cfg.max_retries:
                    raise
                self._sleep_backoff(attempt, None)
                continue
            received_at = utc_now()
            used = resp.headers.get("x-mbx-used-weight-1m")
            if used and used.isdigit():
                self.last_used_weight_header = int(used)
                if int(used) > 0.8 * 2400:  # stay well under the exchange limit
                    time.sleep(5)
            if self.raw is not None:
                self.raw.write({
                    "kind": "rest", "path": path, "params": params or {}, "status": resp.status_code,
                    "sent_at": iso_utc(sent_at), "received_at": iso_utc(received_at),
                    "used_weight_1m": used, "body": resp.text if resp.status_code == 200 else resp.text[:2000],
                })
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and "code" in data and "msg" in data and data.get("code") not in (0, 200):
                    raise BinanceRestError(200, resp.text, path)  # e.g. limit=1001 returns 200 with an error body
                return data
            if resp.status_code in (418, 429) or resp.status_code >= 500:
                attempt += 1
                if attempt > self.cfg.max_retries:
                    raise BinanceRestError(resp.status_code, resp.text, path)
                ra = resp.headers.get("Retry-After")
                self._sleep_backoff(attempt, float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)
                continue
            raise BinanceRestError(resp.status_code, resp.text, path)

    def _sleep_backoff(self, attempt: int, retry_after: float | None) -> None:
        if retry_after is not None:
            time.sleep(retry_after)
            return
        base = min(self.cfg.backoff_max_seconds, self.cfg.backoff_base_seconds * (2 ** (attempt - 1)))
        time.sleep(base * (0.5 + random.random()))

    def measure_clock(self, samples: int = 3) -> ClockMeasurement:
        best: ClockMeasurement | None = None
        for _ in range(samples):
            t0 = time.time()
            data = self.get("/fapi/v1/time", weight=1)
            t1 = time.time()
            mid_ms = (t0 + t1) / 2 * 1000
            m = ClockMeasurement(offset_ms=data["serverTime"] - mid_ms, rtt_ms=(t1 - t0) * 1000,
                                 server_time_ms=int(data["serverTime"]), measured_at_local=iso_utc(utc_now()))
            if best is None or m.rtt_ms < best.rtt_ms:
                best = m
        assert best is not None
        return best
