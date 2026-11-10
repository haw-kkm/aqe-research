# Third-party notices

AQE depends on the following third-party software. No source files from these projects are vendored
into this repository; they are installed as packages. Their licenses apply to their own code.

- **LightGBM** — MIT License. Copyright (c) Microsoft Corporation / LightGBM contributors.
- **PyTorch** — BSD-3-Clause License. Copyright (c) Meta Platforms, Inc. and affiliates and contributors.
- **scikit-learn** — BSD-3-Clause License. Copyright (c) the scikit-learn developers.
- **TradingAgents** (TauricResearch) — Apache License 2.0. Used unmodified at commit
  9fa1196fffbcfd9de5146d2dd2869df84ab99838 in a separate virtual environment; AQE applies runtime
  monkeypatches only (documented in docs/UPSTREAM_PATCHES.md). No upstream file is changed or redistributed.
- **TCN reference** (locuslab/TCN) — MIT License. AQE's `src/aqe/models/tcn.py` is an independent
  implementation of the published causal residual-block design; no code was copied.
  Reference: S. Bai, J. Z. Kolter, V. Koltun, "An Empirical Evaluation of Generic Convolutional and
  Recurrent Networks for Sequence Modeling", arXiv:1803.01271, 2018.
- pydantic (MIT), PyYAML (MIT), numpy (BSD-3-Clause), pandas (BSD-3-Clause), pyarrow (Apache-2.0),
  httpx (BSD-3-Clause), websockets (BSD-3-Clause), psycopg (LGPL-3.0, optional).

Market data from Binance public endpoints is subject to Binance's terms of use.
