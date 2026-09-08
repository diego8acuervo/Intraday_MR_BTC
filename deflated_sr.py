# -*- coding: utf-8 -*-
"""Deflated Sharpe Ratio y estadística asociada.

Responde a una sola pregunta: **¿sobrevive este Sharpe al hecho de que se
probaron muchas configuraciones antes de quedarse con ésta?**

Probando N configuraciones sin ninguna habilidad, el mejor Sharpe de la rejilla
es alto **por construcción**. El DSR compara el Sharpe seleccionado contra
`SR*`, el máximo que cabría esperar bajo la hipótesis nula de que ninguna
configuración tiene edge, y devuelve la probabilidad de que el Sharpe verdadero
supere ese listón.

Referencias:
  Bailey & López de Prado (2012), "The Sharpe Ratio Efficient Frontier" — PSR, MinTRL
  Bailey & López de Prado (2014), "The Deflated Sharpe Ratio"

Todos los Sharpe de este módulo son **por periodo** (sin anualizar). La
anualización es una convención de presentación y solo aparece en
`annualized_sharpe_ratio`; introducirla en el resto de las fórmulas rompería la
relación entre el Sharpe y el tamaño muestral, que es de lo que vive el DSR.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Union

import numpy as np
import pandas as pd
from scipy import stats

__all__ = [
    "EULER_MASCHERONI", "DSRResult",
    "estimated_sharpe_ratio", "annualized_sharpe_ratio", "sharpe_ratio_stdev",
    "probabilistic_sharpe_ratio", "min_track_record_length",
    "num_independent_trials", "expected_maximum_sr", "deflated_sharpe_ratio",
]

EULER_MASCHERONI = 0.5772156649015328606065120900824024310421

Returns = Union[pd.Series, pd.DataFrame, np.ndarray, list]


@dataclass(frozen=True)
class DSRResult:
    """Resultado del DSR. `dsr` es una probabilidad, no un Sharpe."""
    dsr: float                  # P(SR verdadero > SR*), en [0, 1]
    sr: float                   # Sharpe observado de la serie seleccionada, por periodo
    sr_std: float               # error típico del Sharpe observado
    sr_star: float              # listón: E[max SR] bajo la nula
    psr: float                  # PSR contra el benchmark 0 (sin deflactar)
    n_obs: int                  # observaciones de la serie seleccionada
    n_trials_total: int         # configuraciones probadas
    n_trials_effective: int     # configuraciones INDEPENDIENTES equivalentes
    trials_sr_std: float        # dispersión de los Sharpe de la rejilla
    skew: float
    kurtosis: float             # no centrada (normal = 3)

    def __str__(self) -> str:
        return (f"DSR={self.dsr:.4f} | SR={self.sr:.4f} vs SR*={self.sr_star:.4f} "
                f"| n={self.n_obs} | ensayos {self.n_trials_total} "
                f"({self.n_trials_effective} efectivos)")


def _as_frame_or_series(x: Returns):
    if isinstance(x, (pd.Series, pd.DataFrame)):
        return x
    arr = np.asarray(x, dtype=float)
    return pd.Series(arr) if arr.ndim == 1 else pd.DataFrame(arr)


def estimated_sharpe_ratio(returns: Returns):
    """SR por periodo = media / desviación típica muestral (ddof=1).

    Con un DataFrame devuelve una Series con el SR de cada columna, que es la
    forma en que se alimenta la rejilla de ensayos.
    """
    r = _as_frame_or_series(returns)
    return r.mean() / r.std(ddof=1)


def annualized_sharpe_ratio(sr: float, periods_per_year: int = 252) -> float:
    """Escalado de presentación: SR_anual = SR_periodo · √periodos.

    No se usa en ningún cálculo del DSR. El factor √T cancela en el numerador y
    el denominador del PSR solo si se aplica de forma consistente, y mezclarlo
    con el tamaño muestral es la forma más común de inflar el resultado.
    """
    return float(sr) * math.sqrt(periods_per_year)


def sharpe_ratio_stdev(n: int, skew: float, kurtosis: float, sr: float) -> float:
    """Error típico del Sharpe estimado, con corrección por NO normalidad.

        σ(SR) = √( (1 − γ₃·SR + ((γ₄−1)/4)·SR²) / (n−1) )

    `kurtosis` es la NO centrada (3.0 en la normal). Con γ₃ = 0 y γ₄ = 3 se
    reduce al caso gaussiano √((1 + SR²/2)/(n−1)).

    El signo del término de sesgo importa: con cola izquierda gorda (γ₃ < 0) y
    SR positivo el error típico CRECE, así que la misma estrategia necesita más
    track record para justificar el mismo Sharpe.
    """
    if n < 2:
        raise ValueError(f"n debe ser >= 2, recibido {n}")
    var = (1.0 - skew * sr + ((kurtosis - 1.0) / 4.0) * sr ** 2) / (n - 1)
    if var <= 0:
        raise ValueError("varianza del SR no positiva: revisa skew/kurtosis/sr")
    return math.sqrt(var)


def probabilistic_sharpe_ratio(sr: float, sr_std: float,
                               sr_benchmark: float = 0.0) -> float:
    """PSR = Φ((SR − SR_benchmark) / σ(SR)).

    Probabilidad de que el Sharpe verdadero supere el benchmark. Con
    `sr_benchmark = SR*` esto ES el Deflated Sharpe Ratio.
    """
    if sr_std <= 0:
        raise ValueError(f"sr_std debe ser > 0, recibido {sr_std}")
    return float(stats.norm.cdf((sr - sr_benchmark) / sr_std))


def min_track_record_length(sr: float, sr_std: float, n: int,
                            sr_benchmark: float = 0.0, prob: float = 0.95) -> float:
    """Observaciones necesarias para que el SR sea distinguible del benchmark
    con confianza `prob`:

        MinTRL = 1 + σ(SR)²·(n−1)·( Z_prob / (SR − SR_benchmark) )²

    Si sale mayor que `n`, el track record todavía no alcanza para afirmar nada.
    """
    if sr <= sr_benchmark:
        return float("inf")     # sin exceso sobre el benchmark no hay longitud que valga
    z = stats.norm.ppf(prob)
    return float(1 + (sr_std ** 2 * (n - 1)) * (z / (sr - sr_benchmark)) ** 2)


def num_independent_trials(trials: Optional[pd.DataFrame] = None, *,
                           m: Optional[int] = None,
                           rho: Optional[float] = None) -> int:
    """Número EFECTIVO de ensayos independientes:

        N_eff = M·(1 − ρ̄) + ρ̄

    con ρ̄ la correlación media fuera de la diagonal entre los ensayos. Con
    ρ̄ = 1 (todas las configuraciones son la misma estrategia disfrazada) hay un
    solo ensayo; con ρ̄ = 0 cuentan las M.

    Es la corrección que evita castigar una rejilla de 200 combinaciones que en
    realidad explora dos ideas. Se puede llamar con un DataFrame de retornos por
    ensayo, o directamente con `m` y `rho`.
    """
    if trials is not None:
        df = trials if isinstance(trials, pd.DataFrame) else pd.DataFrame(trials)
        m = df.shape[1]
        if m < 2:
            return 1
        corr = df.corr().to_numpy(dtype=float)
        off = corr[~np.eye(m, dtype=bool)]
        off = off[np.isfinite(off)]
        rho = float(off.mean()) if off.size else 0.0
    if m is None or rho is None:
        raise ValueError("pasa un DataFrame de ensayos, o bien `m` y `rho`")
    rho = float(np.clip(rho, 0.0, 1.0))
    n_eff = m * (1.0 - rho) + rho
    return int(np.clip(round(n_eff), 1, m))


def expected_maximum_sr(independent_trials: int, trials_sr_std: float,
                        expected_mean_sr: float = 0.0) -> float:
    """E[max SR] entre `independent_trials` ensayos SIN edge (López de Prado):

        E[max] = μ + σ·[ (1−γ)·Z⁻¹(1 − 1/N) + γ·Z⁻¹(1 − 1/(N·e)) ]

    con γ la constante de Euler-Mascheroni. Es el listón que hay que superar:
    cuantas más configuraciones se prueben, más alto es el mejor Sharpe por puro
    azar, y más tiene que valer el candidato para significar algo.
    """
    n = int(independent_trials)
    if n < 1:
        raise ValueError(f"independent_trials debe ser >= 1, recibido {n}")
    if n == 1:
        return float(expected_mean_sr)
    z = stats.norm.ppf
    maxz = ((1 - EULER_MASCHERONI) * z(1 - 1.0 / n)
            + EULER_MASCHERONI * z(1 - 1.0 / (n * math.e)))
    return float(expected_mean_sr + trials_sr_std * maxz)


def deflated_sharpe_ratio(selected_returns: Returns, trials_returns: pd.DataFrame,
                          expected_mean_sr: float = 0.0) -> DSRResult:
    """DSR de la serie seleccionada, deflactado por la rejilla que la produjo.

    `selected_returns` : retornos por periodo de la configuración elegida.
    `trials_returns`   : un retorno por columna y configuración PROBADA — todas,
                         incluidas las que se descartaron. El sesgo de selección
                         no distingue entre rejilla formal y prueba y error.

    El listón `SR*` se calcula bajo la nula E[SR] = `expected_mean_sr` (cero por
    defecto, que es la formulación de Bailey & López de Prado) y la dispersión
    OBSERVADA de los Sharpe de la rejilla.
    """
    sel = _as_frame_or_series(selected_returns)
    if isinstance(sel, pd.DataFrame):
        if sel.shape[1] != 1:
            raise ValueError("selected_returns debe ser una sola serie")
        sel = sel.iloc[:, 0]
    sel = sel.dropna().astype(float)

    trials = (trials_returns if isinstance(trials_returns, pd.DataFrame)
              else pd.DataFrame(trials_returns))
    if trials.shape[1] < 2:
        raise ValueError(
            f"se necesitan al menos 2 ensayos para deflactar; recibidos {trials.shape[1]}. "
            "Con un solo ensayo no hay sesgo de selección que corregir y el DSR "
            "no está definido — usa el PSR.")
    n_obs = int(len(sel))
    if n_obs < 3:
        raise ValueError(
            f"la serie seleccionada tiene {n_obs} observaciones; hacen falta al "
            "menos 3 para estimar sesgo y curtosis")

    sr = float(estimated_sharpe_ratio(sel))
    g3 = float(stats.skew(sel))
    g4 = float(stats.kurtosis(sel, fisher=False))
    sr_std = sharpe_ratio_stdev(n=n_obs, skew=g3, kurtosis=g4, sr=sr)

    trials_sr = estimated_sharpe_ratio(trials).replace([np.inf, -np.inf], np.nan).dropna()
    trials_sr_std = float(trials_sr.std(ddof=1)) if len(trials_sr) > 1 else 0.0
    n_eff = num_independent_trials(trials)
    sr_star = expected_maximum_sr(n_eff, trials_sr_std, expected_mean_sr)

    return DSRResult(
        dsr=probabilistic_sharpe_ratio(sr=sr, sr_std=sr_std, sr_benchmark=sr_star),
        sr=sr, sr_std=sr_std, sr_star=sr_star,
        psr=probabilistic_sharpe_ratio(sr=sr, sr_std=sr_std, sr_benchmark=0.0),
        n_obs=n_obs, n_trials_total=int(trials.shape[1]), n_trials_effective=int(n_eff),
        trials_sr_std=trials_sr_std, skew=g3, kurtosis=g4,
    )
