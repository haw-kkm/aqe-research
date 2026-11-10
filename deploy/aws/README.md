# AWS deployment preparation (NOT provisioned)

Nothing in this folder has been created in any AWS account. This is a sizing/cost checklist only (B14).

Suggested shape (single-region, minimal):
| Role | Service (example) | Notes |
|---|---|---|
| Collector + numeric analyze (always on) | 1x EC2 t4g.medium/t3.medium or ECS Fargate task | `aqe collect` + 5-min `aqe analyze` (shadow); needs NTP (chrony) — clock skew is a risk rule |
| Context worker (slow, LLM) | separate process/task, concurrency 1 | `.venv-ta` image; outbound only to the configured LLM endpoint; budget caps in config |
| Raw/normalized/artifacts | S3 (versioned bucket, lifecycle to IA after 30 d) | raw depth logs ≈ hundreds of MB/day compressed |
| Metadata DB | RDS PostgreSQL (+ TimescaleDB not available on RDS → use plain PG or self-managed Timescale) | apply `migrations/postgres/001_init.sql` via `aqe init-db` |
| Secrets | SSM Parameter Store / Secrets Manager → env vars | least privilege: no exchange keys exist or are needed |
| Training | on-demand GPU (g5.xlarge) or local GPU | TCN fold protocol ≈ 10–20 min on an RTX 3060 Ti class GPU |

Cost items to estimate before provisioning: compute hours, S3 storage + PUT requests (avoid tiny objects —
collector rotates hourly), RDS instance + storage, data transfer out, LLM API spend (hard cap via
`context.max_cost_usd_per_day`), CloudWatch logs. No exchange order permissions are ever configured.
