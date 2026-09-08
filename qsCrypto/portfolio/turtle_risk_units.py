#!/usr/bin/env python3
"""
Turtle Trading Risk Unit Calculator for Crypto Perpetuals (Binance)
===================================================================

Adapts the Original Turtle Trading Rules position sizing to cryptocurrency
perpetual futures on Binance with configurable leverage.

Turtle Position Sizing (adapted for crypto perps):
  N  = 20-day EMA of True Range  (same formula as original Turtle N)
  Unit_Notional = (RISK_PER_UNIT × Account_Equity × Price) / N
  
  This ensures 1 unit moving 1N causes P&L = 1% of account equity.

  Max_Position = Unit_Notional × 4 (Turtle max per market)
  Required_Margin = Max_Position / Leverage

Turtle Risk Limits (adapted):
  - Single market:           max 4 units
  - Closely correlated:      max 6 units
  - Loosely correlated:      max 10 units
  - Single direction total:  max 12 units

Stop: STOP_N × N from entry. With STOP_N = 1.0 (the constant below) that is 1N and
      1% account risk per unit — R = 1N, which is the unit every MAE/MFE figure in
      this codebase is read in. The canonical Turtle rules used 2N; the constant
      below is the authority, not this comment.
Unit Adding: +1 unit at each ½N interval; raise all stops to 1N from newest unit.
      Aggregate risk to the common stop: 1.0N with one unit, 1.5N with two and
      three, back to 1.0N at full load (see qsCrypto/portfolio/mae_test.py).
MAE: the empirical inner barrier that replaces guessing the stop width lives in
      qsCrypto/portfolio/mae.py.

Usage:
  python turtle_risk_units.py                     # defaults: $100k, 20x
  python turtle_risk_units.py --account 50000     # $50k account
  python turtle_risk_units.py --leverage 10       # 10x leverage
  python turtle_risk_units.py --output report.json

Dependencies:
  pip install requests

References:
  - Curtis Faith, "The Original Turtle Trading Rules" (2003)
  - Binance FAPI: https://developers.binance.com/docs/derivatives/usds-margined-futures
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    print("ERROR: 'requests' required. Install: pip install requests")
    sys.exit(1)

# Fuente única de datos: caché en disco, descarte de la vela en formación y
# cobertura de TRADIFI_PERPETUAL además de cripto.
try:
    from .data import BinanceFetcher, DataProcessor
except ImportError:                       # ejecutado como script, no como paquete
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.data import BinanceFetcher, DataProcessor

_FETCHER: BinanceFetcher | None = None
_PROCESSOR = DataProcessor()


def _fetcher(session=None) -> BinanceFetcher:
    """Fetcher compartido: una sesión HTTP y un exchangeInfo por ejecución."""
    global _FETCHER
    if _FETCHER is None:
        _FETCHER = BinanceFetcher(session=session)
    return _FETCHER


def _klines_start() -> str:
    """Fecha desde la que descargar para tener `CANDLE_LIMIT` velas con holgura."""
    return (datetime.now(timezone.utc)
            - timedelta(days=CANDLE_LIMIT * 3)).strftime("%Y-%m-%d")

# ─────────────────────────── Defaults ─────────────────────────────────

DEFAULT_ACCOUNT = 10_000.0
DEFAULT_LEVERAGE = 10
RISK_PER_UNIT = 0.01            # 1% of equity per unit (Turtle standard)
ATR_PERIOD = 20                 # 20-day EMA of True Range
MAX_UNITS = 4                   # Turtle: max 4 units per market
STOP_N = 1.0                    # 1N stop → 1% risk per unit; R = STOP_N x N
CANDLE_LIMIT = ATR_PERIOD + 30  # Extra bars for EMA warm-up

# El endpoint vive en src/data/fetcher.py; aquí no se hacen llamadas HTTP.

TOKENS =[
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
    "RAY",
    "TAO",
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
    "NVDA",
    "QQQ",
    "SPY",
    "EWY",
    "EWJ",
    "EWT",
        # may not exist on Binance perps
]

# Tokens que se quieren vigilar pero aún no cotizan como perpetuo USDT.
# NO se listan a mano los que sí existen: la lista real se calcula en main()
# contra exchangeInfo. Mantenerla fija ya se quedó obsoleta una vez —decía que
# XAU y XAG no existían cuando sí cotizan como TRADIFI_PERPETUAL— y eso oculta
# activos operables detrás de un dato caducado.
WATCHLIST_SIN_PERP = ["RIVER", "ASTER"]

CORRELATION_GROUPS = {
    "layer1_major":  {"BTC", "ETH"},
    "layer1_alt":    {"SOL", "AVAX", "NEAR", "APT", "SUI", "DOT", "ADA", "HBAR"},
    "meme":          {"DOGE", "PNUT", "WLD"},
    "defi":          {"AAVE", "LINK"},
    "legacy":        {"LTC", "BCH", "XLM", "XTZ"},
    "exchange":      {"BNB"},
}

# ─────────────────────────── Data ─────────────────────────────────────

@dataclass
class Candle:
    ts: int; o: float; h: float; l: float; c: float; v: float

@dataclass
class TurtleUnit:
    symbol: str
    price: float
    atr_20: float          # N in USD
    n_pct: float           # N / price
    unit_notional: float   # 1 unit USD
    unit_qty: float        # 1 unit tokens
    max_pos_usd: float     # 4 units USD
    max_pos_qty: float     # 4 units tokens
    margin: float          # max_pos / leverage
    margin_pct: float      # margin / account
    stop_usd: float        # 2N USD
    stop_pct: float        # 2N %
    risk_usd: float        # risk per unit at 2N
    half_n: float          # ½N add interval
    group: str
    qty_prec: int = 3

# ─────────────────────────── Binance ──────────────────────────────────

def get_klines(s, token: str) -> list[Candle]:
    """
    Últimas `CANDLE_LIMIT` velas diarias **cerradas**.

    Va por src.data.BinanceFetcher: caché en disco y, sobre todo, descarte de la
    vela en formación. Antes la última fila era la vela de hoy sin cerrar, así que
    N se calculaba con un rango incompleto y el sizing salía de un ATR falseado.
    """
    f = _fetcher(s)

    # Binance sirve velas de contratos deslistados: RAYUSDT (SETTLING desde 2022)
    # devuelve precio congelado y volumen 0, lo que da N=0 y un tamaño de unidad
    # infinito por división por cero. No se puede operar: fuera.
    if f.native_symbol(token) not in f.available_symbols():
        raise ValueError(f"{f.native_symbol(token)}: contrato no operable (no TRADING)")

    df = f.fetch(token, start=_klines_start(), freq="1d")

    # N se calcula sobre las sesiones del mercado SUBYACENTE. Los perps TradFi
    # cotizan 24/7 pero la bolsa cierra el fin de semana: esas barras son finas
    # (rango ~0.31 del laborable) y diluyen el ATR un 26% de media, lo que infla
    # el tamaño de posición en la misma proporción. Al quitarlas, el TR del lunes
    # se mide contra el cierre del viernes, así que el gap del finde no se pierde.
    # En cripto los siete días son sesión real y no se filtra nada.
    df = _PROCESSOR.to_sessions(df, calendar=f.calendar_of(token))

    df = df.tail(CANDLE_LIMIT)
    return [Candle(int(ts.timestamp() * 1000), r.open, r.high, r.low, r.close, r.volume)
            for ts, r in df.iterrows()]

def get_sym_info(s) -> dict:
    """Precisiones de cantidad y precio por símbolo."""
    return {sym["symbol"]: {"qp": sym.get("quantityPrecision", 3),
                            "pp": sym.get("pricePrecision", 2)}
            for sym in _fetcher(s).exchange_info().get("symbols", [])}

# ─────────────────────────── ATR / N ──────────────────────────────────

def calc_n(candles: list[Candle]) -> float:
    """
    N = 20-day EMA of True Range.
    Formula: N = (19 × PDN + TR) / 20. Seed: SMA of first 20 TRs.
    """
    tr = [candles[0].h - candles[0].l]
    for i in range(1, len(candles)):
        pdc = candles[i-1].c
        tr.append(max(candles[i].h - candles[i].l,
                      abs(candles[i].h - pdc),
                      abs(pdc - candles[i].l)))
    if len(tr) < ATR_PERIOD:
        raise ValueError(f"Need {ATR_PERIOD}+ candles")
    n = sum(tr[:ATR_PERIOD]) / ATR_PERIOD
    for t in tr[ATR_PERIOD:]:
        n = ((ATR_PERIOD - 1) * n + t) / ATR_PERIOD
    # N es el denominador del sizing (Unit = 1%·equity·precio / N). Un N nulo da
    # tamaño infinito, no "sin riesgo": pasa con contratos parados, cuyo rango
    # alto-bajo es cero en todas las velas.
    if not n > 0:
        raise ValueError(f"N degenerado ({n}): rango nulo en {len(tr)} velas, "
                         f"¿contrato sin actividad?")
    return n

# ─────────────────────────── Unit Calc ────────────────────────────────

def corr_group(sym: str) -> str:
    for name, members in CORRELATION_GROUPS.items():
        if sym in members: return name
    return "ungrouped"

def calc_unit(sym: str, candles: list[Candle], acct: float, lev: int, info: dict) -> TurtleUnit:
    n = calc_n(candles)
    price = candles[-1].c
    n_pct = n / price
    si = info.get(f"{sym}USDT", {})
    qp = si.get("qp", 3)

    # Turtle unit: 1% of equity / (N/Price) = 1% × equity × price / N
    unit_not = (RISK_PER_UNIT * acct * price) / n
    unit_qty = unit_not / price

    max_not = unit_not * MAX_UNITS
    max_qty = unit_qty * MAX_UNITS
    margin = max_not / lev

    return TurtleUnit(
        symbol=sym, price=round(price, si.get("pp", 2)),
        atr_20=round(n, 4), n_pct=round(n_pct, 6),
        unit_notional=round(unit_not, 2), unit_qty=round(unit_qty, qp),
        max_pos_usd=round(max_not, 2), max_pos_qty=round(max_qty, qp),
        margin=round(margin, 2), margin_pct=round(margin / acct * 100, 2),
        stop_usd=round(STOP_N * n, 4), stop_pct=round(STOP_N * n_pct * 100, 2),
        risk_usd=round(RISK_PER_UNIT * acct * STOP_N, 2),
        half_n=round(n / 2, 4), group=corr_group(sym), qty_prec=qp,
    )

# ─────────────────────────── Portfolio ────────────────────────────────

def check_portfolio(units: list[TurtleUnit], acct: float) -> dict:
    total_u = sum(u.max_pos_usd for u in units) / (units[0].max_pos_usd / MAX_UNITS) if units else 0
    total_u = len(units) * MAX_UNITS
    total_m = sum(u.margin for u in units)
    by_g = {}
    for u in units:
        by_g[u.group] = by_g.get(u.group, 0) + MAX_UNITS
    warn = []
    for g, c in by_g.items():
        if c > 6: warn.append(f"Group '{g}': {c} units (limit: 6)")
    if total_u > 12:
        warn.append(f"Total {total_u} units > direction limit (12). Select top trending markets.")
    if total_m > acct:
        warn.append(f"Total margin ${total_m:,.0f} > account ${acct:,.0f}")
    return {"total_units": total_u, "total_margin": round(total_m, 2),
            "margin_pct": round(total_m / acct * 100, 2),
            "by_group": by_g, "warnings": warn}

# ─────────────────────────── Display ──────────────────────────────────

def print_table(units: list[TurtleUnit], pf: dict, acct: float, lev: int,
                skipped: list[str] | None = None):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    w = 120
    print(f"\n{'='*w}")
    print(f"  TURTLE RISK UNIT SHEET — {ts}")
    print(f"  Account: ${acct:,.0f} | Leverage: {lev}x | Risk/Unit: {RISK_PER_UNIT*100:.0f}% of equity | N: {ATR_PERIOD}-day EMA(TR) | Stop: {STOP_N:.0f}N")
    print(f"{'='*w}")
    if skipped:
        print(f"  Sin perpetuo USDT vivo: {', '.join(skipped)}")

    hdr = (f"  {'Tkn':<5} {'Price':>10} {'N($)':>9} {'N%':>6} "
           f"{'1Unit$':>9} {'1UnitQty':>10} "
           f"{'MaxPos$':>10} {'Margin$':>9} {'M%':>5} "
           f"{'Stop%':>5} {'½N($)':>8} {'Group':<14}")
    print(f"\n{hdr}")
    print(f"  {'─'*115}")

    for u in units:
        print(f"  {u.symbol:<5} "
              f"{u.price:>10,.2f} "
              f"{u.atr_20:>9,.2f} "
              f"{u.n_pct*100:>5.2f}% "
              f"{u.unit_notional:>9,.0f} "
              f"{u.unit_qty:>10,.{min(u.qty_prec, 4)}f} "
              f"{u.max_pos_usd:>10,.0f} "
              f"{u.margin:>9,.0f} "
              f"{u.margin_pct:>4.1f}% "
              f"{u.stop_pct:>4.1f}% "
              f"{u.half_n:>8,.2f} "
              f"{u.group:<14}")

    print(f"\n{'='*w}")
    print(f"  PORTFOLIO SUMMARY (if ALL markets loaded to {MAX_UNITS} units)")
    print(f"  {'─'*60}")
    print(f"  Total margin: ${pf['total_margin']:>10,.0f}  ({pf['margin_pct']:.1f}% of account)")
    print(f"  Total units:  {pf['total_units']:>10}")
    print(f"\n  Correlation groups:")
    for g, c in sorted(pf["by_group"].items(), key=lambda x: -x[1]):
        flag = " ⚠>6" if c > 6 else ""
        print(f"    {g:<18} {c:>3} units{flag}")
    if pf["warnings"]:
        print(f"\n  ⚠ WARNINGS:")
        for msg in pf["warnings"]: print(f"    • {msg}")

    print(f"\n  TURTLE RULES QUICK REFERENCE:")
    print(f"    Entry:    1 unit on breakout (20d or 55d Donchian channel)")
    print(f"    Add:      +1 unit each ½N from last fill (max {MAX_UNITS})")
    print(f"    Stop:     2N from entry; raise all stops to 2N from newest unit")
    print(f"    Exit:     10d low (Sys1) or 20d low (Sys2) for longs")
    print(f"    Drawdown: Account −10% → reduce notional by 20%")
    print(f"    Limits:   4/mkt, 6/correlated, 10/loose, 12/direction")
    print(f"{'='*w}\n")

# ─────────────────────────── Main ─────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Turtle Risk Units — Crypto Perps")
    p.add_argument("--account", type=float, default=DEFAULT_ACCOUNT)
    p.add_argument("--leverage", type=int, default=DEFAULT_LEVERAGE)
    p.add_argument("--output", type=str, default="turtle_risk_units.json")
    a = p.parse_args()

    print(f"\n  Connecting to Binance FAPI...")
    s = requests.Session()
    s.headers["User-Agent"] = "TurtleRiskCalc/1.0"

    try:
        info = get_sym_info(s)
    except Exception as e:
        print(f"  ERROR: {e}"); sys.exit(1)

    # Qué tokens no tienen perpetuo vivo — se calcula contra exchangeInfo, no a
    # mano: la lista fija llegó a afirmar que XAU y XAG no existían.
    _f = _fetcher(s)
    _vivos = _f.available_symbols()
    skipped = sorted({t for t in sorted(TOKENS) if _f.native_symbol(t) not in _vivos}
                     | set(WATCHLIST_SIN_PERP))

    units, errs = [], []
    for tok in sorted(TOKENS):
        try:
            cd = get_klines(s, tok)
            u = calc_unit(tok, cd, a.account, a.leverage, info)
            units.append(u)
            print(f"    ✓ {tok:<5} ${u.price:>10,.2f}  N={u.atr_20:>8,.2f} ({u.n_pct*100:.2f}%)")
        except Exception as e:
            errs.append(f"{tok}: {e}")
            print(f"    ✗ {tok}: {e}")
        time.sleep(0.08)

    if not units:
        print("  No data. Exiting."); sys.exit(1)

    units.sort(key=lambda u: u.symbol, reverse=False)
    pf = check_portfolio(units, a.account)
    print_table(units, pf, a.account, a.leverage, skipped=skipped)

    # JSON export
    export = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "config": {"account": a.account, "leverage": a.leverage,
                   "risk_per_unit_pct": RISK_PER_UNIT * 100,
                   "atr_period": ATR_PERIOD, "stop_n": STOP_N, "max_units": MAX_UNITS},
        "tokens": [
            {"symbol": u.symbol, "price": u.price, "atr_20": u.atr_20,
             "n_pct": round(u.n_pct * 100, 4),
             "unit_notional": u.unit_notional, "unit_qty": u.unit_qty,
             "max_pos_usd": u.max_pos_usd, "max_pos_qty": u.max_pos_qty,
             "margin": u.margin, "margin_pct": u.margin_pct,
             "stop_2n_usd": u.stop_usd, "stop_2n_pct": u.stop_pct,
             "half_n_usd": u.half_n, "risk_per_unit_usd": u.risk_usd,
             "correlation_group": u.group}
            for u in units
        ],
        "portfolio": pf,
        "skipped": skipped, "errors": errs,
    }
    with open(a.output, "w") as f:
        json.dump(export, f, indent=2, default=str)
    print(f"  JSON → {a.output}")

if __name__ == "__main__":
    main()
