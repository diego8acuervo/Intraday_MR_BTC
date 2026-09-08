"""
Binance Perpetuals Signal Scanner
==================================
Indicators:
  1. Donchian Breakout (20-day)
       +1  if today's close > max(high[-21:-1])   [bullish breakout]
       -1  if today's close < min(low[-21:-1])    [bearish breakout]
        0  otherwise
  2. Momentum (180-day window, excluding last 30 days)
       Price change = close[-31] / close[-181] - 1
       +1  if positive
       -1  if negative
        0  if zero (edge case)

  Combined signal:
       +1  if Donchian == +1 AND Momentum == +1
       -1  if Donchian == -1 AND Momentum == -1
        0  otherwise
"""

import pandas as pd
from datetime import datetime, timedelta, timezone
import time
import sys

# ── Config ────────────────────────────────────────────────────────────────────
# El endpoint vive en src/data/fetcher.py; aquí no se hacen llamadas HTTP.
INTERVAL   = "1d"
LIMIT      = 210          # fetch extra to guarantee 181 clean candles after dropna
DONCHIAN_N = 55
MOM_START  = 20           # index from end (oldest anchor)
MOM_END    = 2            # index from end (recent anchor, excludes last 30 days)

SYMBOLS = [
     "AAVE",
    "AIXBT",
    "AVAX",
    "BCH",
    "BNB",
    "BTC",
    "COMP",
    "DOGE",
    "DOT",
    "DYDX",
    "EIGEN",
    "ENA",
    "ETH",
    "ETHFI",
    "FORM",
    "INJ",
    "JUP",
    "LTC",
    "NEAR",
    "PNUT",
    "SOL",
    "SUI",
    "TRX",
    "UNI",
    "XAU",
    "XAG",
    "BZ",
    "XPT",
    "XPD",
    "CL",
    "NATGAS",
    "COPPER",
    "INTC",
    "MSTR",
    "COIN",
    "HOOD",
    "AMZN",
    "PLTR",
    "CRCL",
    "LLY",
    "NVO",
    "BBX",
    "NOK",
    "ASTS",
    "SKHYNIX",
    "SAMSUNG",
    "HYUNDAI",
    "DELL",
    "IBM",
    "NOW",
    "CRM",
    "IREN",
    "ONDS",
    "GOOGL",
    "AAPL",
    "META",
    "MSFT",
    "TSLA",
    "STRC",
    "QQQ",
    "SPY",
    "EWY",
    "EWJ",
    "EWT"
        # may not exist on Binance perps
]

# ── Datos ─────────────────────────────────────────────────────────────────────
# Fuente única: src.data.BinanceFetcher. Aporta caché parquet, descarte de la
# vela en formación (la de hoy está incompleta: usarla es look-ahead) y cobertura
# de TRADIFI_PERPETUAL, que es donde viven XAU, CL, AAPL, SPY y compañía.
try:
    from .data import BinanceFetcher
except ImportError:                       # ejecutado como script, no como paquete
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.data import BinanceFetcher

OHLCV = ["open", "high", "low", "close", "volume"]

# Historia mínima que necesitan los indicadores, con margen para el warm-up.
HISTORY_DAYS = max(LIMIT, DONCHIAN_N, MOM_START) * 3

_FETCHER: BinanceFetcher | None = None


def get_fetcher(session=None) -> BinanceFetcher:
    """Fetcher compartido: reutiliza sesión HTTP, exchangeInfo y caché en disco."""
    global _FETCHER
    if _FETCHER is None:
        _FETCHER = BinanceFetcher(session=session)
    return _FETCHER


def fetch_klines(symbol: str, *, fetcher: BinanceFetcher | None = None
                 ) -> pd.DataFrame | None:
    """
    Últimas `LIMIT` velas diarias cerradas, indexadas por fecha.

    None si el símbolo no existe o no hay historia suficiente para el momentum.
    """
    f = fetcher or get_fetcher()

    # Binance sigue sirviendo velas de contratos deslistados: RAYUSDT está en
    # estado SETTLING desde 2022 y devuelve 220 velas con volumen 0 y el precio
    # congelado en 0.248. Sin este filtro el scanner emite señal sobre un
    # contrato muerto que no se puede operar.
    if f.native_symbol(symbol) not in f.available_symbols():
        return None

    start = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    try:
        df = f.fetch(symbol, start=start, freq=INTERVAL)
    except Exception:
        return None
    if len(df) < MOM_START:
        return None
    return df.tail(LIMIT)


def fetch_history(symbol: str, start: str = "2021-01-01", session=None,
                  fetcher: BinanceFetcher | None = None) -> pd.DataFrame:
    """Histórico diario completo desde `start`, indexado por fecha."""
    return (fetcher or get_fetcher(session)).fetch(symbol, start=start, freq=INTERVAL)


def donchian_signal(df: pd.DataFrame) -> int:
    """
    +1 if today's close broke above the 20-day high (prior window, excluding today).
    -1 if today's close broke below the 20-day low.
     0 otherwise.
    """
    if len(df) < DONCHIAN_N + 1:
        return 0
    today_close  = df["close"].iloc[-1]
    prior_window = df.iloc[-(DONCHIAN_N + 1):-1]   # last 20 candles before today
    dc_top = prior_window["high"].max()
    dc_bot = prior_window["low"].min()

    if today_close > dc_top:
        return  1
    elif today_close < dc_bot:
        return -1
    return 0


def momentum_signal(df: pd.DataFrame) -> int:
    """
    Momentum = close[-31] / close[-181] - 1  (excludes last 30 days).
    +1 if positive, -1 if negative, 0 if zero.
    Requires at least 181 rows.
    """
    if len(df) < MOM_START:
        return 0
    price_now  = df["close"].iloc[-MOM_END]    # 30 days ago
    price_then = df["close"].iloc[-MOM_START]  # ~150 days before that
    momentum   = price_now / price_then - 1

    if momentum > 0:
        return  1
    elif momentum < 0:
        return -1
    return 0


def combined_signal(dc: int, mom: int) -> int:
    if dc == 1 and mom == 1:
        return  1
    if dc == -1 and mom == -1:
        return -1
    return 0


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*70}")
    print(f"  Binance Perps Signal Scanner  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Donchian(55-20d) + Momentum(180d ex last 30d)")
    print(f"{'='*70}\n")

    rows = []
    errors = []

    for i, sym in enumerate(SYMBOLS):
        df = fetch_klines(sym)
        if df is None:
            errors.append(sym)
            print(f"  [{i+1:>2}/{len(SYMBOLS)}]  {sym:<10}  NOT FOUND / INSUFFICIENT DATA", flush=True)
            time.sleep(0.05)
            continue

        dc  = donchian_signal(df)
        mom = momentum_signal(df)
        sig = combined_signal(dc, mom)

        # Price levels for context
        close_today  = df["close"].iloc[-1]
        prior        = df.iloc[-(DONCHIAN_N + 1):-1]
        dc_top       = prior["high"].max()
        dc_bot       = prior["low"].min()
        mom_pct      = (df["close"].iloc[-MOM_END] / df["close"].iloc[-MOM_START] - 1) * 100

        rows.append({
            "symbol":    sym,
            "close":     close_today,
            "dc_top":    dc_top,
            "dc_bot":    dc_bot,
            "mom_pct":   mom_pct,
            "donchian":  dc,
            "momentum":  mom,
            "signal":    sig,
            "n_rows":    len(df),
        })

        sig_char = {1: "▲", -1: "▼", 0: "─"}
        print(
            f"  [{i+1:>2}/{len(SYMBOLS)}]  {sym:<10}"
            f"  close={close_today:>12.4f}"
            f"  DC={sig_char[dc]}"
            f"  MOM={mom_pct:>+7.1f}%{sig_char[mom]}"
            f"  SIG={sig:>+2}",
            flush=True
        )
        time.sleep(0.06)   # ~16 req/s — well within Binance rate limits

    # ── Summary table ─────────────────────────────────────────────────────────
    if not rows:
        print("\nNo data retrieved.")
        return

    results = pd.DataFrame(rows)

    print(f"\n{'='*70}")
    print("  FINAL SIGNALS SUMMARY")
    print(f"{'='*70}")

    # Sort: +1 first, then 0, then -1
    results["sort_key"] = results["signal"].map({1: 0, 0: 1, -1: 2})
    results = results.sort_values(["sort_key", "symbol"]).drop(columns="sort_key")

    # Print grouped
    for label, emoji, grp_sig in [("LONG (both +1)", "▲", 1), ("NEUTRAL", "─", 0), ("SHORT (both -1)", "▼", -1)]:
        grp = results[results["signal"] == grp_sig]
        print(f"\n  {emoji} {label}  ({len(grp)} tokens)")
        if grp.empty:
            print("     —")
        for _, row in grp.iterrows():
            print(
                f"     {row['symbol']:<10}"
                f"  close={row['close']:>12.4f}"
                f"  DC={'▲' if row['donchian']==1 else ('▼' if row['donchian']==-1 else '─')}"
                f"  MOM={row['mom_pct']:>+7.1f}%"
            )

    # ── Stats ─────────────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    n_valid = len(results)
    n_long  = (results["signal"] ==  1).sum()
    n_short = (results["signal"] == -1).sum()
    n_neut  = (results["signal"] ==  0).sum()
    n_err   = len(errors)

    print(f"  Scanned: {n_valid} tokens   |  Long: {n_long}  |  Short: {n_short}  |  Neutral: {n_neut}  |  Errors: {n_err}")
    if errors:
        print(f"  Not listed / insufficient data: {', '.join(errors)}")
    print(f"{'='*70}\n")

    # ── CSV export ────────────────────────────────────────────────────────────
    out_cols = ["symbol","close","dc_top","dc_bot","mom_pct","donchian","momentum","signal"]
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    csv_path = f"signals_{ts}.csv"
    results[out_cols].to_csv(csv_path, index=False, float_format="%.6f")
    print(f"  CSV saved → {csv_path}\n")


if __name__ == "__main__":
    main()
