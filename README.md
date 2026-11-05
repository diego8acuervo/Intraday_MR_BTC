# Intraday Mean Reversion BTC Structure (CME Futures @ IBKR)

This production-grade repository implements and executes the **Turtle Mean Reversion / Fade Strategy** on Chicago Mercantile Exchange (CME) Bitcoin futures (primarily using the micro contract `MBT`, and optionally `BRR`), designed for event-driven live trading and execution on Interactive Brokers (IBKR).

It leverages an asynchronous architecture to cleanly handle real-time market feeds (`TickEvent`/`BarEvent`), state management via a robust portfolio module, and concurrent order execution via IBKR's Trader Workstation (TWS) or Gateway.

---

## 🌎 1. Architecture Map & Asynchronous Engine

The execution pipeline follows a strict unidirectional, decoupled event-routing structure:

```
[Interactive Brokers API]
         │  (Real-Time Feeds)
         ▼
 ┌───────────────┐
 │ Streaming Data│ ─────► Emits BarEvent (closes every 30m) & TickEvent (bid/asks)
 └───────────────┘
         │
         ▼ (Enqueued)
 ┌───────────────┐
 │ Event Queue   │
 └───────────────┘
         │
         ├───► [1. Strategy] ────► Computes Donchian Channel, EMA(20) of True Range (N)
         │                         Emits SignalEvent when breakout fails (inside confirmation)
         │
         ├───► [2. Portfolio] ───► Receives SignalEvents, applies money management & sizing
         │                         Manages open units, MFE/MAE thresholds, stops/targets
         │                         Emits OrderEvent (bracket: entry + stop + profit target)
         │
         ├───► [3. Execution] ───► Translates details, resolves contracts, sends bracket to TWS
         │                         Emits FillEvent once filled
         │
         └───► [4. Portfolio] ───► Consumes FillEvent, reconciles positions, emits PortfolioEvent
                                            │
                                            ▼
                                  ┌───────────────────┐
                                  │ Monitor Class     │ ───► Writes JSONL / Atomic Live State
                                  └───────────────────┘
```

The system is compartmentalized into clean modules:
*   `qsCrypto/data/streaming.py`: Manages market-data subscriptions, queues live feeds, and handles connection states recursively.
*   `qsCrypto/event/event.py`: Lightweight schema definitions for data-carrying event classes.
*   `qsCrypto/portfolio/portfolio.py`: Coordinates positions, units, scaling (upsizing on N-intervals), risk limits, and real-time computation of PnL, MAE, and MFE in risk units (R).
*   `qsCrypto/strategy/strategy.py`: Formulates signal triggers based on failed Donchian breakouts with confirmation checks.
*   `qsCrypto/execution/execution.py`: Handles Order routing, brackets construction, custom order placement, and contract metadata resolution.
*   `qsCrypto/trading/trading.py`: Runs the central, synchronized polling event wheel directing enqueued tasks.

---

## ⚡ 2. Quick Setup & Bootstrapping (Under 5 Minutes)

Our environments are fully standardized. Follow these precise instructions to install dependencies and test execution.

### Prerequisites & Dependencies
To ensure this repository is executable on any Operating System (macOS, Linux, Windows), we manage environments cleanly either via standard `venv` or `make`. 

*   **Python:** Version `3.11.x` is strictly recommended (tested and integrated with Python `3.11.9`).
*   **Active Gateway/TWS:** Ensure Interactive Brokers Trader Workstation (TWS) or IB Gateway is running in **Paper Trading mode** before launching.

### Standard Setup (For any OS)
Choose either the automated build command or step-by-step raw terminal execution.

#### Option A: Automated environment setup (Makefile)
Simply open your shell in the root of `/Users/diegoochoa/Projects/Intraday_MR_BTC` and run:
```bash
# Sets up a virtual environment (.venv) using Python 3.11, installs all dependencies (including ib_async, nest_asyncio, and Jupyter tooling), and prints verification instructions
make bootstrap
```

#### Option B: Step-by-Step Manual Setup (Cross-Platform compatibility)
If `make` is unavailable on your OS (e.g., native Windows without WSL):

1.  **Create your Environment (Python 3.11 Recommended):**
    ```bash
    python3.11 -m venv .venv
    source .venv/bin/activate  # On Windows: .venv\Scripts\activate
    ```
2.  **Install Requirements & Notebook/Live-trading Packages (`ib_async`, `nest_asyncio`):**
    ```bash
    pip install --upgrade pip setuptools wheel
    pip install -r requirements.txt
    pip install ib_async nest_asyncio jupyterlab ipywidgets ipympl
    pip install -e .
    ```
3.  **Environment Variables:**
    Create a `.env` file in the project folder with the following variables:
    ```env
    IB_HOST=127.0.0.1
    IB_PORT=7497       # Default TWS Paper port. Use 4002 for Gateway Paper
    IB_CLIENT_ID=99    # Unique ID for API connection
    IB_ACCOUNT=        # Optional: Force a specific paper account ID
    ```

---

## 📁 3. Core Deliverables & Execution Instructions

### A. PART A: Live Paper-Trading Integration
To verify that real-time execution, contract resolution, live pricing, bracket placement, and MAE/MFE logs work properly, utilize the:
*   **Notebook:** `qsCrypto/notebooks/Turtle_MR_Intraday_Live.ipynb`

#### Running Part A:
1.  Open VS Code and navigate to `qsCrypto/notebooks/Turtle_MR_Intraday_Live.ipynb`.
2.  Run **Cell 1** to load necessary imports. It will automatically resolve the absolute root path.
3.  Run **Cell 2**. This will instantiate `ib = IB()`, programmatically resolve your paper account balance/equity, and attempt a connection (`ib.connect`) to your local TWS instance.
4.  Run **Cell 3**. This will load entry rules, stops, target margins, and critical risk parameters (e.g., `STOP_N = 1.0`, `RISK_PER_UNIT = 0.01`). `INSTRUMENT` selects what is traded: `"IBIT_CFD"` (default — CFD on the IBIT bitcoin ETF via SMART; bars from the underlying IBIT stock, polled at each 30m close of the regular session, since IBKR serves no history for CFDs), `"MBT"` (the CME micro future of the backtest, bars via `keepUpToDate`) or `"EURUSD"` (spot EUR.USD on IDEALPRO: the one with real-time ticks on this account, so it exercises tick-level MAE; `MIDPOINT` bars, orders capped at 30,000 USD notional, positions closed from this session's own fills because IBKR does not report spot FX in `reqPositions`). IBIT and EUR.USD have no backtest of their own: they run with the parameters calibrated on MBT, as an engine test.
5.  Run **Cell 4**. This cell creates the data streaming handler, resolves the active front-month contract programmatically via IBKR contract details (no expiries are hardcoded), creates the execution and portfolio handlers, and scales the contract risk.
6.  Run **Cell 5**. It instantiates the append-only JSONL `Monitor` at `journals/` and a live state snapshot writer at `output/`.
7.  Run **Cell 6 (Arrancar la sesión)**. Setting `RUN_LIVE = True` immediately fires up the `trade-loop` thread and the `ibkr-stream` thread, letting the bot scan ticks in the background.
8.  Run **Cell 7 (Monitor de posiciones abiertas)** at any time. It will print an active DataFrame of open positions with current PnL (R), MAE (`mae_r` and price), MFE, and distance to the brackets.
9.  To stop and flatten all entries safely without leaving orphaned orders, run **Cell 8 (Cierre de la jornada)** and uncomment `kill_all()`.

---

### B. PART B: Backtester Setup & Strategy Review
To understand our edge validation, statistical criteria, search grids, and Monte Carlo checks, examine:
*   **Notebook:** `qsCrypto/notebooks/MeanReversion_IBKR_CME_Bitcoin.ipynb`

#### Execution & Structure of Backtest:
*   **Warmup & Downloader:** Section §1 & §2 dynamically fetch rolling historical CME data and cache it locally in `data_cache/ibkr/30mins/` or `1day/` as continuous contract chains.
*   **Replication of Signal Engine:** Section §4 implements vectorized/iterative operations to calculate Donchian bounds (20 period), EMA ATR (20 period), and places entries under confirmation checks (`SIGNAL_MODE = 'fade'`).
*   **Monte Carlo & Validation Checklist:** Section §10 runs hundreds of path-dependent randomizations to compute probability of bankruptcy as well as the **Deflated Sharpe Ratio (DSR)** to prune research luck and selection bias of the parameter sweep.
*   **Review Walk-Forward Stability:** It conducts cross-validation folds OOS to ensure statistical robustness on unseen data.

---

### C. PART C: Binance USDC Perpetuals — Market-Maker Volume & Rebate Exercise
The same fade system, transplanted to `BTCUSDC`, `ETHUSDC` and `SOLUSDC` perpetuals, which are commission-free for a registered market maker (reachable with a 100k account). The question is not only whether the strategy makes money there, but **how much notional volume (in USDT) a 100k account trades per month with a 10x maximum leverage**, and what that volume is worth in rebates.
*   **Notebook:** `qsCrypto/notebooks/MeanReversion_Binance_Perp.ipynb`, **§13** (§1–§12 are the Binance USDT pilot and the CME-vs-Binance comparison that §13 builds on).

#### How it is modeled:
*   **One account, not one backtest per symbol:** `run_portfolio_backtest` (`qsCrypto/backtest/engine.py`) runs the three symbols on a single 100k balance with the live `BinancePerpetualsPortfolio` limits: **10x gross leverage across the whole account** (an order that doesn't fit is rejected, not trimmed), 12 units per direction, 6 per correlation group and direction, and `max_units=3` for SOL. Risk is 1% of total account equity per trade.
*   **Volume from every fill:** each entry, pyramid add and exit is logged with its notional (`fills`), converted from USDC to USDT at the daily `USDCUSDT` spot rate.
*   **Data:** real 30m klines and funding from the public Binance endpoints, from the 2024-01-03 listing to 2026-09-09.
*   **Regimes and costs:** 24/7 vs. the CME session only (13:30–20:15 UTC, weekdays); 0 commission, with 0/1/3 bp slippage per side and a 2 bp commission control; 10x vs. 4x.
*   **Parity:** the engine was refactored into one book per symbol; `run_backtest` reproduces all previous results bit for bit, and `qsCrypto/backtest/engine_test.py` pins the portfolio limits.

#### Results (central scenario: CME session, 10x, 0 commission, 1 bp slippage):
| | Monthly volume (USDT) | Rebate at 0.5 bp on all volume | Rebate at 0.5 bp on maker volume only (14.3%) |
|---|---|---|---|
| Median month | ~31M | ~1,550 USDT | ~220 USDT |
| Worst 10% of months | ≤18.5M | ≤~925 USDT | ≤~130 USDT |
| Best 10% of months | ≥64M | ≥~3,185 USDT | ≥~455 USDT |
| Median, account held at 100k | ~22M | ~1,095 USDT | ~155 USDT |

Volume scales with equity (monthly turnover ≈ 220x equity); the monthly table is written to `output/usdc_mm_volumen_mensual.csv`. The strategy's median monthly P&L in this scenario is ~3,800 USDT, so rebates are a supplement (+5% to +40%), not what carries the account. Rebate figures are static: the backtest does not reinvest them.

#### What has to be true for those figures to exist:
1.  **Trade only the CME session.** 24/7 loses ~90% a year at *any* cost, including zero: the signal is negative before friction (same finding as §12). No commission exemption fixes that.
2.  **The exemption (and the rebate) must cover taker flow.** Only 14% of the volume can be a limit order (exits at the target); entries, adds, stops and time exits are market orders. If the program only covers maker flow, a 2 bp taker fee would cost ~5.3k USDT/month — more than the median P&L — and the result turns to −18% a year. **Confirm the program terms with Binance first.**
3.  **Slippage ≤ ~1 bp per side on the USDC books.** At 3 bp the central scenario goes from +41% to −15% a year. USDC books are thinner than USDT (SOLUSDC ~8B/month vs ~66B for BTCUSDC): measure it before relying on it.
4.  **The +41% a year is optimistic.** The session restriction was chosen in §12 on a window that overlaps this sample; on the stretch that did not inform that choice (2024-01 → 2025-04) it is +17% a year, Sharpe 0.22.
5.  **Accept ~48% drawdowns.** The 10x cap rarely binds (95th percentile of leverage used: 5.5x); versus 4x it adds volume (~31M vs ~24M/month) more than return.

---

## 🔧 4. Crucial Assumptions & Exclusions
The following guidelines detail what was purposely cut to stay within a rapid deployment window, as well as critical risk-mitigation measures:

1.  **No Margin Simulation:** We assume a cash balance and enforce a gross leverage cap proxy (`MAX_GROSS_LEV = 4.0`). Broker maintenance margin requirements call triggers are not simulated inside the backtester.
2.  **No Back-adjusted Continuous Gaps:** The data caching layers download contract chains continuously as IBKR provides them. Intraday price splits and roll adjustments are not retroactively smoothed or offset.
3.  **Conservative Signal Confirmations:** Brute-force breakout trading results in chain losses during range contractions. By enforcing `REQUIRE_CONF = True` (requiring prices to drift back *inside* the Donchian channel), we eliminated false breakouts, doubling overall expectancy.
4.  **No Intrabarra path resolution:** Brackets inside the backtest are checked at bar close using pessimistic assumptions (if both stop and profit target are matched within the same 30m bar, we assume the loss took place first).
5.  **Exclusion of Sun_Valley folder:** All files under the `Sun_Valley` folder are strictly excluded from this repository context to protect proprietary source briefs.
