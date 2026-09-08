---
name: turtlerisk
description: Turtle Trading position sizing and risk management for crypto perpetuals — N/ATR volatility units, Donchian channel entries and exits, N-based stops (1N = 1R), MAE-derived early close, triple-barrier exits, unit adding at 1/2N intervals, portfolio correlation limits, drawdown-based account reduction, and how these act as hard constraints on top of the CPPI pipeline. Use whenever implementing, modifying, debugging or reviewing Turtle rules, ATR/N-based sizing, volatility-normalized units, the TurtlePositionLimits class or calibrate_turtle_limits() in this codebase. Triggers: turtle, Donchian, ATR, N units, position sizing, unit limits, stop placement, pyramiding, correlation limits, drawdown reduction.
---

# Turtle Risk — Position Sizing & Risk Management for Crypto Perpetuals

Reference skill for Turtle Trading position sizing, ATR/N-based inventory limits,
and how they integrate as hard constraints on top of the CPPI pipeline.

Invoke when: implementing, modifying, or debugging code related to Turtle Trading
rules, ATR/N-based position sizing, volatility-normalized units, Donchian channel
entries/exits, N-based stop placement, unit adding at ½N intervals, portfolio-level
correlation risk limits, drawdown-based account reduction, or the `TurtlePositionLimits`
class and `calibrate_turtle_limits()` method in this codebase.

---

## 1. Core Mathematical Framework

### 1.1 N — The Volatility Measure

N is the 20-day exponential moving average of True Range. It is the foundational
building block of all Turtle position sizing and risk calculations.

**True Range:**
```
TR = max(H − L, |H − PDC|, |PDC − L|)
```
Where H = current high, L = current low, PDC = previous day's close.

**N (20-day EMA of TR):**
```
N = (19 × PDN + TR) / 20
```
Seed with a 20-day SMA of TR. Fetch at least `ATR_PERIOD + 30` candles for warm-up.

**Canonical implementation** — `qsCrypto/portfolio/turtle_risk_units.py::calc_n()`:
```python
def calc_n(candles: list[Candle], period: int = 20) -> float:
    tr = [candles[0].h - candles[0].l]
    for i in range(1, len(candles)):
        pdc = candles[i - 1].c
        tr.append(max(
            candles[i].h - candles[i].l,
            abs(candles[i].h - pdc),
            abs(pdc - candles[i].l),
        ))
    if len(tr) < period:
        raise ValueError(f"Need {period}+ candles")
    n = sum(tr[:period]) / period        # SMA seed
    for t in tr[period:]:
        n = ((period - 1) * n + t) / period  # EMA update
    return n
```

### 1.2 Unit Sizing

One unit moving 1N produces exactly `RISK_PER_UNIT × Account_Equity` in P&L.

**Crypto perpetuals (price IS the point):**
```
Unit_Qty = (RISK_PER_UNIT × Account_Equity) / N
Max_Qty  = Unit_Qty × MAX_UNITS
```

**Verification identity — always assert in tests:**
```
Unit_Qty × N == RISK_PER_UNIT × Account_Equity
```

Example — $33,000 account, BTC N = $2,700:
- Unit_Qty = (0.01 × 33,000) / 2,700 = 0.122 BTC
- Max_Qty = 0.122 × 4 = 0.489 BTC  ← stored as `q_directional` in config

### 1.3 Margin and Stops

```
Required_Margin = (Max_Qty × Price) / Leverage
Long_Stop       = Entry − (STOP_N × N)     # STOP_N = 1.0 in this codebase
Short_Stop      = Entry + (STOP_N × N)
```

**R is the stop distance.** With `STOP_N = 1.0`, `R = 1N`, so one unit stopped out
loses exactly `RISK_PER_UNIT` (1%) of equity. Every excursion in this codebase is
read in R, so changing `STOP_N` rescales every MAE/MFE figure — it is not a free
knob. Canonical Turtle used 2N; this repo uses 1N and the code is the authority
(`qsCrypto/portfolio/turtle_risk_units.py::STOP_N`).

**MAE as the inner barrier.** The hard stop above is the outermost protection and
the definition of R. Inside it sits a data-derived early close at `MAE*`, taken
from the empirical MAE distribution of winning trades (see
`../backtester/references/MAE.md` and `qsCrypto/portfolio/mae.py`). Measured on
30-minute MBT/BRR bars, `MAE* = 1.54N` — **wider** than the 1N stop, i.e. the data
says this stop already cuts winners rather than that it should be tighter.

### 1.4 Unit Adding

Add 1 unit at each **½N interval** from the actual fill price:
```
Unit 1: Breakout fill
Unit 2: Fill_1 + ½N
Unit 3: Fill_2 + ½N
Unit 4: Fill_3 + ½N
```
Raise all stops to `STOP_N × N` (1N here) from each new unit's fill price.

Aggregate risk to the common stop across the ladder: **1.0N** with one unit,
**1.5N** with two and three, back to **1.0N** at full load, because the earlier
units are already in profit. The 1.5N peak is the number to watch; claiming a
flat 1% at every stage is false. Fixed by
`qsCrypto/portfolio/mae_test.py::TestUnitRiskArithmetic`.

---

## 2. Portfolio Risk Limits

| Level | Scope                    | Max Units |
|-------|--------------------------|-----------|
| 1     | Single market            | 4         |
| 2     | Closely correlated group | 6         |
| 3     | Loosely correlated group | 10        |
| 4     | Single direction (total) | 12        |

### Correlation Groups (`qsCrypto/portfolio/turtle_risk_units.py`)
```python
CORRELATION_GROUPS = {
    "layer1_major":  {"BTC", "ETH"},
    "layer1_alt":    {"SOL", "AVAX", "NEAR", "APT", "SUI", "DOT", "ADA", "HBAR"},
    "meme":          {"DOGE", "PNUT", "WLD"},
    "defi":          {"AAVE", "LINK"},
    "legacy":        {"LTC", "BCH", "XLM", "XTZ"},
    "exchange":      {"BNB"},
}
```

### Drawdown Account Reduction
Per Turtle rules: −10% from starting equity → reduce notional by 20%. Repeat.
```python
def adjusted_notional(starting_equity: float, current_equity: float) -> float:
    notional = starting_equity
    drawdown = starting_equity - current_equity
    while drawdown >= notional * 0.10:
        drawdown -= notional * 0.10
        notional *= 0.80
    return notional
```

---

## 3. Codebase Integration

### 3.1 File Map

> **Note:** in *this* repo (`qsCrypto`) only `qsCrypto/portfolio/turtle_risk_units.py`
> exists — it holds the standalone CLI (`Candle`, `get_klines`, `calc_n`, `calc_unit`,
> `check_portfolio`), but **not** a `TurtlePositionLimits` class. The `calibration/`,
> `strategy/`, `examples/` and `scripts/` rows below describe the companion
> market-maker repo where this skill originated; treat them as the target design for
> the CPPI integration, not as paths present here.


| File | Role |
|---|---|
| `qsCrypto/portfolio/turtle_risk_units.py` | Standalone CLI: `Candle`, `get_klines`, `calc_n`, `corr_group`, `calc_unit`, `check_portfolio`, `print_table` (present in this repo) |
| `calibration/calibrator.py` | `Calibrator.calibrate_turtle_limits()` — fetches daily klines at init, writes to cache |
| `strategy/calibration_cache.json` | Persistent cache; adds `turtle_n` and `turtle_max_qty` fields per token |
| `strategy/as_mm_strategy_config.json` | `cppi_overrides.{TOKEN}.q_directional` holds the static 4-unit max_qty fallback |
| `examples/as_market_making.py` | Composition root — instantiates `TurtlePositionLimits`, clamps `CPPIState.Qmax_eff` |
| `scripts/calibrate.py` | Calls `calibrate_turtle_limits()` as Phase 2 after A-S calibration |

### 3.2 `TurtlePositionLimits` — Public API

```python
limits = TurtlePositionLimits(account_equity=CAPITAL_INICIAL)
limits.refresh(PAIRS)            # blocking, sync — call at startup before loop
limits.get_max_qty("BTCUSDT")   # returns max base-token qty (inf until refreshed)
limits.set_account_equity(eq)   # call alongside CPPI equity poll
limits.maybe_refresh(PAIRS)     # no-op until 24 h elapses; call in trade loop
```

### 3.3 How It Clamps CPPI in the Composition Root

```python
# examples/as_market_making.py — inside the TICK handler, after cppi.compute()
cppi_state[sym] = cppi.compute(sym, mid, sigma)

if turtle_limits is not None:
    turtle_limits.maybe_refresh(PAIRS)
    t_max = turtle_limits.get_max_qty(sym)
    cs = cppi_state[sym]
    if cs.Qmax_eff > t_max:
        cs.Qmax_eff = t_max
        cs.qty_bid, cs.qty_ask = cppi_quote_sizes(
            cs.inventory,
            t_max,
            cs.q_target,
            n_slices=cppi._n_slices,
        )
```

`CPPIState` is a non-frozen dataclass — fields are directly mutable.
No changes required to `strategy.py` or `portfolio.py`.

### 3.4 `calibrate_turtle_limits()` — Calibration Cache Integration

Called from `scripts/calibrate.py` as Phase 2 after A-S calibration:

```python
turtle_results = await cal.calibrate_turtle_limits(
    symbols=symbols,            # e.g. ["BTCUSDC", "SUIUSDC"]
    account_equity=CAPITAL_INICIAL,
    cache_path=_CACHE_PATH,     # strategy/calibration_cache.json
)
```

- Normalizes symbols to `{TOKEN}USDT` for the daily klines fetch
- Fetches 50 daily bars concurrently via aiohttp
- Calls `calc_n()` → computes `turtle_max_qty = (0.01 × equity × 4) / N`
- Merges `turtle_n` and `turtle_max_qty` into existing cache entries
  (sigma/kappa entries are untouched)
- Saves atomically via `store.save()`

Resulting cache schema per token:
```json
{
  "BTC": {
    "sigma": 45.32, "kappa": 1.84, "lambda_ask": 183.5,
    "turtle_n": 2813.42,
    "turtle_max_qty": 0.4690
  }
}
```

### 3.5 Static Fallback — `q_directional` in Config

`cppi_overrides.{TOKEN}.q_directional` stores the last known 4-unit max_qty.
Used by `StrategyConfig.as_params()` when:
- CPPI is disabled (`CAPITAL_INICIAL = None`)
- Cache does not yet have `turtle_max_qty`
- `TurtlePositionLimits` has not yet refreshed

Current values (computed at $33,000 equity, 1% risk/unit):

| Token | q_directional | Token | q_directional |
|-------|-------------|-------|-------------|
| BTC   | 0.489       | LINK  | 2753.81     |
| ETH   | 11.38       | BNB   | 64.52       |
| SOL   | 284.36      | SUI   | 23374.3     |
| XRP   | 22973.8     | XAU   | 11.006      |
| DOGE  | 291240      | LTC   | 690.816     |
| ADA   | 89576       | BCH   | 79.889      |
| AVAX  | 2423        | XLM   | 173254      |
| DOT   | 15761.8     | XTZ   | 75440.8     |
| HBAR  | 321667      | APT   | 22257.5     |

---

## 4. Entry & Exit Rules (Turtle System Reference)

### System 1 — Short-term (20-day Donchian)
- Long: price exceeds 20-day high by 1 tick
- Short: price drops below 20-day low by 1 tick
- **Filter:** Skip if previous breakout was profitable; use System 2 as failsafe

### System 2 — Long-term (55-day Donchian)
- Long: price exceeds 55-day high
- Short: price drops below 55-day low
- No filter — take all breakouts

### Exits

Canonical Turtle (channel exits):
- System 1: 10-day low (longs) / 10-day high (shorts)
- System 2: 20-day low (longs) / 20-day high (shorts)

**Superseded in this repo by a triple barrier** (Lopez de Prado, AFML ch. 3,
applied as an execution rule rather than a label), replacing both the channel
exit and the bare stop:

| Barrier | Level | Role |
|---|---|---|
| Lower (hard) | `−STOP_N·N` = −1N | Defines R; lives in the broker as a bracket order |
| Lower (inner) | `−MAE*·N` | Data-derived early close |
| Upper | `+TAKE_N·N` = +3N | Target, anchored on the FIRST fill, not the last add |
| Vertical | `MAX_HOLD_BARS` = 10 bars | Holding limit; also sets the OOS embargo |

---

## 5. Constants

```python
RISK_PER_UNIT = 0.01    # 1% of equity per unit
ATR_PERIOD    = 20      # 20-day EMA of True Range
MAX_UNITS     = 4       # Max units per single market
STOP_N        = 1.0     # Stop distance in N multiples; R = STOP_N x N
TAKE_N        = 3.0     # Upper barrier (profit target)
MAX_HOLD_BARS = 10      # Vertical barrier in bars; also the OOS embargo
CANDLE_LIMIT  = 50      # ATR_PERIOD + 30 for warm-up
```

---

## 6. Testing Invariants

```python
def test_unit_sizing_identity():
    """1 unit × 1N move == RISK_PER_UNIT × account_equity."""
    account = 33_000
    for n in [2700, 8.5, 0.008]:
        unit_qty = (0.01 * account) / n
        assert abs(unit_qty * n - account * 0.01) < 0.01

def test_stop_risk():
    """1N stop on 1 unit == 1% of account (STOP_N = 1.0)."""
    account = 33_000
    n = 2700.0
    unit_qty = (0.01 * account) / n
    assert abs(unit_qty * 1 * n - account * 0.01) < 0.01

def test_ladder_risk():
    """Adds every 1/2N with stops trailed to 1N: 1.0 / 1.5 / 1.5 / 1.0 N."""
    agg = lambda k: sum((1.0 + 0.5*i) - ((1.0 + 0.5*(k-1)) - 1.0) for i in range(k))
    assert [round(agg(k), 2) for k in (1, 2, 3, 4)] == [1.0, 1.5, 1.5, 1.0]
```

---

## 7. Common Pitfalls

1. **Truncating N to price precision.** Retain full float precision internally;
   only truncate quantities at order time using the exchange `step_size`.
2. **Close-to-close returns instead of True Range.** True Range must include
   gap (|H − PDC| and |PDC − L|) — especially for daily candles.
3. **Forgetting to raise stops when adding units.** Each add must raise ALL
   stops to 1N from the newest fill. That is what keeps aggregate risk at 1.0N
   with the ladder full instead of 4x.
4. **SHIB/1000-prefixed symbols.** Binance perp is `1000SHIBUSDT`; the raw
   `SHIBUSDT` symbol returns HTTP 400. Skip or use `symbol_override` from config.
5. **Rounding unit_qty UP.** Always `floor()` to exchange precision — rounding
   up can silently exceed intended risk.
6. **Stale `turtle_max_qty` after equity changes.** `TurtlePositionLimits`
   auto-refreshes ATR every 24 h but equity must be updated via
   `set_account_equity()` — same cadence as `CPPIRiskManager.set_account_equity()`.

---

## 8. CLI (Standalone Risk Sheet)

```bash
# Print full risk unit sheet for all tokens at default equity
python -m Binance_Market_Maker.portfolio.turtle_risk

# Custom account and leverage
python -m Binance_Market_Maker.portfolio.turtle_risk --account 50000 --leverage 10
```

---

## 9. Skill Resources

| File | Contents |
|---|---|
| `../backtester/references/MAE.md` | MAE methodology: MAE* threshold, P(win | MAE≥x) curve, E-ratio, stop calibration from data |
| `../backtester/references/lopez_de_prado.md` | Triple barrier, purging, embargo, CPCV, DSR/PBO |
| `../backtester/scripts/purged_cv.py` | `walk_forward_folds`, `PurgedKFold`, `CombinatorialPurgedCV` |
| `../backtester/scripts/stats_tests.py` | Deflated Sharpe, PBO by CSCV, fold t-test |
| `qsCrypto/portfolio/mae.py` | The implementation: `Excursion`, `mae_star`, `win_prob_curve`, `resolvability` |
| `qsCrypto/notebooks/Turtle_IBKR_CME_Bitcoin.ipynb` | Backtest, MAE calibration (§8.5) |
| `qsCrypto/notebooks/Turtle_IBKR_Live_MAE.ipynb` | Live IBKR trade management with MAE in R |
