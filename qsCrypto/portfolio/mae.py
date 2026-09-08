#!/usr/bin/env python3
"""
Maximum Adverse / Favourable Excursion (MAE / MFE), risk-normalised
===================================================================

MAE is the worst unrealised loss a position carries between entry and exit;
MFE the best unrealised gain. Introduced by John Sweeney, they turn stop
placement into an empirical question instead of a heuristic one.

Definitions (all in PRICE POINTS first, then normalised):

    side = +1 long, -1 short
    MAE  = side * (anchor - worst_adverse_price)     >= 0
    MFE  = side * (best_favourable_price - anchor)   >= 0

Normalisers:

    N = 20-bar EMA of True Range at entry   -> config-free volatility unit
    R = STOP_N * N = entry-to-initial-stop  -> the unit the brief asks for

    mae_n = MAE / N        mae_r = MAE / R = mae_n / STOP_N

Both are kept on purpose. With STOP_N = 1.0 they coincide (R == N), but
`mae_n` stays comparable across grid points if STOP_N is ever varied, and it
is the only one defined for a calibration run carried out with no stop at all.

Anchoring: path extremes accumulate in ABSOLUTE PRICE, which does not depend
on any anchor. Normalisation is applied on close against the anchor frozen at
the FIRST fill, so that pyramiding onto a position never retroactively
rewrites an excursion that was already measured. The live anchor (last fill)
moves with each add and is used only to place barriers, mirroring the Turtle
rule of trailing every stop to the newest unit.

Triple barrier (Lopez de Prado, AFML ch. 3) in these terms: the lower barrier
is the stop, the upper barrier the profit target and the vertical barrier a
holding limit. MAE analysis is the empirical method for choosing the width of
the lower barrier instead of an arbitrary volatility multiple.

Usage:
    from qsCrypto.portfolio.mae import Excursion, mae_star, win_prob_curve

    exc = Excursion.open(side=+1, fill=95_000.0, n=424.0, stop_n=1.0)
    exc.update_bar(high=95_200, low=94_800)     # backtest, bar by bar
    exc.update_tick(94_750)                     # live, tick by tick
    exc.mae_r, exc.mfe_r, exc.e_ratio
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = [
    "Excursion",
    "excursions_from_ohlc",
    "mae_star",
    "win_prob_curve",
    "edge_ratio",
    "resolvability",
    "flat_bar_mask",
]


# ─────────────────────────────────────────────────────────────────────────────
# Incremental accumulator — usable bar by bar (backtest) and tick by tick (live)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Excursion:
    """Running path extremes of one open position.

    `anchor0` / `n0` / `r0` are frozen at the first fill and form the frame of
    reference for every statistic. `anchor` is the live reference (last fill)
    that the engine moves on each add; it only ever places barriers.
    """

    side: int
    anchor0: float
    n0: float
    r0: float
    anchor: float = None
    mae_px: float = None
    mfe_px: float = None
    bars: int = 0
    flat_bars: int = 0
    amb_bars: int = 0
    bars_to_mae: int = 0
    bars_to_mfe: int = 0

    def __post_init__(self):
        if self.side not in (1, -1):
            raise ValueError(f"side must be +1 or -1, got {self.side!r}")
        if self.anchor is None:
            self.anchor = self.anchor0
        if self.mae_px is None:
            self.mae_px = self.anchor0
        if self.mfe_px is None:
            self.mfe_px = self.anchor0

    @classmethod
    def open(cls, side, fill, n, stop_n=1.0):
        """Open an excursion at `fill` with volatility unit `n`. R = stop_n * n."""
        return cls(side=int(side), anchor0=float(fill), n0=float(n),
                   r0=float(stop_n) * float(n))

    # ── updates ──────────────────────────────────────────────────────────────
    def update_bar(self, high, low, flat=False):
        """Consume one OHLC bar.

        `flat` marks a bar with no observable range (high == low and no
        volume). It advances time but contributes no excursion: counting a
        synthetic bar as "price never moved against us" would bias MAE low,
        which is the dangerous direction — it makes stops look safer than they
        are.
        """
        self.bars += 1
        if flat:
            self.flat_bars += 1
            return
        adv, fav = (low, high) if self.side > 0 else (high, low)
        self._touch(float(adv), float(fav))

    def update_tick(self, price):
        """Consume one price. Idempotent and O(1); for the live monitor."""
        px = float(price)
        self._touch(px, px)

    def _touch(self, adv, fav):
        if self.side * (adv - self.mae_px) < 0:
            self.mae_px, self.bars_to_mae = adv, self.bars
        if self.side * (fav - self.mfe_px) > 0:
            self.mfe_px, self.bars_to_mfe = fav, self.bars

    def reanchor(self, fill):
        """After an add: move the BARRIER anchor only. Extremes are untouched."""
        self.anchor = float(fill)

    # ── reads ────────────────────────────────────────────────────────────────
    @property
    def mae_pts(self):
        return self.side * (self.anchor0 - self.mae_px)

    @property
    def mfe_pts(self):
        return self.side * (self.mfe_px - self.anchor0)

    @property
    def mae_n(self):
        return self.mae_pts / self.n0 if self.n0 else np.nan

    @property
    def mfe_n(self):
        return self.mfe_pts / self.n0 if self.n0 else np.nan

    @property
    def mae_r(self):
        return self.mae_pts / self.r0 if self.r0 else np.nan

    @property
    def mfe_r(self):
        return self.mfe_pts / self.r0 if self.r0 else np.nan

    @property
    def flat_frac(self):
        return self.flat_bars / self.bars if self.bars else 0.0

    @property
    def e_ratio(self):
        """MFE / MAE. Above 1 the path gives more in favour than against."""
        return self.mfe_pts / self.mae_pts if self.mae_pts > 0 else np.inf

    def barrier_prices(self, stop_n, take_n=None, n_ref=None):
        """Horizontal barrier prices right now.

        Stop uses the LIVE anchor, preserving the Turtle rule that every add
        trails all stops to the newest fill. The target uses the INITIAL
        anchor: a profit target belongs to the trade, not to the last add —
        re-anchoring it would mean a pyramided position could never reach it.

        `stop_n = inf` yields -inf / +inf, i.e. the "no stop" observation mode,
        with no branch needed at the call site.
        """
        n = self.n0 if n_ref is None else float(n_ref)
        stop = self.anchor - self.side * float(stop_n) * n
        take = None if take_n is None else self.anchor0 + self.side * float(take_n) * n
        return stop, take

    def snapshot(self, price=None):
        """Flat dict for the live monitor and the trade journal."""
        out = {
            "side": self.side, "anchor0": self.anchor0, "anchor": self.anchor,
            "n0": self.n0, "r0": self.r0,
            "mae_price": self.mae_px, "mfe_price": self.mfe_px,
            "mae_pts": self.mae_pts, "mfe_pts": self.mfe_pts,
            "mae_n": self.mae_n, "mfe_n": self.mfe_n,
            "mae_r": self.mae_r, "mfe_r": self.mfe_r,
            "bars": self.bars, "bars_to_mae": self.bars_to_mae,
            "bars_to_mfe": self.bars_to_mfe, "flat_frac": self.flat_frac,
            "e_ratio": self.e_ratio,
        }
        if price is not None:
            out["price"] = float(price)
            out["pnl_pts"] = self.side * (float(price) - self.anchor0)
            out["pnl_r"] = out["pnl_pts"] / self.r0 if self.r0 else np.nan
        return out


# ─────────────────────────────────────────────────────────────────────────────
# Data quality
# ─────────────────────────────────────────────────────────────────────────────
def flat_bar_mask(df):
    """Bars carrying no information: zero range and no volume.

    The IBKR demo feed returns these for contracts outside their front-month
    window (measured: 33% of daily bars, 2024-09..2025-05, collapsing to 0.8%
    once useRTH=True and the chain fetch restricts each contract to its own
    front-month window). A flat bar has zero excursion by construction, so
    leaving them in biases MAE low and every stop looks safer than it is.
    """
    vol = df["volume"] if "volume" in df else pd.Series(0.0, index=df.index)
    return (df["high"] <= df["low"]) & (vol.fillna(0.0) <= 0)


def resolvability(bar_range_n, thresholds):
    """Can a touch at `x`*N be distinguished inside a bar of this typical range?

    Returns, per threshold, the fraction of bars whose range exceeds it: that
    fraction is the probability that the intrabar ordering (stop first or
    target first) is UNOBSERVABLE. Measured on 30-minute MBT bars the median
    range is ~0.48 N, so a threshold below ~0.5 N cannot be resolved at that
    granularity and scanning percentiles below it is measuring nothing.
    """
    r = pd.Series(bar_range_n).replace([np.inf, -np.inf], np.nan).dropna().to_numpy(float)
    rows = []
    for x in np.asarray(thresholds, dtype=float):
        p = float((r >= x).mean()) if r.size else np.nan
        rows.append({
            "x_n": float(x), "p_bar_range_ge_x": p,
            "veredicto": ("RESOLUBLE" if p < 0.25 else
                          "AMBIGUO" if p < 0.60 else "NO RESOLUBLE"),
        })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Vectorised per-trade computation from OHLC
# ─────────────────────────────────────────────────────────────────────────────
def excursions_from_ohlc(df, trades, t0_col="opened", t1_col="closed",
                         side_col="side", anchor_col="entry0", n_col="n_entry",
                         r_col="r_pts", flat_col="flat", include_t1_bar=True,
                         include_t0_bar=False):
    """MAE / MFE per trade straight from OHLC, independent of the engine.

    `df`     OHLC of ONE symbol, sorted index (e.g. PREP[(entry, exit)][sym]).
    `trades` blotter carrying the columns named above.

    `include_t0_bar` defaults to False to match the engine, which opens a
    position after that bar's exit checks have run and so starts measuring on
    the following bar. Counting the entry bar would fold in the range the
    market travelled BEFORE the fill, which is not part of the trade's path.

    Exists to cross-check the incremental path: if `Excursion` and this
    disagree, one of them is wrong, and a silent disagreement is exactly the
    kind of bug a backtest hides until live trading finds it.
    """
    idx = df.index
    hi = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)
    flat = (df[flat_col].to_numpy(bool) if flat_col in df
            else np.zeros(len(df), dtype=bool))
    # neutralise flat bars with +-inf so they can never win a min/max
    hi_e = np.where(flat, -np.inf, hi)
    lo_e = np.where(flat, np.inf, lo)

    i0 = idx.get_indexer(pd.DatetimeIndex(trades[t0_col]))
    i1 = idx.get_indexer(pd.DatetimeIndex(trades[t1_col]))
    rows = []
    for k, (a, b) in enumerate(zip(i0, i1)):
        row = trades.iloc[k]
        if a < 0 or b < 0 or b < a:
            rows.append({})
            continue
        a_ = a if include_t0_bar else a + 1
        b_ = b + 1 if include_t1_bar else b
        if b_ <= a_:      # opened and closed with no bar in between
            a_ = a
        side = int(row[side_col])
        anchor = float(row[anchor_col])
        n0 = float(row[n_col])
        r0 = float(row[r_col]) if r_col in trades.columns else n0
        w_hi, w_lo, w_fl = hi_e[a_:b_], lo_e[a_:b_], flat[a_:b_]
        if side > 0:
            mae_px, mfe_px = float(np.min(w_lo)), float(np.max(w_hi))
            j_mae, j_mfe = int(np.argmin(w_lo)), int(np.argmax(w_hi))
        else:
            mae_px, mfe_px = float(np.max(w_hi)), float(np.min(w_lo))
            j_mae, j_mfe = int(np.argmax(w_hi)), int(np.argmin(w_lo))
        if not np.isfinite(mae_px):          # every bar in the window was flat
            mae_px, j_mae = anchor, 0
        if not np.isfinite(mfe_px):
            mfe_px, j_mfe = anchor, 0
        # Clamp against the anchor. A trade that never traded below its entry
        # has an MAE of zero, not a negative one: an excursion is a distance
        # travelled against the position, and the accumulator starts both
        # extremes at the anchor for exactly this reason.
        if side * (mae_px - anchor) > 0:
            mae_px, j_mae = anchor, 0
        if side * (mfe_px - anchor) < 0:
            mfe_px, j_mfe = anchor, 0
        mae_pts = side * (anchor - mae_px)
        mfe_pts = side * (mfe_px - anchor)
        rows.append({
            "mae_price": mae_px, "mfe_price": mfe_px,
            "mae_pts": mae_pts, "mfe_pts": mfe_pts,
            "mae_n": mae_pts / n0 if n0 else np.nan,
            "mfe_n": mfe_pts / n0 if n0 else np.nan,
            "mae_r": mae_pts / r0 if r0 else np.nan,
            "mfe_r": mfe_pts / r0 if r0 else np.nan,
            "bars_to_mae": j_mae, "bars_to_mfe": j_mfe,
            "flat_frac": float(w_fl.mean()) if w_fl.size else 0.0,
            "n_bars": int(b_ - a_),
        })
    return pd.DataFrame(rows, index=trades.index)


# ─────────────────────────────────────────────────────────────────────────────
# Threshold calibration and diagnostics
# ─────────────────────────────────────────────────────────────────────────────
def mae_star(mae, win, q=0.90, min_winners=30, flat_frac=None, max_flat=0.25,
             fallback=1.0, resolvable_floor=None):
    """MAE* = `q` quantile of the MAE of WINNING trades, in N units.

    Reading: "with this threshold, q% of the trades that would have won still
    survive". Returns `(threshold, info)`.

    Two refusals are deliberate, because both failure modes are silent:
      * fewer than `min_winners` winners -> return `fallback`, never a quantile
        fitted on a handful of observations;
      * `resolvable_floor` (typically the median bar range in N) clips from
        below, since a touch finer than one bar's range cannot be resolved.

    The winner/loser split is outcome information, so this must be fitted
    INSIDE a training fold, never on the full sample (checklist.md:25-26).
    """
    d = pd.DataFrame({"mae": pd.Series(mae).astype(float),
                      "win": pd.Series(win).astype(bool)}).dropna()
    if flat_frac is not None:
        keep = pd.Series(flat_frac).reindex(d.index).fillna(0.0) <= max_flat
        d = d[keep]
    w = d.loc[d.win, "mae"]
    info = {"n_total": int(len(d)), "n_winners": int(len(w)), "q": float(q),
            "fallback_usado": False, "recortado_por_resolucion": False}
    if len(w) < min_winners:
        info["fallback_usado"] = True
        info["motivo"] = f"solo {len(w)} ganadores (< {min_winners})"
        info["mae_star"] = float(fallback)
        return float(fallback), info
    star = float(np.quantile(w.to_numpy(float), q))
    info["mae_star_bruto"] = star
    info["winners_dentro"] = float((w <= star).mean())
    losers = d.loc[~d.win, "mae"]
    info["losers_dentro"] = float((losers <= star).mean()) if len(losers) else np.nan
    if resolvable_floor is not None and star < float(resolvable_floor):
        star = float(resolvable_floor)
        info["recortado_por_resolucion"] = True
    info["mae_star"] = star
    return star, info


def win_prob_curve(mae, win, grid=None):
    """P(win | MAE >= x), its complement, and winner/loser survival curves.

    This is the gate for the whole exercise: if `p_win_ge` does not fall as x
    grows, MAE carries no information about the eventual outcome and cutting
    on it only adds transaction cost. Check it before reading any Sharpe.

    MAE* at quantile q is the x where `surv_win` equals 1 - q.
    """
    grid = np.linspace(0.0, 3.0, 61) if grid is None else np.asarray(grid, float)
    m = pd.Series(mae).to_numpy(float)
    w = pd.Series(win).to_numpy(bool)
    ok = np.isfinite(m)
    m, w = m[ok], w[ok]
    rows = []
    for x in grid:
        ge = m >= x
        rows.append({
            "x_n": float(x), "n_ge": int(ge.sum()), "n_lt": int((~ge).sum()),
            "p_win_ge": float(w[ge].mean()) if ge.any() else np.nan,
            "p_win_lt": float(w[~ge].mean()) if (~ge).any() else np.nan,
            "surv_win": float((m[w] >= x).mean()) if w.any() else np.nan,
            "surv_loss": float((m[~w] >= x).mean()) if (~w).any() else np.nan,
        })
    out = pd.DataFrame(rows)
    out["lift"] = out.p_win_ge - out.p_win_lt          # expected negative
    out["ks"] = (out.surv_loss - out.surv_win).abs()   # KS-style separation
    return out


def edge_ratio(mfe, mae):
    """Aggregate E-ratio (Tharp): mean(MFE) / mean(MAE), both in N.

    Above 1 the entry has immediate directional edge; at or below 1 the entry
    is a timing problem, not a stop problem, and tightening the stop will not
    fix it (MAE.md:85).
    """
    a = pd.Series(mae).astype(float).replace(0.0, np.nan).dropna()
    if a.empty:
        return np.nan
    b = pd.Series(mfe).astype(float).reindex(a.index)
    return float(b.mean() / a.mean())
