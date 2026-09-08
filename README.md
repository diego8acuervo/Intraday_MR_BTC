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

*   **Python:** Version `3.11.x` is strictly recommended. Native compatibility is guaranteed inside our Makefile.
*   **Active Gateway/TWS:** Ensure Interactive Brokers Trader Workstation (TWS) or IB Gateway is running in **Paper Trading mode** before launching.

### Standard Setup (For any OS)
Choose either the automated build command or step-by-step raw terminal execution.

#### Option A: Automated environment setup (Makefile)
Simply open your shell in the root of `/Users/diegoochoa/Projects/Intraday_MR_BTC` and run:
```bash
# Sets up a virtual environment (.venv), installs all dependencies, and prints verification instructions
make bootstrap
```

#### Option B: Step-by-Step Manual Setup (Cross-Platform compatibility)
If `make` is unavailable on your OS (e.g., native Windows without WSL):

1.  **Create your Environment:**
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate  # On Windows: .venv\Scripts\activate
    ```
2.  **Install Requirements:**
    ```bash
    pip install --upgrade pip
    pip install -r requirements.txt
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
4.  Run **Cell 3**. This will load entry rules, stops, target margins, and critical risk parameters (e.g., `STOP_N = 1.0`, `RISK_PER_UNIT = 0.01`).
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

## 🔧 4. Crucial Assumptions & Exclusions
The following guidelines detail what was purposely cut to stay within a rapid deployment window, as well as critical risk-mitigation measures:

1.  **No Margin Simulation:** We assume a cash balance and enforce a gross leverage cap proxy (`MAX_GROSS_LEV = 4.0`). Broker maintenance margin requirements call triggers are not simulated inside the backtester.
2.  **No Back-adjusted Continuous Gaps:** The data caching layers download contract chains continuously as IBKR provides them. Intraday price splits and roll adjustments are not retroactively smoothed or offset.
3.  **Conservative Signal Confirmations:** Brute-force breakout trading results in chain losses during range contractions. By enforcing `REQUIRE_CONF = True` (requiring prices to drift back *inside* the Donchian channel), we eliminated false breakouts, doubling overall expectancy.
4.  **No Intrabarra path resolution:** Brackets inside the backtest are checked at bar close using pessimistic assumptions (if both stop and profit target are matched within the same 30m bar, we assume the loss took place first).
5.  **Exclusion of Sun_Valley folder:** All files under the `Sun_Valley` folder are strictly excluded from this repository context to protect proprietary source briefs.
