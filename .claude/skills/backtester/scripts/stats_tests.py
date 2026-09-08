"""
Estadística de validación: ¿este Sharpe sobrevive al hecho de que probaste
muchas configuraciones antes de quedarte con esta?

Referencias:
  Bailey & López de Prado (2012) "The Sharpe Ratio Efficient Frontier" — PSR, MinTRL
  Bailey & López de Prado (2014) "The Deflated Sharpe Ratio"
  Bailey, Borwein, López de Prado & Zhu (2017) "The Probability of Backtest
      Overfitting" — CSCV
"""
from __future__ import annotations

from itertools import combinations

import numpy as np
import pandas as pd
from scipy import stats

__all__ = [
    "sharpe_ratio", "probabilistic_sharpe_ratio", "deflated_sharpe_ratio",
    "min_track_record_length", "expected_max_sharpe", "pbo_cscv",
    "fold_ttest",
]

_EULER = 0.5772156649015329


def sharpe_ratio(returns: pd.Series, freq: int = 252, rf: float = 0.0) -> float:
    r = pd.Series(returns).dropna()
    if len(r) < 2 or r.std(ddof=1) == 0:
        return 0.0
    return float((r.mean() - rf / freq) / r.std(ddof=1) * np.sqrt(freq))


def probabilistic_sharpe_ratio(returns: pd.Series, sr_benchmark: float = 0.0,
                               freq: int = 252) -> float:
    """PSR: P(SR_verdadero > sr_benchmark) dado el SR observado, la longitud de
    la muestra y la NO NORMALIDAD de los retornos.

    El sesgo y la curtosis importan: una estrategia con cola izquierda gorda
    (vender volatilidad, trend-following invertido) necesita mucho más track
    record para justificar el mismo Sharpe.
    """
    r = pd.Series(returns).dropna()
    n = len(r)
    if n < 3:
        return float("nan")
    sr = sharpe_ratio(r, freq=freq) / np.sqrt(freq)      # SR por periodo
    sr_b = sr_benchmark / np.sqrt(freq)
    g3, g4 = float(stats.skew(r)), float(stats.kurtosis(r, fisher=False))
    denom = np.sqrt(max(1e-12, 1 - g3 * sr + (g4 - 1) / 4.0 * sr ** 2))
    return float(stats.norm.cdf((sr - sr_b) * np.sqrt(n - 1) / denom))


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """E[max SR] bajo la hipótesis nula de que ninguna configuración tiene edge.
    Es el listón que hay que superar: probando 200 rejillas, el mejor Sharpe es
    alto POR CONSTRUCCIÓN, aunque todas sean ruido."""
    if n_trials < 2:
        return 0.0
    z1 = stats.norm.ppf(1 - 1.0 / n_trials)
    z2 = stats.norm.ppf(1 - 1.0 / (n_trials * np.e))
    return float(np.sqrt(sr_variance) * ((1 - _EULER) * z1 + _EULER * z2))


def deflated_sharpe_ratio(returns: pd.Series, n_trials: int,
                          sr_variance: float | None = None,
                          trial_sharpes: np.ndarray | None = None,
                          freq: int = 252) -> dict:
    """Deflated Sharpe Ratio: PSR contra el benchmark E[max SR] de `n_trials`
    pruebas. DSR > 0.95 ⇒ el Sharpe sobrevive al ajuste por pruebas múltiples.

    `n_trials` debe incluir TODAS las configuraciones evaluadas, también las
    que probaste a mano y descartaste. `sr_variance` es la varianza de los
    Sharpe anualizados de la rejilla (pásala, o pasa `trial_sharpes`).
    """
    if sr_variance is None:
        if trial_sharpes is None:
            raise ValueError("da sr_variance o trial_sharpes")
        sr_variance = float(np.var(np.asarray(trial_sharpes, dtype=float), ddof=1))
    sr0 = expected_max_sharpe(n_trials, sr_variance)
    dsr = probabilistic_sharpe_ratio(returns, sr_benchmark=sr0, freq=freq)
    return {"sharpe": sharpe_ratio(returns, freq=freq), "sr_benchmark": sr0,
            "dsr": dsr, "n_trials": n_trials,
            "veredicto": "sobrevive" if dsr > 0.95 else "NO distinguible de ruido"}


def min_track_record_length(returns: pd.Series, sr_benchmark: float = 0.0,
                            confidence: float = 0.95, freq: int = 252) -> float:
    """Nº mínimo de observaciones para que el SR observado sea significativo.
    Si supera la longitud real de tu muestra, no tienes evidencia todavía."""
    r = pd.Series(returns).dropna()
    sr = sharpe_ratio(r, freq=freq) / np.sqrt(freq)
    sr_b = sr_benchmark / np.sqrt(freq)
    if sr <= sr_b:
        return float("inf")
    g3, g4 = float(stats.skew(r)), float(stats.kurtosis(r, fisher=False))
    z = stats.norm.ppf(confidence)
    return float(1 + (1 - g3 * sr + (g4 - 1) / 4.0 * sr ** 2) * (z / (sr - sr_b)) ** 2)


def pbo_cscv(perf: pd.DataFrame, n_partitions: int = 16, freq: int = 252) -> dict:
    """Probability of Backtest Overfitting por CSCV.

    `perf`: DataFrame (T x N) de retornos por periodo — una columna por
    configuración probada. Se parte el eje temporal en `n_partitions` bloques y,
    para cada combinación de la mitad como IS, se mira dónde queda OOS la
    configuración ganadora IS.

    PBO = P(la ganadora IS quede por debajo de la MEDIANA OOS).
    PBO > 0.5 ⇒ tu proceso de selección es peor que elegir al azar.
    """
    perf = perf.dropna(axis=1, how="all").dropna()
    T, N = perf.shape
    if N < 2 or T < n_partitions * 2:
        raise ValueError(f"muestra insuficiente para CSCV (T={T}, N={N})")
    if n_partitions % 2:
        n_partitions -= 1
    blocks = np.array_split(np.arange(T), n_partitions)
    half = n_partitions // 2
    logits, ranks = [], []
    for combo in combinations(range(n_partitions), half):
        is_idx = np.concatenate([blocks[b] for b in combo])
        oos_idx = np.concatenate([blocks[b] for b in range(n_partitions)
                                  if b not in combo])
        sr_is = perf.iloc[is_idx].apply(lambda c: sharpe_ratio(c, freq=freq))
        sr_oos = perf.iloc[oos_idx].apply(lambda c: sharpe_ratio(c, freq=freq))
        best = sr_is.idxmax()
        rank = sr_oos.rank(pct=True)[best]          # 1.0 = la mejor OOS
        rank = min(max(rank, 1.0 / (N + 1)), N / (N + 1.0))
        ranks.append(float(rank))
        logits.append(float(np.log(rank / (1 - rank))))
    logits = np.array(logits)
    return {"pbo": float((logits <= 0).mean()),
            "n_combinations": len(logits),
            "median_oos_rank": float(np.median(ranks)),
            "logits": logits,
            "veredicto": "selección con valor" if (logits <= 0).mean() < 0.5
                          else "SOBREAJUSTE: la selección no generaliza"}


def fold_ttest(oos: pd.Series | np.ndarray, baseline: pd.Series | np.ndarray) -> dict:
    """t-test pareado de retornos OOS por fold contra el baseline en la MISMA
    ventana. Con pocos folds (n<20) es diagnóstico cualitativo, no prueba."""
    d = np.asarray(oos, dtype=float) - np.asarray(baseline, dtype=float)
    t, p = stats.ttest_1samp(d, 0.0)
    return {"n_folds": int(len(d)), "mean_diff": float(d.mean()),
            "t": float(t), "p": float(p),
            "wins": int((d > 0).sum()), "win_rate": float((d > 0).mean()),
            "veredicto": ("diferencia significativa (ojo con n pequeño)" if p < 0.05
                          else "NO significativa: la optimización es ruido"),
            "aviso_muestra": "n<20 folds: diagnóstico cualitativo, no evidencia"
                             if len(d) < 20 else ""}
