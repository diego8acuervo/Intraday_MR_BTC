"""
Auditoría de sesgos de un backtest: look-ahead, survivorship, concentración de
P&L, diversificación ilusoria, sensibilidad a costes y Monte Carlo.

Cada función devuelve NÚMEROS, no descargos genéricos. Un backtest sin estas
cinco medidas no está terminado.
"""
from __future__ import annotations

from typing import Callable, Mapping, Sequence

import numpy as np
import pandas as pd

__all__ = [
    "assert_no_lookahead", "check_shift_discipline", "survivorship_split",
    "listing_inventory", "pnl_concentration", "worst_periods", "effective_bets",
    "cost_sensitivity", "mc_bootstrap_trades", "equity_metrics",
]


# ───────────────────────────── 1. look-ahead ────────────────────────────────

def assert_no_lookahead(feature_fn: Callable[[pd.DataFrame], pd.Series],
                        df: pd.DataFrame, n_checks: int = 25,
                        min_bars: int = 100, rtol: float = 1e-8,
                        raise_on_fail: bool = True) -> pd.DataFrame:
    """TEST DE TRUNCAMIENTO — la única prueba que cierra el asunto.

    Recalcula la feature sobre el histórico truncado en t y compara con el valor
    que tenía en la serie completa. Si difieren, la feature ve el futuro.

    Detecta lo que un `.shift(1)` visual no detecta: normalizaciones sobre toda
    la muestra, `bfill`, medias centradas, ajustes de escala globales, etc.

    >>> assert_no_lookahead(lambda d: d["high"].rolling(20).max().shift(1), df)
    """
    full = pd.Series(feature_fn(df)).astype(float)
    idx = full.dropna().index
    if len(idx) <= min_bars:
        raise ValueError("serie demasiado corta para el test de truncamiento")
    positions = np.linspace(min_bars, len(idx) - 1, num=min(n_checks, len(idx) - min_bars))
    rows = []
    for p in positions.astype(int):
        t = idx[p]
        trunc = pd.Series(feature_fn(df.loc[:t])).astype(float)
        if t not in trunc.index:
            rows.append({"t": t, "full": full[t], "truncado": np.nan, "ok": False})
            continue
        a, b = float(full[t]), float(trunc[t])
        ok = (np.isnan(a) and np.isnan(b)) or np.isclose(a, b, rtol=rtol, equal_nan=True)
        rows.append({"t": t, "full": a, "truncado": b, "ok": bool(ok)})
    out = pd.DataFrame(rows)
    n_bad = int((~out.ok).sum())
    if n_bad and raise_on_fail:
        raise AssertionError(
            f"LOOK-AHEAD DETECTADO: {n_bad}/{len(out)} puntos cambian al truncar "
            f"el histórico. La feature usa información posterior a la barra.\n"
            f"{out[~out.ok].head(5).to_string(index=False)}")
    return out


def check_shift_discipline(df: pd.DataFrame, decision_cols: Sequence[str],
                           price_col: str = "close") -> pd.DataFrame:
    """Heurística rápida: correlación de cada columna de decisión con el retorno
    CONTEMPORÁNEO. Una correlación anormalmente alta con el retorno del propio
    día es la firma típica de una feature sin shift. No sustituye al test de
    truncamiento; sirve para priorizar qué auditar."""
    fwd = df[price_col].pct_change(fill_method=None)
    rows = [{"columna": c,
             "corr_contemporanea": float(df[c].corr(fwd)),
             "corr_rezagada": float(df[c].shift(1).corr(fwd))}
            for c in decision_cols if c in df]
    out = pd.DataFrame(rows)
    if len(out):
        out["sospechosa"] = out.corr_contemporanea.abs() > out.corr_rezagada.abs() * 2 + 0.1
    return out


# ──────────────────────────── 2. survivorship ───────────────────────────────

def listing_inventory(data: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Inventario por símbolo: primera barra, última, nº de barras, años.
    La fecha de listado es la clave del test de survivorship — y también revela
    contratos muertos (última barra muy anterior al final de la muestra)."""
    end = max(d.index[-1] for d in data.values())
    inv = pd.DataFrame([
        {"symbol": s, "inicio": d.index[0], "fin": d.index[-1], "barras": len(d),
         "años": round((d.index[-1] - d.index[0]).days / 365.25, 2),
         "muerto": bool(d.index[-1] < end - pd.Timedelta(days=30)),
         "precio_congelado": bool(d["close"].tail(60).std() == 0) if "close" in d else False}
        for s, d in data.items()
    ]).sort_values("inicio").reset_index(drop=True)
    return inv


def survivorship_split(data: Mapping[str, pd.DataFrame], run_fn: Callable,
                       veteran_window_days: int = 180) -> dict:
    """Test de listing-date: los símbolos listados tarde son, por construcción,
    los que 'llegaron' tras sobrevivir. Corre la estrategia sobre los veteranos
    (vivos al inicio de la muestra) y sobre el universo completo.

    `run_fn(subset_dict) -> (equity_curve, trades_df)`.
    La diferencia de CAGR es una COTA INFERIOR del sesgo: no recupera a los que
    ni siquiera están en la base de datos.
    """
    first = min(d.index[0] for d in data.values())
    cut = first + pd.Timedelta(days=veteran_window_days)
    veterans = [s for s, d in data.items() if d.index[0] <= cut]
    newcomers = [s for s in data if s not in veterans]
    if len(veterans) < 3:
        return {"error": f"solo {len(veterans)} veteranos; muestra insuficiente",
                "veteranos": veterans, "recientes": newcomers}
    ec_v, tr_v = run_fn({s: data[s] for s in veterans})
    ec_all, tr_all = run_fn(dict(data))
    m_v, m_all = equity_metrics(ec_v, tr_v), equity_metrics(ec_all, tr_all)
    gap = m_all.get("CAGR", np.nan) - m_v.get("CAGR", np.nan)
    return {"corte_veteranos": cut.date(), "n_veteranos": len(veterans),
            "n_recientes": len(newcomers), "recientes": newcomers,
            "metricas_veteranos": m_v, "metricas_universo": m_all,
            "gap_cagr": float(gap),
            "veredicto": ("gran parte del edge es survivorship" if gap > 0.03
                          else "sesgo de survivorship acotado")}


# ────────────────────── 3. concentración de resultado ───────────────────────

def pnl_concentration(trades: pd.DataFrame, pnl_col: str = "pnl",
                      tops: Sequence[int] = (1, 3, 5, 10)) -> dict:
    """Si 3 trades hacen el 80% del P&L, tu track record tiene tamaño muestral
    3, no N. Mide también la cola negativa: dónde está el riesgo de ruina."""
    tr = trades.sort_values(pnl_col, ascending=False).reset_index(drop=True)
    total = float(tr[pnl_col].sum())
    if total == 0 or len(tr) == 0:
        return {"error": "sin trades o P&L nulo"}
    share_top = {f"top_{k}": float(tr[pnl_col].head(k).sum() / total)
                 for k in tops if len(tr) >= k}
    share_bot = {f"bottom_{k}": float(tr[pnl_col].tail(k).sum() / total)
                 for k in tops if len(tr) >= k}
    return {"n_trades": len(tr), "pnl_total": total,
            **share_top, **share_bot,
            "pnl_sin_mejor": float(total - tr[pnl_col].iloc[0]),
            "pnl_sin_peor": float(total - tr[pnl_col].iloc[-1]),
            "mejores": tr.head(5), "peores": tr.tail(5).iloc[::-1],
            "veredicto": ("resultado concentrado: muestra efectiva ~3 eventos"
                          if share_top.get("top_3", 0) > 0.5 else "concentración aceptable")}


def worst_periods(trades: pd.DataFrame, closed_col: str = "closed",
                  pnl_col: str = "pnl", freq: str = "ME", n: int = 10) -> pd.DataFrame:
    """Peores periodos por P&L cerrado — donde se agrupan los stops."""
    tr = trades.copy()
    tr[closed_col] = pd.to_datetime(tr[closed_col])
    return (tr.resample(freq, on=closed_col)
              .agg(pnl=(pnl_col, "sum"), trades=(pnl_col, "size"))
              .sort_values("pnl").head(n))


# ───────────────────── 4. diversificación ilusoria ──────────────────────────

def effective_bets(data: Mapping[str, pd.DataFrame], price_col: str = "close",
                   min_obs: int = 120, min_overlap: int = 60) -> dict:
    """Nº EFECTIVO de apuestas independientes vía autovalores de la matriz de
    correlación: n_eff = (Σλ)² / Σλ².

    Decisiones deliberadas, todas aprendidas a base de que reventara:
      - Retornos sobre el índice PROPIO de cada símbolo (nada de inner join: el
        activo más joven trunca a todos, y con calendarios heterogéneos la
        intersección puede quedar VACÍA).
      - Correlación PAR A PAR con `min_periods` — un NaN aquí significa "mercado
        cerrado", no "dato perdido".
      - Se descartan columnas con varianza nula (contrato muerto con precio
        congelado): std=0 ⇒ correlación indefinida ⇒ columna de NaN ⇒
        `eigvalsh` revienta con "eigenvalues did not converge" (LAPACK no valida
        su entrada; un NaN no da error de dominio legible).
      - Poda iterativa del símbolo con más pares sin estimar, en vez de un
        `dropna(how='any')` que se lleva por delante a los sanos.
      - Proyección a la matriz PSD más cercana (clip de autovalores negativos +
        rediagonalización a 1): la matriz par a par no es semidefinida positiva
        porque cada celda ve una muestra distinta. Sin esto, descartar los
        autovalores negativos quita masa de la traza e INFLA el % de PC1.
      - Aviso si T/N < 10: matriz mal condicionada ⇒ n_eff sesgado AL ALZA.
    """
    rets = pd.DataFrame({s: d[price_col].pct_change(fill_method=None)
                         for s, d in data.items()}).replace([np.inf, -np.inf], np.nan)
    n0 = rets.shape[1]
    thin = rets.columns[rets.count() < min_obs].tolist()
    rets = rets.drop(columns=thin)
    flat = rets.columns[rets.std(skipna=True).fillna(0) == 0].tolist()
    rets = rets.drop(columns=flat)
    if rets.shape[1] < 3:
        return {"error": "menos de 3 símbolos con muestra suficiente",
                "descartados_muestra": thin, "descartados_var_nula": flat}

    corr = rets.corr(min_periods=min_overlap)
    dropped = []
    while len(corr) > 2 and corr.isna().any().any():
        worst = corr.isna().sum().idxmax()
        corr = corr.drop(index=worst, columns=worst)
        dropped.append(worst)
    if len(corr) < 3:
        return {"error": f"solo {len(corr)} activos con solapamiento >= {min_overlap}",
                "descartados_sin_solape": dropped}

    C = (corr.values + corr.values.T) / 2
    ev_raw = np.linalg.eigvalsh(C)
    psd_fix = bool(ev_raw.min() < -1e-10)
    if psd_fix:
        w, V = np.linalg.eigh(C)
        C = V @ np.diag(np.clip(w, 0, None)) @ V.T
        d = np.sqrt(np.diag(C)); C = C / np.outer(d, d)
        np.fill_diagonal(C, 1.0)
    ev = np.linalg.eigvalsh(C)[::-1]
    ev = ev[ev > 1e-12]
    n_eff = float((ev.sum() ** 2) / (ev ** 2).sum())
    off = C[np.triu_indices_from(C, 1)]
    t_over_n = rets.shape[0] / len(corr)
    return {"n_simbolos_inicial": n0, "n_simbolos_final": len(corr),
            "descartados_muestra": thin, "descartados_var_nula": flat,
            "descartados_sin_solape": dropped, "psd_corregida": psd_fix,
            "obs_por_activo": float(t_over_n),
            "corr_media": float(off.mean()), "corr_mediana": float(np.median(off)),
            "pct_pares_gt_070": float((off > 0.70).mean()),
            "pc1_varianza": float(ev[0] / ev.sum()),
            "n_efectivo": n_eff,
            "matriz": pd.DataFrame(C, index=corr.index, columns=corr.columns),
            "aviso": ("T/N < 10: matriz mal condicionada, n_eff sesgado AL ALZA"
                      if t_over_n < 10 else ""),
            "veredicto": (f"crees tener {len(corr)} mercados; tienes ~{n_eff:.0f}")}


# ──────────────────────── 5. costes y Monte Carlo ───────────────────────────

DEFAULT_COST_SCENARIOS = [
    # (etiqueta, fee, slippage, funding_diario)
    ("Sin costes", 0.0,    0.0,    0.0),
    ("Optimista",  0.0002, 0.0005, 0.0001),
    ("Base",       0.0005, 0.0010, 0.0003),
    ("Pesimista",  0.0005, 0.0025, 0.0005),
    ("Estrés",     0.0010, 0.0050, 0.0010),
]


def cost_sensitivity(run_fn: Callable, scenarios=DEFAULT_COST_SCENARIOS) -> pd.DataFrame:
    """Barre escenarios de fricción. `run_fn(fee, slip, funding) -> (ec, trades)`.

    Si el edge muere al doblar el slippage, no era un edge. Y cobra el funding
    SOLO a los instrumentos que lo pagan de verdad (perpetuos): en futuros
    listados o índices cash es un coste inventado que escala con el NOCIONAL
    mientras el sizing escala con el RIESGO — en instrumentos de baja vol drena
    la cuenta hasta dejarla en negativo.
    """
    rows = []
    for label, fee, slip, fund in scenarios:
        ec, tr = run_fn(fee, slip, fund)
        m = equity_metrics(ec, tr)
        rows.append({"escenario": label, "fee_bps": fee * 1e4, "slip_bps": slip * 1e4,
                     "funding_pct_anual": round(fund * 365 * 100, 1),
                     "CAGR": m.get("CAGR", np.nan), "Sharpe": m.get("Sharpe", np.nan),
                     "MaxDD": m.get("Max DD", np.nan)})
    df = pd.DataFrame(rows)
    df.attrs["friccion_cagr"] = float(df.CAGR.iloc[0] - df.CAGR.iloc[2]) if len(df) > 2 else np.nan
    df.attrs["sobrevive_pesimista"] = bool(df.loc[df.escenario == "Pesimista", "CAGR"].gt(0).all())
    return df


def mc_bootstrap_trades(trades: pd.DataFrame, pnl_col: str = "pnl",
                        equity0: float = 10_000, n_sims: int = 5000,
                        bust_dd: float = -0.50, seed: int = 7) -> dict:
    """Bootstrap CON REEMPLAZO de los P&L de los trades.

    Permutar el orden NO sirve: la suma es invariante y el equity final sale
    idéntico en todas las simulaciones. Solo el remuestreo con reemplazo genera
    una distribución real de resultados finales y de drawdowns.

    La curva que viste es UNA realización de un proceso estocástico. Lo que
    importa es la distribución, no el camino que tocó.
    """
    pnl = pd.Series(trades[pnl_col]).dropna().values
    if len(pnl) < 10:
        return {"error": f"solo {len(pnl)} trades; muestra insuficiente"}
    rng = np.random.default_rng(seed)
    finals = np.empty(n_sims); dds = np.empty(n_sims)
    for i in range(n_sims):
        eq = equity0 + np.cumsum(rng.choice(pnl, size=len(pnl), replace=True))
        path = np.concatenate([[equity0], eq])
        peak = np.maximum.accumulate(path)
        dds[i] = ((path - peak) / peak).min()
        finals[i] = eq[-1]
    return {"n_sims": n_sims, "equity0": equity0,
            "percentiles_equity": {f"p{q}": float(np.percentile(finals, q))
                                   for q in (5, 25, 50, 75, 95)},
            "percentiles_dd": {f"p{q}": float(np.percentile(dds, q))
                               for q in (5, 25, 50, 75, 95)},
            "p_perdida": float((finals < equity0).mean()),
            "p_ruina": float((dds <= bust_dd).mean()),
            "peor_caso": float(finals.min()), "peor_dd": float(dds.min()),
            "finals": finals, "dds": dds}


# ──────────────────────────── métricas comunes ──────────────────────────────

def equity_metrics(ec: pd.Series, trades: pd.DataFrame | None = None,
                   freq: int = 365) -> dict:
    """Métricas de una curva de equity. `freq`=365 para cripto (24/7), 252 para
    TradFi — usar el calendario equivocado desescala la volatilidad."""
    ec = pd.Series(ec).dropna()
    if len(ec) < 2:
        return {}
    r = ec.pct_change().dropna()
    yrs = (ec.index[-1] - ec.index[0]).days / 365.25
    dd = ec / ec.cummax() - 1
    vol = float(r.std() * np.sqrt(freq))
    dn = float(r[r < 0].std() * np.sqrt(freq))
    cagr = float((ec.iloc[-1] / ec.iloc[0]) ** (1 / yrs) - 1) if yrs > 0 else 0.0
    m = {"Retorno total": float(ec.iloc[-1] / ec.iloc[0] - 1), "CAGR": cagr,
         "Volatilidad": vol, "Sharpe": cagr / vol if vol > 0 else 0.0,
         "Sortino": cagr / dn if dn > 0 else 0.0, "Max DD": float(dd.min()),
         "Calmar": cagr / abs(dd.min()) if dd.min() < 0 else 0.0,
         "Días en DD>20%": int((dd < -0.20).sum()), "Años": float(yrs)}
    if trades is not None and len(trades):
        w = trades[trades.pnl > 0]; l = trades[trades.pnl <= 0]
        m |= {"Trades": len(trades), "Win rate": len(w) / len(trades),
              "Profit factor": float(w.pnl.sum() / abs(l.pnl.sum()))
                               if len(l) and l.pnl.sum() else np.inf,
              "Payoff (W/L)": float(w.pnl.mean() / abs(l.pnl.mean()))
                              if len(l) and len(w) else np.nan}
    return m
