# Third-party sources (R02, B02)

Retrieved/verified 2026-11-11 KST. "Reuse" says exactly what AQE takes from each source. No upstream
repository was copied wholesale into this project.

| Component | Official source | Pinned / installed version | How it is used | Files copied into AQE | License (as published) |
|---|---|---|---|---|---|
| LightGBM | https://github.com/lightgbm-org/LightGBM (former https://github.com/microsoft/LightGBM) | `lightgbm==4.7.0` (PyPI wheel, core venv) | Python package for multiclass GBDT training/inference; AQE writes dataset, time split, wrapper (`src/aqe/models/lgbm.py`) | none | MIT |
| Temporal Convolutional Network design | https://github.com/locuslab/TCN (Bai, Kolter, Koltun 2018, arXiv:1803.01271) | design reference only | Causal dilated residual block design re-implemented in `src/aqe/models/tcn.py` (left padding instead of pad+chomp, per-timestep 1x1 head) | none (no code copied) | MIT |
| PyTorch | https://github.com/pytorch/pytorch | `torch==2.15.0+cu132` (download.pytorch.org cu132 index); CPU build `2.15.0+cpu` also verified | Tensor ops, Conv1d, weight_norm parametrization, AdamW | none | BSD-3-Clause |
| scikit-learn | https://github.com/scikit-learn/scikit-learn | `scikit-learn==1.9.1` | `LogisticRegression` (multinomial, L2), `StandardScaler` for the meta model | none | BSD-3-Clause |
| TradingAgents | https://github.com/TauricResearch/TradingAgents | git SHA `9fa1196fffbcfd9de5146d2dd2869df84ab99838` (package 0.7.1, commit 2026-11-04 10:13:55 UTC), installed in the separate `.venv-ta` via `pip install "tradingagents @ git+...@<sha>"` (`direct_url.json` commit verified) | Real `TradingAgentsGraph.propagate(..., asset_type="crypto")` driven by `src/aqe/context/ta_bridge.py` | none (runtime monkeypatches only; see docs/UPSTREAM_PATCHES.md) | Apache-2.0 |
| Binance USDⓈ-M public API docs | https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/ | docs retrieved 2026-11-10 UTC | Endpoint paths, `/public` vs `/market` WS split, local order book U/u/pu procedure, rate limits | none | n/a (API documentation) |
| pydantic, PyYAML, numpy, pandas, pyarrow, httpx, websockets, psycopg | PyPI | see `requirements.lock` | contracts, config, data handling, IO | none | MIT / BSD / Apache-2.0 / LGPL-3.0 (psycopg; optional, not yet exercised) |

Optional local LLM (Qwen3 / Qwen3-8B weights): **not installed**. No resource/license/quality validation
has been done; it is not a default dependency.

Lock files: `requirements.lock` (core venv `pip freeze`), `requirements-ta.lock` (TradingAgents worker venv).
