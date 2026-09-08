# -*- coding: utf-8 -*-
"""
Contract tests for deflated_sr.py.

These are COMPLETE: they define what a correct implementation must satisfy.
Claude Code fills in deflated_sr.py until `pytest -q` is green.

Run:  pytest -q test_deflated_sr.py

Split:
  - Deterministic unit tests pin the exact formulas (no randomness).
  - Monte-Carlo tests pin the statistical behaviour that is the whole point of
    the DSR: the best of N random strategies must NOT look skilful (DSR ~ 0.5),
    while a genuine edge must survive the deflation (DSR ~ 1).
"""
import math

import numpy as np
import pandas as pd
import pytest
from scipy import stats as scipy_stats

from deflated_sr import (
    EULER_MASCHERONI,
    DSRResult,
    estimated_sharpe_ratio,
    annualized_sharpe_ratio,
    sharpe_ratio_stdev,
    probabilistic_sharpe_ratio,
    min_track_record_length,
    num_independent_trials,
    expected_maximum_sr,
    deflated_sharpe_ratio,
)

RNG_SEED = 20260906


def _normal_trials(rng, n_obs, m_trials, mean=0.0, std=1.0):
    """DataFrame of iid normal returns: shape (n_obs, m_trials)."""
    data = rng.normal(mean, std, size=(n_obs, m_trials))
    return pd.DataFrame(data, columns=[f"t{i}" for i in range(m_trials)])


# --------------------------------------------------------------------------- #
# Deterministic unit tests — exact formulas                                   #
# --------------------------------------------------------------------------- #
def test_estimated_sharpe_matches_mean_over_std():
    rng = np.random.default_rng(RNG_SEED)
    x = pd.Series(rng.normal(0.01, 0.05, size=500))
    assert estimated_sharpe_ratio(x) == pytest.approx(x.mean() / x.std(ddof=1))


def test_estimated_sharpe_dataframe_returns_series():
    rng = np.random.default_rng(RNG_SEED)
    df = _normal_trials(rng, 400, 6)
    out = estimated_sharpe_ratio(df)
    assert isinstance(out, pd.Series) and len(out) == 6


def test_annualized_is_reporting_only_scaling():
    # per-period 0.1 over 365 periods -> 0.1 * sqrt(365)
    assert annualized_sharpe_ratio(0.1, periods_per_year=365) == pytest.approx(0.1 * math.sqrt(365))


def test_sr_stdev_normal_case_closed_form():
    # skew=0, kurtosis=3  ->  sqrt((1 + 0.5*sr^2)/(n-1))
    n, sr = 100, 0.10
    expected = math.sqrt((1 + 0.5 * sr ** 2) / (n - 1))
    got = sharpe_ratio_stdev(n=n, skew=0.0, kurtosis=3.0, sr=sr)
    assert got == pytest.approx(expected)


def test_sr_stdev_full_formula():
    # general skew/kurtosis: (1 - g3*sr + ((g4-1)/4)*sr^2)/(n-1)
    n, sr, g3, g4 = 250, 0.12, -0.7, 6.0
    expected = math.sqrt((1 - g3 * sr + ((g4 - 1) / 4) * sr ** 2) / (n - 1))
    got = sharpe_ratio_stdev(n=n, skew=g3, kurtosis=g4, sr=sr)
    assert got == pytest.approx(expected)


def test_negative_skew_increases_sr_stdev():
    base = sharpe_ratio_stdev(n=250, skew=0.0, kurtosis=3.0, sr=0.12)
    neg = sharpe_ratio_stdev(n=250, skew=-1.0, kurtosis=3.0, sr=0.12)
    assert neg > base  # left-skew inflates the SR standard error for a positive SR


def test_psr_is_half_when_benchmark_equals_estimate():
    assert probabilistic_sharpe_ratio(sr=0.15, sr_std=0.05, sr_benchmark=0.15) == pytest.approx(0.5)


def test_psr_matches_normal_cdf():
    sr, sr_std, bench = 0.20, 0.04, 0.05
    expected = scipy_stats.norm.cdf((sr - bench) / sr_std)
    assert probabilistic_sharpe_ratio(sr=sr, sr_std=sr_std, sr_benchmark=bench) == pytest.approx(expected)


def test_min_track_record_length_closed_form():
    sr, sr_std, n, bench, prob = 0.10, 0.05, 500, 0.0, 0.95
    z = scipy_stats.norm.ppf(prob)
    expected = 1 + (sr_std ** 2 * (n - 1)) * (z / (sr - bench)) ** 2
    got = min_track_record_length(sr=sr, sr_std=sr_std, n=n, sr_benchmark=bench, prob=prob)
    assert got == pytest.approx(expected)


def test_num_independent_trials_bounds():
    assert num_independent_trials(m=50, rho=1.0) == 1     # perfectly correlated -> one trial
    assert num_independent_trials(m=50, rho=0.0) == 50    # independent -> all M count


def test_num_independent_trials_from_dataframe_is_between_1_and_m():
    rng = np.random.default_rng(RNG_SEED)
    df = _normal_trials(rng, 800, 30)
    n_eff = num_independent_trials(df)
    assert 1 <= n_eff <= 30
    assert isinstance(n_eff, int)


def test_expected_max_sr_scales_linearly_with_dispersion():
    a = expected_maximum_sr(independent_trials=10, trials_sr_std=0.02, expected_mean_sr=0.0)
    b = expected_maximum_sr(independent_trials=10, trials_sr_std=0.04, expected_mean_sr=0.0)
    assert b == pytest.approx(2 * a)


def test_expected_max_sr_matches_closed_form():
    n, sigma = 20, 0.03
    z = scipy_stats.norm.ppf
    maxz = (1 - EULER_MASCHERONI) * z(1 - 1.0 / n) + EULER_MASCHERONI * z(1 - 1.0 / (n * math.e))
    assert expected_maximum_sr(independent_trials=n, trials_sr_std=sigma) == pytest.approx(sigma * maxz)


def test_expected_max_sr_increases_with_more_trials():
    few = expected_maximum_sr(independent_trials=5, trials_sr_std=0.03)
    many = expected_maximum_sr(independent_trials=200, trials_sr_std=0.03)
    assert many > few  # more searching -> higher bar to clear


# --------------------------------------------------------------------------- #
# Validation                                                                  #
# --------------------------------------------------------------------------- #
def test_rejects_fewer_than_two_trials():
    rng = np.random.default_rng(RNG_SEED)
    sel = pd.Series(rng.normal(0, 1, 300))
    one_col = pd.DataFrame({"t0": sel})
    with pytest.raises(ValueError):
        deflated_sharpe_ratio(sel, one_col)


def test_rejects_too_short_selected_series():
    rng = np.random.default_rng(RNG_SEED)
    trials = _normal_trials(rng, 300, 5)
    with pytest.raises(ValueError):
        deflated_sharpe_ratio(pd.Series([0.01]), trials)


# --------------------------------------------------------------------------- #
# Result object                                                               #
# --------------------------------------------------------------------------- #
def test_result_shape_and_bounds():
    rng = np.random.default_rng(RNG_SEED)
    trials = _normal_trials(rng, 1000, 20)
    selected = trials[estimated_sharpe_ratio(trials).idxmax()]
    res = deflated_sharpe_ratio(selected, trials)
    assert isinstance(res, DSRResult)
    assert 0.0 <= res.dsr <= 1.0
    assert res.n_trials_total == 20
    assert 1 <= res.n_trials_effective <= 20
    assert math.isfinite(res.sr_star) and math.isfinite(res.sr_std)
    assert res.n_obs == 1000


# --------------------------------------------------------------------------- #
# Monte-Carlo behaviour — the reason the DSR exists                           #
# --------------------------------------------------------------------------- #
def test_null_best_of_N_is_not_flagged_as_skill():
    """
    Pick the best of N skill-less strategies and deflate it. Because the selected
    SR ~ the expected maximum SR, the DSR must center near 0.5 and almost never
    look confidently skilful. This is the anti-overfitting property.
    """
    rng = np.random.default_rng(RNG_SEED)
    dsrs = []
    for _ in range(120):
        trials = _normal_trials(rng, 1000, 40, mean=0.0, std=1.0)
        selected = trials[estimated_sharpe_ratio(trials).idxmax()]
        dsrs.append(deflated_sharpe_ratio(selected, trials).dsr)
    dsrs = np.array(dsrs)
    assert 0.35 <= dsrs.mean() <= 0.65          # centered near 0.5, not inflated
    assert (dsrs > 0.95).mean() < 0.15          # rarely fooled into "real edge"


def test_genuine_edge_survives_deflation():
    """A strategy with a real per-period edge, selected among null trials, keeps a high DSR."""
    rng = np.random.default_rng(RNG_SEED)
    nulls = _normal_trials(rng, 2000, 39, mean=0.0, std=1.0)
    edge = pd.Series(rng.normal(0.15, 1.0, size=2000), name="edge")   # SR ~ 0.15 per period
    trials = pd.concat([nulls, edge], axis=1)
    selected = trials[estimated_sharpe_ratio(trials).idxmax()]
    res = deflated_sharpe_ratio(selected, trials)
    assert res.dsr > 0.95


def test_more_trials_lowers_dsr_for_same_strategy():
    """Holding the selected strategy fixed, searching more configs raises SR* and lowers DSR."""
    rng = np.random.default_rng(RNG_SEED)
    n = 1500
    selected = pd.Series(rng.normal(0.06, 1.0, size=n), name="sel")   # modest edge
    few = pd.concat([selected] + [pd.Series(rng.normal(0, 1, n), name=f"n{i}") for i in range(4)], axis=1)
    many = pd.concat([selected] + [pd.Series(rng.normal(0, 1, n), name=f"n{i}") for i in range(200)], axis=1)
    dsr_few = deflated_sharpe_ratio(selected, few).dsr
    dsr_many = deflated_sharpe_ratio(selected, many).dsr
    assert dsr_many < dsr_few
