"""
session_backtest.py
===================

Backtest de rentabilidad para estrategias de UNA operación por sesión con
salida por reloj: entra al cierre de la barra de muestreo (volumen, dólar,
desequilibrio o tiempo) y cierra a una hora fija.

Por qué no se reutiliza `engine.py`. Aquel motor está construido sobre una
serie de barras REGULARES: detecta el fin de sesión por el hueco de tiempo
contra el paso mediano, ejecuta al open de la barra siguiente y lleva la
escalera de adds Turtle. Con barras de volumen o de dólar el paso mediano no
significa nada —una barra dura 3 minutos y la siguiente 40— así que esa
detección deja de ser fiable, y la escalera de adds no forma parte de esta
estrategia. Forzarlo habría exigido tocar el camino que sostiene dos backtests
ya cerrados y sus fixtures doradas. Lo que sí se reutiliza es todo lo que
importa para que las cifras sean comparables: la regla de tamaño con
reducción por drawdown (`_sizing_equity`), el funding histórico real, y las
métricas de `skill_stats`.

Lo que mide, y que la tasa de acierto NO mide: R por operación neto de
comisiones, deslizamiento y funding; curva de equity; Sharpe; máximo
drawdown; concentración del P&L; y la excursión adversa dentro de la
operación. Una estrategia puede acertar el signo el 55 % de las veces y
perder dinero si los aciertos son velas planas y los fallos no.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from qsCrypto.backtest.engine import _sizing_equity

TRADE_COLS = [
    "session", "side", "entry_time", "exit_time", "entry", "exit", "qty",
    "n", "notional", "hours", "pnl", "r_multiple", "gross_r", "fees",
    "slip_paid", "funding_paid", "mae_n", "mfe_n", "flat_minutes", "balance",
]


@dataclass
class SessionParams:
    """Parámetros del backtest.

    `stop_n` fija la UNIDAD DE RIESGO (1R = stop_n*N): dimensiona la posición
    y normaliza la R. NO hay stop real — la única salida es el reloj, igual
    que en `Intraday_Momentum_OR_VWAP.ipynb`. Eso deja la excursión adversa
    sin truncar, que es lo que permite leer `mae_n` como riesgo realmente
    soportado y no como el sitio donde se puso una barrera.
    """
    stop_n: float = 1.0
    risk_per_trade: float = 0.01
    equity0: float = 10_000.0
    fee_bps: float = 0.0002        # 2 pb taker por lado
    slip_bps: float = 0.0001       # 1 pb por lado
    max_gross_lev: float = 4.0
    use_dd_rule: bool = True
    dd_from_peak: bool = True
    allow_short: bool = True


def _exit_minute(entry_times, tz, hhmm, in_utc):
    """Marca del minuto CUYO CIERRE liquida la posición.

    `hhmm` nombra el minuto directamente, no "la hora a la que quiero estar
    plano". Una vela de 1 minuto con marca `t` cubre [t, t+1min), así que el
    close del minuto `hhmm` es el precio en `hhmm+1min`:

        exit_minute="15:59" (ET)  -> plano a las 16:00 ET, cierre del NYSE
        exit_minute="23:59" (UTC) -> plano a las 00:00 UTC, fin del día UTC

    Nombrar el minuto evita la ambigüedad de "cerrar a las 16:00", que puede
    querer decir el open de esa vela o su close, y que son dos precios con un
    minuto de diferencia.

    El día se toma de la hora de la SEÑAL convertida al huso de salida, no de
    la medianoche UTC: normalizar en UTC y luego convertir a Nueva York cae
    en el día ANTERIOR (00:00 UTC son las 19:00 o 20:00 del día previo en
    ET), y la salida quedaría antes que la entrada.
    """
    h, m = (int(x) for x in hhmm.split(":"))
    ent = pd.DatetimeIndex(entry_times)
    if in_utc:
        return ent.normalize() + pd.Timedelta(hours=h, minutes=m)
    loc = ent.tz_localize("UTC").tz_convert(tz)
    target = loc.normalize() + pd.Timedelta(hours=h, minutes=m)
    return pd.DatetimeIndex(target.tz_convert("UTC").tz_localize(None))


def run_session_backtest(signals, m1, funding=None, params=None,
                         exit_minute="15:59", exit_tz="America/New_York",
                         exit_in_utc=False):
    """Una operación por sesión, entrada al cierre de la barra, salida por reloj.

    `signals` : DataFrame con una fila por sesión — `session`, `end` (fin de
        la barra de muestreo), `side` (+1/-1/0), `n` (unidad de volatilidad).
        Es la salida de `score()` del cuaderno.
    `m1`      : velas de 1 minuto (UTC naive) con open/high/low/close.
    `funding` : Series de tasas de funding con su marca de tiempo. Se cobra
        en CADA liquidación que cae dentro de la posición.
    `exit_minute` : minuto cuyo CIERRE liquida la posición — "15:59" en
        `exit_tz` deja plano a las 16:00 ET; "23:59" con `exit_in_utc=True`
        deja plano a las 00:00 UTC.

    **Ejecución.** La señal se conoce al cerrar la barra de muestreo, es decir
    al final del minuto `end`. El fill se hace al OPEN del minuto siguiente:
    llenarlo sobre el propio minuto `end` sería look-ahead de un minuto,
    porque su apertura ya pasó cuando se conoce su cierre.

    Devuelve `(equity, trades)`. `equity` está indexada por fecha de sesión,
    así que sus retornos son diarios y el Sharpe se anualiza con 252.
    """
    p = params or SessionParams()
    sig = signals[(signals["side"] != 0) & signals["n"].notna()].copy()
    sig = sig.sort_values("end", kind="stable").reset_index(drop=True)
    if not len(sig):
        return pd.Series(dtype=float), pd.DataFrame(columns=TRADE_COLS)

    o = m1["open"].to_numpy(float); h = m1["high"].to_numpy(float)
    lo = m1["low"].to_numpy(float); c = m1["close"].to_numpy(float)
    midx = m1.index
    exit_marks = _exit_minute(sig["end"], exit_tz, exit_minute, exit_in_utc)

    fnd = funding if funding is not None else pd.Series(dtype=float)
    f_idx = fnd.index.to_numpy() if len(fnd) else np.array([], dtype="datetime64[ns]")
    f_val = fnd.to_numpy(float) if len(fnd) else np.array([])

    balance, peak = p.equity0, p.equity0
    rows = []
    entry_pos = midx.searchsorted(pd.DatetimeIndex(sig["end"]).to_numpy(),
                                  side="right")   # minuto SIGUIENTE al cierre
    # El minuto exacto nombrado por `exit_minute`; si faltara en la serie se
    # toma el último anterior, nunca uno posterior.
    exit_pos = midx.searchsorted(exit_marks.to_numpy(), side="right") - 1

    for k in range(len(sig)):
        i, j = int(entry_pos[k]), int(exit_pos[k])
        if i >= len(midx) or j < i:
            continue
        side = int(sig["side"].iloc[k])
        n = float(sig["n"].iloc[k])
        if not np.isfinite(n) or n <= 0:
            continue

        ref = max(peak, balance) if p.dd_from_peak else None
        eq = _sizing_equity(balance, p.equity0, p.use_dd_rule, ref=ref)
        entry_px = o[i] * (1 + side * p.slip_bps)
        qty = p.risk_per_trade * eq / (p.stop_n * n)
        if entry_px > 0 and qty * entry_px > p.max_gross_lev * balance:
            qty = (p.max_gross_lev * balance) / entry_px
        if qty <= 0:
            continue

        exit_px = c[j] * (1 - side * p.slip_bps)
        fee = abs(qty * entry_px) * p.fee_bps + abs(qty * exit_px) * p.fee_bps

        # Funding: cada liquidación dentro de (entrada, salida].
        fpay = 0.0
        if len(f_idx):
            a = np.searchsorted(f_idx, midx[i].to_datetime64(), side="right")
            b = np.searchsorted(f_idx, midx[j].to_datetime64(), side="right")
            for t in range(a, b):
                fpay += -side * qty * c[j] * f_val[t]

        gross = side * qty * (exit_px - entry_px)
        pnl = gross - fee + fpay
        balance += pnl
        peak = max(peak, balance)

        seg_h, seg_l = h[i:j + 1], lo[i:j + 1]
        adverse = seg_l.min() if side > 0 else seg_h.max()
        favorable = seg_h.max() if side > 0 else seg_l.min()
        r_unit = p.stop_n * n * qty
        rows.append({
            "session": sig["session"].iloc[k], "side": side,
            "entry_time": midx[i], "exit_time": midx[j],
            "entry": entry_px, "exit": exit_px, "qty": qty, "n": n,
            "notional": qty * entry_px,
            "hours": (midx[j] - midx[i]).total_seconds() / 3600.0,
            "pnl": pnl,
            "r_multiple": pnl / r_unit if r_unit > 0 else np.nan,
            "gross_r": gross / r_unit if r_unit > 0 else np.nan,
            "fees": fee, "slip_paid": abs(qty * (entry_px - o[i]))
            + abs(qty * (exit_px - c[j])),
            "funding_paid": fpay,
            "mae_n": side * (entry_px - adverse) / n,
            "mfe_n": side * (favorable - entry_px) / n,
            "flat_minutes": int((seg_h <= seg_l).sum()),
            "balance": balance,
        })

    tr = pd.DataFrame(rows, columns=TRADE_COLS)
    if not len(tr):
        return pd.Series(dtype=float), tr
    ec = pd.Series(tr["balance"].to_numpy(),
                   index=pd.DatetimeIndex(tr["session"]), name="equity")
    ec = ec[~ec.index.duplicated(keep="last")]
    return ec, tr


def _ratio(num, denom, min_denom=1.0):
    """Cociente sobre un bruto que puede ser ~0. `min_denom` en unidades de R
    acumuladas: por debajo de 1R de bruto total el cociente no es informativo."""
    return num / abs(denom) if abs(denom) >= min_denom else np.nan


def summarise_backtest(ec, tr, label, freq=252):
    """Métricas de rentabilidad y riesgo. `freq=252` porque la curva es
    diaria (una operación por sesión), no de barras de 30 minutos: el Sharpe
    de aquí NO es comparable con el de los cuadernos 24/7."""
    from qsCrypto.backtest.skill_stats.bias_audit import equity_metrics
    if not len(tr):
        return {"esquema": label, "trades": 0}
    m = equity_metrics(ec, tr, freq=freq)
    r = tr["r_multiple"]
    wins, losses = r[r > 0], r[r < 0]
    # Comisiones y funding expresados en R, para poder compararlos con el
    # bruto sin mezclar dinero con unidades de riesgo.
    r_unit = tr["n"] * tr["qty"]          # stop_n va dentro de gross_r
    fee_r = tr["fees"] / r_unit
    fund_r = tr["funding_paid"] / r_unit
    t_stat = (r.mean() / (r.std() / np.sqrt(len(r)))) if r.std() > 0 else np.nan
    return {
        "esquema": label, "trades": len(tr),
        "R neto medio": r.mean(), "R neto mediano": r.median(),
        "R bruto medio": tr["gross_r"].mean(),
        "t-stat(R)": t_stat,
        "win %": 100 * (tr["pnl"] > 0).mean(),
        "payoff": wins.mean() / abs(losses.mean()) if len(losses) else np.nan,
        "profit factor": m.get("Profit factor", np.nan),
        "Retorno total": m["Retorno total"], "CAGR": m["CAGR"],
        "Sharpe": m["Sharpe"], "Sortino": m["Sortino"],
        "Max DD": m["Max DD"], "Calmar": m["Calmar"],
        "horas exp.": tr["hours"].median(),
        "MAE mediana (N)": tr["mae_n"].median(),
        "MFE mediana (N)": tr["mfe_n"].median(),
        # Fricción como fracción del bruto, TODO en unidades de R: es la
        # cifra que mató a los dos sistemas anteriores (56 % y 46 %).
        # Si el bruto suma ~0 el cociente se dispara y deja de significar
        # nada (no es que el coste sea el 1.150 % de algo, es que no hay
        # algo): se devuelve NaN en vez de un número que invita a leerse.
        "coste/|bruto|": _ratio(fee_r.sum(), tr["gross_r"].sum()),
        "funding/|bruto|": _ratio(abs(fund_r.sum()), tr["gross_r"].sum()),
    }
