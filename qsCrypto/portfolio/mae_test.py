import unittest

import numpy as np
import pandas as pd

from qsCrypto.portfolio.mae import (
    Excursion, excursions_from_ohlc, mae_star, win_prob_curve, edge_ratio,
    resolvability, flat_bar_mask,
)


# Deterministic path used across several tests. Entry at 100, N = 10.
#   bars:      (high, low)
#   worst low  = 96   -> MAE = 4 points = 0.4 N
#   best high  = 105  -> MFE = 5 points = 0.5 N
PATH = [(101.0, 99.0), (102.0, 96.0), (105.0, 100.0)]
ENTRY, N = 100.0, 10.0


class TestExcursionLong(unittest.TestCase):
    def setUp(self):
        self.exc = Excursion.open(side=1, fill=ENTRY, n=N, stop_n=1.0)
        for high, low in PATH:
            self.exc.update_bar(high, low)

    def test_known_path_in_points(self):
        self.assertEqual(self.exc.mae_pts, 4.0)
        self.assertEqual(self.exc.mfe_pts, 5.0)

    def test_normalisation_by_n_and_r(self):
        self.assertAlmostEqual(self.exc.mae_n, 0.4)
        self.assertAlmostEqual(self.exc.mfe_n, 0.5)
        # STOP_N = 1.0 makes R == N, so the two views coincide
        self.assertAlmostEqual(self.exc.mae_r, self.exc.mae_n)

    def test_r_scales_the_reading(self):
        """A 2N stop halves the same excursion when read in R."""
        exc = Excursion.open(side=1, fill=ENTRY, n=N, stop_n=2.0)
        for high, low in PATH:
            exc.update_bar(high, low)
        self.assertAlmostEqual(exc.mae_n, 0.4)
        self.assertAlmostEqual(exc.mae_r, 0.2)

    def test_bars_to_extremes(self):
        self.assertEqual(self.exc.bars_to_mae, 2)   # low of 96 on bar 2
        self.assertEqual(self.exc.bars_to_mfe, 3)   # high of 105 on bar 3

    def test_e_ratio(self):
        self.assertAlmostEqual(self.exc.e_ratio, 1.25)


class TestExcursionShort(unittest.TestCase):
    def test_long_short_symmetry(self):
        """A short on the mirrored path must report identical MAE and MFE."""
        short = Excursion.open(side=-1, fill=ENTRY, n=N, stop_n=1.0)
        for high, low in [(2 * ENTRY - low, 2 * ENTRY - high) for high, low in PATH]:
            short.update_bar(high, low)
        self.assertEqual(short.mae_pts, 4.0)
        self.assertEqual(short.mfe_pts, 5.0)

    def test_barriers_are_mirrored(self):
        short = Excursion.open(side=-1, fill=ENTRY, n=N, stop_n=1.0)
        stop, take = short.barrier_prices(stop_n=1.0, take_n=3.0)
        self.assertEqual(stop, 110.0)   # above entry for a short
        self.assertEqual(take, 70.0)


class TestExcursionInvariants(unittest.TestCase):
    def test_zero_when_price_never_goes_against(self):
        exc = Excursion.open(side=1, fill=ENTRY, n=N)
        for high, low in [(101.0, 100.0), (103.0, 101.0)]:
            exc.update_bar(high, low)
        self.assertEqual(exc.mae_pts, 0.0)
        self.assertEqual(exc.e_ratio, np.inf)

    def test_mae_is_monotone_non_decreasing(self):
        exc = Excursion.open(side=1, fill=ENTRY, n=N)
        seen = []
        for high, low in PATH + [(106.0, 104.0), (99.0, 93.0)]:
            exc.update_bar(high, low)
            seen.append(exc.mae_pts)
        self.assertEqual(seen, sorted(seen))

    def test_tick_and_bar_updates_agree(self):
        by_bar = Excursion.open(side=1, fill=ENTRY, n=N)
        for high, low in PATH:
            by_bar.update_bar(high, low)
        by_tick = Excursion.open(side=1, fill=ENTRY, n=N)
        for high, low in PATH:
            by_tick.update_tick(low)
            by_tick.update_tick(high)
        self.assertEqual(by_bar.mae_pts, by_tick.mae_pts)
        self.assertEqual(by_bar.mfe_pts, by_tick.mfe_pts)

    def test_flat_bar_advances_time_but_not_excursion(self):
        exc = Excursion.open(side=1, fill=ENTRY, n=N)
        exc.update_bar(90.0, 90.0, flat=True)
        self.assertEqual(exc.mae_pts, 0.0)
        self.assertEqual(exc.bars, 1)
        self.assertEqual(exc.flat_frac, 1.0)

    def test_reanchor_moves_stop_but_not_measured_excursion(self):
        """An add trails the stop; it must not rewrite the excursion already seen."""
        exc = Excursion.open(side=1, fill=ENTRY, n=N)
        exc.update_bar(102.0, 96.0)
        mae_before = exc.mae_pts
        exc.reanchor(105.0)
        self.assertEqual(exc.mae_pts, mae_before)
        stop, take = exc.barrier_prices(stop_n=1.0, take_n=3.0)
        self.assertEqual(stop, 95.0)    # trails to the new fill: 105 - 1N
        self.assertEqual(take, 130.0)   # target stays anchored on entry: 100 + 3N

    def test_no_stop_mode(self):
        exc = Excursion.open(side=1, fill=ENTRY, n=N)
        stop, _ = exc.barrier_prices(stop_n=float("inf"))
        self.assertEqual(stop, -np.inf)

    def test_rejects_bad_side(self):
        with self.assertRaises(ValueError):
            Excursion(side=0, anchor0=ENTRY, n0=N, r0=N)


class TestVectorisedParity(unittest.TestCase):
    """The incremental accumulator and the vectorised reader must agree."""

    def setUp(self):
        idx = pd.date_range("2026-01-01", periods=len(PATH), freq="30min")
        self.df = pd.DataFrame(
            {"open": [100.0, 101.0, 100.0],
             "high": [h for h, _ in PATH],
             "low": [l for _, l in PATH],
             "close": [100.0, 100.0, 104.0],
             "volume": [10.0, 10.0, 10.0]},
            index=idx)
        self.trades = pd.DataFrame([{
            "opened": idx[0], "closed": idx[-1], "side": 1,
            "entry0": ENTRY, "n_entry": N, "r_pts": N,
        }])

    def test_parity_with_excursion(self):
        """Fed the same bars, both implementations must agree exactly."""
        exc = Excursion.open(side=1, fill=ENTRY, n=N)
        for high, low in PATH:
            exc.update_bar(high, low)
        got = excursions_from_ohlc(self.df, self.trades, include_t0_bar=True).iloc[0]
        self.assertAlmostEqual(got.mae_n, exc.mae_n)
        self.assertAlmostEqual(got.mfe_n, exc.mfe_n)
        self.assertEqual(got.bars_to_mae, 1)   # positional within the window

    def test_entry_bar_is_excluded_by_default(self):
        """The engine opens after that bar's exit checks, so measurement starts
        on the next bar. Counting the entry bar would fold in the range the
        market travelled BEFORE the fill."""
        with_t0 = excursions_from_ohlc(self.df, self.trades, include_t0_bar=True).iloc[0]
        without = excursions_from_ohlc(self.df, self.trades).iloc[0]
        self.assertEqual(with_t0.n_bars, 3)
        self.assertEqual(without.n_bars, 2)
        # bar 0 only spans 99-101, so dropping it cannot change these extremes
        self.assertEqual(with_t0.mae_price, without.mae_price)

    def test_flat_bars_are_neutralised(self):
        df = self.df.copy()
        df.loc[df.index[1], ["high", "low", "volume"]] = [96.0, 96.0, 0.0]
        df["flat"] = flat_bar_mask(df)
        got = excursions_from_ohlc(df, self.trades, include_t0_bar=True).iloc[0]
        # bar 1 held the worst low; flagged flat it must not count
        self.assertEqual(got.mae_price, 99.0)
        self.assertAlmostEqual(got.flat_frac, 1 / 3)


class TestThresholdCalibration(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(0)
        # winners hug small MAE, losers run wide — the separation MAE assumes
        self.mae = pd.Series(np.r_[rng.uniform(0.0, 0.5, 60),
                                   rng.uniform(0.6, 2.0, 60)])
        self.win = pd.Series([True] * 60 + [False] * 60)

    def test_quantile_of_winners(self):
        star, info = mae_star(self.mae, self.win, q=0.90, min_winners=30)
        self.assertFalse(info["fallback_usado"])
        self.assertEqual(info["n_winners"], 60)
        self.assertLessEqual(star, 0.5)
        self.assertGreaterEqual(info["winners_dentro"], 0.90)

    def test_refuses_to_fit_on_a_tiny_sample(self):
        """Two winners must never produce a fitted percentile."""
        star, info = mae_star(pd.Series([0.2, 0.3, 1.4]), pd.Series([True, True, False]),
                              min_winners=30, fallback=1.0)
        self.assertTrue(info["fallback_usado"])
        self.assertEqual(star, 1.0)

    def test_resolution_floor_clips_from_below(self):
        star, info = mae_star(self.mae, self.win, q=0.90, min_winners=30,
                              resolvable_floor=0.5)
        self.assertTrue(info["recortado_por_resolucion"])
        self.assertEqual(star, 0.5)

    def test_flat_heavy_trades_are_excluded(self):
        flat = pd.Series([0.0] * 119 + [0.9])
        _, info = mae_star(self.mae, self.win, min_winners=30,
                           flat_frac=flat, max_flat=0.25)
        self.assertEqual(info["n_total"], 119)

    def test_win_prob_curve_declines_when_mae_discriminates(self):
        cur = win_prob_curve(self.mae, self.win, grid=np.array([0.0, 0.5, 1.0]))
        self.assertAlmostEqual(cur.p_win_ge.iloc[0], 0.5)
        self.assertEqual(cur.p_win_ge.iloc[-1], 0.0)   # nothing above 1.0 N wins
        self.assertLess(cur.lift.iloc[1], 0.0)

    def test_edge_ratio(self):
        self.assertAlmostEqual(edge_ratio(pd.Series([2.0, 4.0]), pd.Series([1.0, 1.0])), 3.0)


class TestResolvability(unittest.TestCase):
    def test_threshold_below_typical_bar_range_is_unresolvable(self):
        # bars spanning ~0.5 N, the measured 30-minute regime
        out = resolvability(pd.Series([0.5] * 100), [0.2, 0.9]).set_index("x_n")
        self.assertEqual(out.loc[0.2, "veredicto"], "NO RESOLUBLE")
        self.assertEqual(out.loc[0.9, "veredicto"], "RESOLUBLE")


class TestUnitRiskArithmetic(unittest.TestCase):
    """The claim the Part A markdown makes: 1% of equity at the common stop.

    Adds every 1/2 N with every stop trailed to 1N from the newest fill. The
    aggregate risk peaks at 1.5 N with two and three units on, and returns to
    1.0 N at full load because the earlier units are already in profit.
    """

    EQUITY, RISK = 100_000.0, 0.01

    def _aggregate_risk_n(self, n_units):
        n = 1.0
        fills = [1.0 + 0.5 * i * n for i in range(n_units)]   # entry at 1.0
        stop = fills[-1] - 1.0 * n                            # trailed to newest
        return sum(fill - stop for fill in fills)             # in N per unit

    def test_risk_ladder(self):
        self.assertAlmostEqual(self._aggregate_risk_n(1), 1.0)
        self.assertAlmostEqual(self._aggregate_risk_n(2), 1.5)
        self.assertAlmostEqual(self._aggregate_risk_n(3), 1.5)
        self.assertAlmostEqual(self._aggregate_risk_n(4), 1.0)

    def test_one_unit_at_one_n_is_one_percent_of_equity(self):
        n_points, mult = 424.0, 0.1
        unit_qty = (self.RISK * self.EQUITY) / (n_points * mult)
        loss_at_stop = unit_qty * (1.0 * n_points) * mult
        self.assertAlmostEqual(loss_at_stop, self.RISK * self.EQUITY)

    def test_full_load_is_also_one_percent(self):
        n_points, mult = 424.0, 0.1
        unit_qty = (self.RISK * self.EQUITY) / (n_points * mult)
        loss = unit_qty * self._aggregate_risk_n(4) * n_points * mult
        self.assertAlmostEqual(loss, self.RISK * self.EQUITY)

    def test_peak_exposure_is_one_and_a_half_percent(self):
        n_points, mult = 424.0, 0.1
        unit_qty = (self.RISK * self.EQUITY) / (n_points * mult)
        peak = unit_qty * self._aggregate_risk_n(3) * n_points * mult
        self.assertAlmostEqual(peak, 1.5 * self.RISK * self.EQUITY)


if __name__ == "__main__":
    unittest.main()
