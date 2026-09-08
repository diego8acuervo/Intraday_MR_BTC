# Environment setup — Quant Test (Part A / Part B)

## What is installed

* Repo cloned to `/Users/diegoochoa/Projects/qsCrypto` (fork of qsforex, event-driven
  backtest + live engine with a moving-average-cross strategy).
* venv at `./.venv` on Python 3.11.9 (pyenv). `qsCrypto` installed editable (`pip install -e .`).
* `ib_async 2.1.0`, pandas 3.0.5, numpy, scipy, scikit-learn, matplotlib, seaborn,
  jupyterlab, ipywidgets, python-dotenv.

## Open the workspace

```bash
code /Users/diegoochoa/Projects/qsCrypto/qsCrypto.code-workspace
```

`qsCrypto.code-workspace` pins the interpreter to `.venv`, loads `.env`, wires the
unittest runner, and runs the **bootstrap** task on folder open (`runOptions.runOn`), so
opening the workspace creates the venv, installs deps, writes `.env`, and prints a live
connection check. VS Code asks once to allow the automatic task — approve it, or run it
by hand with `Cmd+Shift+B`.

Launch configs: *IBKR connection check*, *MA-cross backtest*, *Python: current file*.
Tasks (`Cmd+Shift+P` → Run Task): bootstrap, check connection, accounts, positions,
quote, jupyter lab, tests.

## Or from the terminal

```bash
cd /Users/diegoochoa/Projects/qsCrypto
make            # == make bootstrap: venv + deps + .env + dirs + connection check
make help       # list every target
```

| Target | Does |
|---|---|
| `bootstrap` | full setup, ends with a live connection check |
| `check` | connect read-only, resolve front month, print a quote |
| `accounts` / `positions` | balances / open positions across all managed accounts |
| `quote` | front-month contract + current bid/ask |
| `lab` | JupyterLab in the repo root |
| `test` | the repo unit tests (20 pass) |
| `backtest` | inherited MA-cross backtest (needs data, see below) |
| `clean` / `distclean` | drop caches / drop the venv too |

`make check` connects read-only, prints the account summary and open positions, resolves the
front-month contract from `reqContractDetails` (no hardcoded expiry), prints trading
hours, and pulls a quote.

## Connection facts (verified 2026-09-04)

| Item | Value |
|---|---|
| Host / port | `127.0.0.1:7496`, TWS, API enabled |
| Server version | 178 |
| Managed accounts | `DF496956`, `DU496957`–`DU496961` |
| Trading account | `DU496961` (NetLiq ≈ $352k) |
| Front month | `MBTU6`, conId 772435596, expiry 2026-09-25, multiplier 0.1, minTick 5.0 |
| Market data | delayed (`reqMarketDataType(3)`); MBT bid/ask quoting normally |

**This login is a Financial Advisor demo.** `DF496956` is the FA master; the `DU*` accounts
are the tradeable clients. Orders placed without an explicit `order.account` on an FA login
are rejected, so `IB_ACCOUNT` is set (default `DU496961`) and every order must carry it.
There are also FA groups defined in TWS (`COMM` → DU496958, `FX` → DU496957) — not used.

**Each DU account already holds a seeded position** (`IBIT 260918P00045000`, −2). Part A's
restart-reconciliation must match on the traded conId, not on "account has a position".

**Market hours.** MBT liquid hours today are 08:30–16:00, and the feed shows
`20260905:CLOSED;20260906:CLOSED` — the weekend. Schedule the live paper run accordingly, or
use a replay/simulated-tick path for the demo.

**Delayed data caveat.** Type 3 is delayed ~10–15 min, so MAE/MFE computed from it is a lagged
approximation and an early-close can fire late relative to the real tape. Flip
`IB_MARKET_DATA_TYPE=1` in `.env` if a CME crypto subscription gets added.

## Configuration

`.env` (gitignored, copied from `.env.example`) — `IB_HOST`, `IB_PORT`, `IB_CLIENT_ID`,
`IB_ACCOUNT`, `IB_MARKET_DATA_TYPE`, `IB_SYMBOL`, `IB_EXCHANGE`, `IB_CURRENCY`.
Read by `qsCrypto/ibkr/config.py`.

## Fixes made to the inherited repo

Two pre-existing breakages, found by wiring up `make test`:

* `qsCrypto/portfolio/position_test.py` still did `from position import Position` — a
  leftover from the move into the `qsCrypto/` package (its sibling `portfolio_test.py`
  had been updated). Repointed to `qsCrypto.portfolio.position`.
* `qsCrypto/portfolio/portfolio_test.py:267,320` called `self.assertRaises(ps)` on a
  Position object, which raises `TypeError` on modern unittest. The comment says
  "Key doesn't exist", so it is now `assertNotIn(currency_pair, self.port.positions)`.

The unit tests also need `QSCRYPTO_OUTPUT_RESULTS_DIR` set (`Portfolio` writes an equity
file at construction); the `test` target exports it.

## Known gap in the inherited repo

`qsCrypto.examples.mac` (the MA-cross backtest) imports and constructs fine but exits with
`IndexError` in `HistoricCSVPriceHandler`: it expects tick files named `PAIR_YYYYMMDD.csv`
and the repo only ships `AAPL-L1.csv`-style files. Part B needs its own data pull
(Binance klines) before that path runs.

## Files added

`Makefile`, `qsCrypto.code-workspace`, `qsCrypto/ibkr/{config,contracts,check_connection}.py`,
`.env.example`, `SETUP.md`. `.gitignore` gained `.env`, `.venv/`, `output/`, `journals/`.

## Not done yet

Part A trading logic (bracket entry, MAE/MFE tracking, early-close, add rule, journal,
kill switch) and Part B. Only the environment and broker connectivity are in place.
