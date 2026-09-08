"""
Validación cruzada para series financieras — purga, embargo y CPCV.

Referencia: Marcos López de Prado, "Advances in Financial Machine Learning"
(Wiley, 2018), caps. 4 (sample weights), 5 (fractional differentiation) y
7 (cross-validation in finance).

Convención central: `t1` es una `pd.Series` indexada por el INSTANTE EN QUE SE
ABRE la observación (t0) y cuyo VALOR es el instante en que su etiqueta queda
determinada (t1). Sin `t1` no se puede purgar: es lo que define el solape entre
train y test. En un backtest de trades, t0 = apertura y t1 = cierre.

numpy/pandas/scipy puros — sin dependencias de ningún repo.
"""
from __future__ import annotations

from itertools import combinations
from typing import Iterator, Sequence

import numpy as np
import pandas as pd

__all__ = [
    "get_train_times", "apply_embargo", "PurgedKFold", "CombinatorialPurgedCV",
    "walk_forward_folds", "num_co_events", "average_uniqueness",
    "sample_weights_by_uniqueness", "time_decay_weights",
    "frac_diff_ffd", "min_ffd_order", "triple_barrier_t1",
]


# ─────────────────────────── purga y embargo ────────────────────────────────

def get_train_times(t1: pd.Series, test_times: pd.Series) -> pd.Series:
    """Purga (AFML 7.1): elimina del train toda observación cuyo intervalo
    [t0, t1] se solape con cualquier intervalo de test.

    t1         : Series index=t0, values=t1 de TODAS las observaciones.
    test_times : Series index=inicio, values=fin de cada bloque de test.
    Devuelve el t1 de las observaciones que SÍ pueden entrenarse.
    """
    trn = t1.copy(deep=True)
    for start, end in test_times.items():
        # tres formas de solaparse con [start, end]
        idx0 = trn[(start <= trn.index) & (trn.index <= end)].index      # empieza dentro
        idx1 = trn[(start <= trn) & (trn <= end)].index                  # termina dentro
        idx2 = trn[(trn.index <= start) & (end <= trn)].index            # lo envuelve
        trn = trn.drop(idx0.union(idx1).union(idx2))
    return trn


def apply_embargo(t1: pd.Series, test_times: pd.Series,
                  embargo_pct: float = 0.01) -> pd.Series:
    """Embargo (AFML 7.2): además de purgar, descarta las observaciones que
    arrancan justo DESPUÉS de cada bloque de test.

    embargo_pct se expresa como fracción del número total de barras. Cubre la
    correlación serial residual que la purga no ve (la purga solo mira solapes
    de etiqueta; el embargo mira la vecindad temporal).
    """
    if embargo_pct <= 0:
        return get_train_times(t1, test_times)
    index = t1.index
    h = int(len(index) * embargo_pct)
    extended = test_times.copy()
    for start, end in test_times.items():
        pos = index.searchsorted(end, side="right")
        pos_emb = min(pos + h, len(index) - 1)
        extended[start] = index[pos_emb]          # alarga el bloque de test
    return get_train_times(t1, extended)


class PurgedKFold:
    """K-fold con test CONTIGUO, purga y embargo (AFML 7.3).

    No baraja. Compatible en forma con sklearn (`split(X)` → (train_idx, test_idx)
    posicionales), pero sin heredar de sklearn para no añadir dependencia.

    >>> cv = PurgedKFold(n_splits=5, t1=t1, embargo_pct=0.01)
    >>> for tr, te in cv.split(X): ...
    """

    def __init__(self, n_splits: int = 5, t1: pd.Series | None = None,
                 embargo_pct: float = 0.0):
        if t1 is None or not isinstance(t1, pd.Series):
            raise ValueError("t1 debe ser una pd.Series (index=t0, values=t1)")
        if not t1.index.is_monotonic_increasing:
            raise ValueError("t1 debe venir ordenada por t0")
        self.n_splits, self.t1, self.embargo_pct = n_splits, t1, embargo_pct

    def get_n_splits(self, X=None, y=None, groups=None) -> int:
        return self.n_splits

    def split(self, X: pd.DataFrame, y=None, groups=None
              ) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        if not X.index.equals(self.t1.index):
            raise ValueError("X y t1 deben compartir exactamente el mismo índice")
        indices = np.arange(X.shape[0])
        bounds = [(i[0], i[-1] + 1) for i in np.array_split(indices, self.n_splits)]
        for lo, hi in bounds:
            test_idx = indices[lo:hi]
            test_times = pd.Series(self.t1.iloc[test_idx].max(),
                                   index=[self.t1.index[lo]])
            train_t1 = apply_embargo(self.t1, test_times, self.embargo_pct)
            train_idx = self.t1.index.get_indexer(train_t1.index)
            yield train_idx[train_idx >= 0], test_idx


class CombinatorialPurgedCV:
    """CPCV (AFML 12): parte la muestra en N grupos y usa k de ellos como test
    en TODAS las combinaciones. Genera k·C(N,k)/N caminos OOS distintos en vez
    de uno solo.

    Su valor: te da una DISTRIBUCIÓN de Sharpe out-of-sample. Un walk-forward
    clásico produce un único camino, y con un único camino no puedes distinguir
    "la estrategia funciona" de "tuve suerte con el corte".
    """

    def __init__(self, n_splits: int = 6, n_test_splits: int = 2,
                 t1: pd.Series | None = None, embargo_pct: float = 0.01):
        if t1 is None:
            raise ValueError("t1 es obligatorio para purgar")
        if not 1 <= n_test_splits < n_splits:
            raise ValueError("1 <= n_test_splits < n_splits")
        self.n_splits, self.n_test_splits = n_splits, n_test_splits
        self.t1, self.embargo_pct = t1, embargo_pct

    @property
    def n_paths(self) -> int:
        from math import comb
        return self.n_test_splits * comb(self.n_splits, self.n_test_splits) // self.n_splits

    def get_n_splits(self, X=None, y=None, groups=None) -> int:
        from math import comb
        return comb(self.n_splits, self.n_test_splits)

    def split(self, X: pd.DataFrame, y=None, groups=None
              ) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        indices = np.arange(X.shape[0])
        groups_idx = np.array_split(indices, self.n_splits)
        for combo in combinations(range(self.n_splits), self.n_test_splits):
            test_idx = np.sort(np.concatenate([groups_idx[g] for g in combo]))
            test_times = pd.Series(
                {self.t1.index[groups_idx[g][0]]: self.t1.iloc[groups_idx[g]].max()
                 for g in combo}
            ).sort_index()
            train_t1 = apply_embargo(self.t1, test_times, self.embargo_pct)
            train_idx = self.t1.index.get_indexer(train_t1.index)
            yield train_idx[train_idx >= 0], test_idx


# ────────────────────────────── walk-forward ────────────────────────────────

def walk_forward_folds(index: pd.DatetimeIndex, is_days: int = 365,
                       oos_days: int = 90, embargo_days: int = 0,
                       anchored: bool = False) -> list[dict]:
    """Ventanas walk-forward ancladas al calendario, con embargo entre IS y OOS.

    - OOS NO se solapan (paso = oos_days) ⇒ el track record OOS es concatenable
      sin doble conteo.
    - `embargo_days` debe cubrir al menos el horizonte máximo de la señal
      (p.ej. el lookback del indicador más largo).
    - `anchored=True` expande el IS desde el origen en vez de rodarlo.

    Devuelve [{'fold', 'is_start', 'is_end', 'oos_start', 'oos_end'}, ...].
    """
    idx = pd.DatetimeIndex(sorted(index))
    t0, tN = idx[0], idx[-1]
    folds, cur = [], t0 + pd.Timedelta(days=is_days)
    emb = pd.Timedelta(days=embargo_days)
    while cur + emb + pd.Timedelta(days=oos_days) <= tN:
        is_start = t0 if anchored else cur - pd.Timedelta(days=is_days)
        folds.append({
            "fold": len(folds) + 1,
            "is_start": is_start, "is_end": cur,
            "oos_start": cur + emb,
            "oos_end": cur + emb + pd.Timedelta(days=oos_days),
        })
        cur += pd.Timedelta(days=oos_days)
    return folds


# ──────────────────── unicidad y pesos de muestra (AFML 4) ──────────────────

def num_co_events(bar_index: pd.DatetimeIndex, t1: pd.Series) -> pd.Series:
    """Nº de etiquetas concurrentes en cada barra. Base de la unicidad."""
    t1 = t1.fillna(bar_index[-1])
    counts = pd.Series(0, index=bar_index, dtype=float)
    for t0, t1_ in t1.items():
        counts.loc[t0:t1_] += 1
    return counts


def average_uniqueness(bar_index: pd.DatetimeIndex, t1: pd.Series) -> pd.Series:
    """Unicidad media de cada etiqueta: media de 1/concurrencia en su vida.

    Etiquetas muy solapadas ⇒ observaciones lejos de i.i.d. Entrenar sin
    corregir esto sobre-pondera los periodos con muchas señales simultáneas.
    """
    co = num_co_events(bar_index, t1).replace(0, np.nan)
    return pd.Series({t0: (1.0 / co.loc[t0:t1_]).mean() for t0, t1_ in t1.items()})


def sample_weights_by_uniqueness(bar_index: pd.DatetimeIndex, t1: pd.Series,
                                 returns: pd.Series | None = None) -> pd.Series:
    """Pesos de muestra por unicidad, opcionalmente escalados por |retorno|
    acumulado de la etiqueta (AFML 4.10: 'return attribution')."""
    u = average_uniqueness(bar_index, t1)
    if returns is None:
        w = u
    else:
        co = num_co_events(bar_index, t1).replace(0, np.nan)
        lr = np.log1p(returns.reindex(bar_index).fillna(0.0))
        w = pd.Series({t0: (lr.loc[t0:t1_] / co.loc[t0:t1_]).sum().__abs__()
                       for t0, t1_ in t1.items()})
    return w * len(w) / w.sum()


def time_decay_weights(uniqueness: pd.Series, last_weight: float = 0.5) -> pd.Series:
    """Decaimiento lineal por antigüedad (AFML 4.11). `last_weight`=peso de la
    observación MÁS ANTIGUA; 1.0 desactiva el decaimiento, valores negativos
    borran del todo la cola más vieja."""
    cum = uniqueness.sort_index().cumsum()
    if last_weight >= 0:
        slope = (1.0 - last_weight) / cum.iloc[-1]
    else:
        slope = 1.0 / ((last_weight + 1) * cum.iloc[-1])
    const = 1.0 - slope * cum.iloc[-1]
    w = const + slope * cum
    return w.clip(lower=0.0)


# ─────────────── estacionariedad con memoria (frac-diff, AFML 5) ────────────

def _ffd_weights(d: float, thres: float = 1e-5) -> np.ndarray:
    w, k = [1.0], 1
    while True:
        w_ = -w[-1] * (d - k + 1) / k
        if abs(w_) < thres:
            break
        w.append(w_); k += 1
    return np.array(w[::-1])


def frac_diff_ffd(series: pd.Series, d: float, thres: float = 1e-5) -> pd.Series:
    """Diferenciación fraccionaria de ventana fija. Hace estacionaria la serie
    conservando memoria — a diferencia de `pct_change()`, que la borra entera."""
    w = _ffd_weights(d, thres)
    width = len(w) - 1
    s = series.dropna()
    if width >= len(s):
        raise ValueError(
            f"la ventana de pesos ({width+1}) excede la serie ({len(s)}): con d={d} "
            f"la memoria decae muy lento. Sube `thres` (p.ej. 1e-4) o usa un d mayor.")
    out = {}
    for i in range(width, len(s)):
        out[s.index[i]] = float(np.dot(w, s.iloc[i - width:i + 1].values))
    return pd.Series(out, name=series.name)


def min_ffd_order(series: pd.Series, max_pvalue: float = 0.05,
                  grid: Sequence[float] = tuple(np.linspace(0, 1, 11))) -> float:
    """Menor `d` que hace la serie estacionaria (ADF) conservando el máximo de
    memoria. Requiere statsmodels; si no está, devuelve NaN."""
    try:
        from statsmodels.tsa.stattools import adfuller
    except ImportError:
        return float("nan")
    for d in grid:
        x = frac_diff_ffd(np.log(series.astype(float)), d).dropna()
        if len(x) < 20:
            continue
        if adfuller(x, maxlag=1, regression="c", autolag=None)[1] <= max_pvalue:
            return float(d)
    return float("nan")


# ───────────────────────── etiquetado triple barrera ────────────────────────

def triple_barrier_t1(close: pd.Series, events: pd.DatetimeIndex,
                      pt_sl: tuple[float, float] = (2.0, 2.0),
                      target: pd.Series | None = None,
                      max_holding_days: int = 20) -> pd.DataFrame:
    """Triple barrera (AFML 3): para cada evento devuelve el instante en que la
    etiqueta queda determinada (`t1`) y el signo.

    Su utilidad aquí no es el ML: es que produce el `t1` que necesitan la purga
    y el embargo. Un stop/take-profit ES una barrera horizontal, y su fecha de
    toque es exactamente cuándo la información deja de ser futura.
    """
    if target is None:
        target = close.pct_change().rolling(20).std().reindex(events).fillna(0.02)
    out = []
    for t0 in events:
        end = min(t0 + pd.Timedelta(days=max_holding_days), close.index[-1])
        path = close.loc[t0:end]
        if len(path) < 2:
            continue
        r = path / path.iloc[0] - 1.0
        trg = float(target.get(t0, np.nan))
        up = r[r >= pt_sl[0] * trg].index.min() if pt_sl[0] > 0 else pd.NaT
        dn = r[r <= -pt_sl[1] * trg].index.min() if pt_sl[1] > 0 else pd.NaT
        t1 = min([t for t in (up, dn, end) if pd.notna(t)])
        label = 1 if t1 == up else (-1 if t1 == dn else 0)
        out.append({"t0": t0, "t1": t1, "label": label, "target": trg})
    return pd.DataFrame(out).set_index("t0")
