"""Tests de run_portfolio_backtest y del registro de fills.

Lo que se fija: una cartera de UN símbolo sin topes activos es exactamente
`run_backtest`, los fills cuadran con los trades (el volumen negociado sale
de ahí), y los topes de la cuenta actúan como en BinancePerpetualsPortfolio
en vivo: rechazan la orden entera en vez de recortarla.
"""
import os
import unittest

import numpy as np
import pandas as pd

from qsCrypto.backtest.engine import (
    BacktestParams, run_backtest, run_portfolio_backtest,
)
from qsCrypto.strategy.strategy import compute_vwap, vwap_fade_policy

CACHE = os.path.join(os.path.dirname(__file__), "..", "..", "data_cache",
                     "binance", "30m", "BTCUSDT.csv")


@unittest.skipUnless(os.path.exists(CACHE), "sin caché de BTCUSDT")
class TestPortfolioEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        df = pd.read_csv(CACHE, index_col=0, parse_dates=True)
        cls.df = df.loc["2025-01-01":"2025-06-30"]

    def test_single_symbol_without_binding_caps_equals_run_backtest(self):
        p = BacktestParams(equity0=100_000.0, max_gross_lev=1e9)
        ec0, tr0 = run_backtest(self.df, p)
        ec1, tr1, fills = run_portfolio_backtest({"BTC": self.df}, p)
        self.assertTrue(np.allclose(ec0.to_numpy(), ec1.to_numpy(), rtol=0, atol=1e-6))
        self.assertEqual(len(tr0), len(tr1))
        self.assertTrue(np.allclose(tr0["pnl"], tr1["pnl"], rtol=0, atol=1e-6))
        self.assertGreater(len(tr0), 20)

    def test_fills_reconcile_with_trades(self):
        """Σ nocional de entrada+adds = qty·precio medio; salida = qty·exit."""
        _, tr, fills = run_portfolio_backtest(
            {"BTC": self.df}, BacktestParams(equity0=100_000.0, max_gross_lev=10.0))
        entries = fills[fills.kind != "exit"]
        exits = fills[fills.kind == "exit"]
        closed_entry_notional = entries.notional.sum() - sum(
            ob["qty"] * ob["entry"] for ob in tr.attrs["open_book"].values()
            if ob is not None)
        self.assertAlmostEqual(closed_entry_notional,
                               (tr.qty * tr.entry).sum(), delta=1e-3)
        self.assertAlmostEqual(exits.notional.sum(), (tr.qty * tr.exit).sum(),
                               delta=1e-3)
        self.assertEqual(len(exits), len(tr))
        self.assertGreater((fills.kind == "add").sum(), 0)

    def test_gross_cap_rejects_instead_of_trimming(self):
        _, tr, fills = run_portfolio_backtest(
            {"BTC": self.df}, BacktestParams(equity0=100_000.0, max_gross_lev=0.01))
        self.assertEqual(len(fills), 0)
        self.assertGreater(tr.attrs["rejected"]["apalancamiento"], 0)

    def test_correlation_cap_blocks_the_twin_symbol(self):
        """Dos símbolos idénticos en el mismo grupo con tope de 1 unidad:
        B nunca abre EN LA MISMA DIRECCIÓN mientras A está dentro (A llena
        primero en la misma barra, y una salida de A en la barra t llega
        DESPUÉS del open de t). El tope es por grupo Y dirección, como
        `_corr_unit_count` en vivo: en contra de A, B sí puede abrir."""
        data = {"A": self.df, "B": self.df}
        p = BacktestParams(equity0=100_000.0, max_gross_lev=100.0, max_units=1)
        _, tr, _ = run_portfolio_backtest(
            data, p, corr_groups={"A": "g", "B": "g"}, max_corr_units=1)
        a, b = tr[tr.symbol == "A"], tr[tr.symbol == "B"]
        for _, r in b.iterrows():
            same_side_open = ((a.side == r.side) & (a.opened <= r.opened)
                              & (r.opened <= a.closed))
            self.assertFalse(same_side_open.any())
        self.assertGreater(tr.attrs["rejected"]["topes"], 0)
        _, tr_free, _ = run_portfolio_backtest(data, p)
        self.assertEqual((tr_free.symbol == "A").sum(), (tr_free.symbol == "B").sum())


@unittest.skipUnless(os.path.exists(CACHE), "sin caché de BTCUSDT")
class TestVwapPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        df = pd.read_csv(CACHE, index_col=0, parse_dates=True)
        cls.df = df.loc["2025-01-01":"2025-06-30"]
        cls.p = BacktestParams(equity0=100_000.0)

    def test_no_policy_is_bit_identical_to_before(self):
        """El sistema clásico no puede haber cambiado ni en el último bit:
        los resultados publicados en MeanReversion_Binance_Perp.ipynb se
        apoyan en él."""
        ec0, tr0 = run_backtest(self.df, self.p)
        ec1, tr1 = run_backtest(self.df, self.p, policy=None)
        self.assertTrue(np.array_equal(ec0.to_numpy(), ec1.to_numpy()))
        self.assertEqual(len(tr0), len(tr1))
        self.assertEqual(tr0.attrs["rejected"], {})

    def test_policy_retargets_exits_to_the_frozen_vwap(self):
        """La salida por objetivo cae en el VWAP congelado en la ruptura, no
        en el centro del canal: es lo que distingue a esta estrategia."""
        anchors = compute_vwap(
            self.df["high"].to_numpy(float), self.df["low"].to_numpy(float),
            self.df["close"].to_numpy(float), self.df["volume"].to_numpy(float),
            mode="rolling", bars=48)
        lo, hi = np.nanmin(anchors), np.nanmax(anchors)
        _, tr = run_backtest(self.df, self.p,
                             policy=vwap_fade_policy(min_range_n=2.0))
        targets = tr[tr.reason == "target"]
        self.assertGreater(len(targets), 0)
        # El precio de salida sale del ancla (más el deslizamiento), así que
        # tiene que caer dentro del rango que toma el VWAP en la muestra.
        self.assertTrue(((targets.exit > lo * 0.99)
                         & (targets.exit < hi * 1.01)).all())

    def test_stretch_filter_is_monotone_and_counts_its_vetoes(self):
        counts = []
        for thr in (0.0, 1.0, 2.0, 3.0):
            _, tr = run_backtest(self.df, self.p,
                                 policy=vwap_fade_policy(min_range_n=thr))
            counts.append(len(tr))
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertGreater(counts[0], counts[-1])
        _, tr2 = run_backtest(self.df, self.p,
                              policy=vwap_fade_policy(min_range_n=2.0))
        self.assertGreater(tr2.attrs["rejected"]["poco_estirado"], 0)

    def test_session_mode_runs_and_differs_from_rolling(self):
        _, a = run_backtest(self.df, self.p,
                            policy=vwap_fade_policy(vwap_mode="rolling"))
        _, b = run_backtest(self.df, self.p,
                            policy=vwap_fade_policy(vwap_mode="session"))
        self.assertGreater(len(b), 0)
        self.assertNotEqual(len(a), len(b))

    def test_policy_applies_to_every_symbol_of_a_portfolio(self):
        """La política es la regla de la estrategia, no un ajuste de un
        símbolo: filtra a todos. (Los recuentos de A y B no son idénticos
        porque comparten saldo y A se dimensiona primero.)"""
        p = BacktestParams(equity0=100_000.0, max_gross_lev=1e9)
        _, libre, _ = run_portfolio_backtest({"A": self.df, "B": self.df}, p)
        _, tr, _ = run_portfolio_backtest(
            {"A": self.df, "B": self.df}, p,
            policy=vwap_fade_policy(min_range_n=2.0))
        for s in ("A", "B"):
            self.assertGreater((tr.symbol == s).sum(), 0)
            self.assertLess((tr.symbol == s).sum(), (libre.symbol == s).sum())
        self.assertGreater(tr.attrs["rejected"]["poco_estirado"], 0)


@unittest.skipUnless(os.path.exists(CACHE), "sin caché de BTCUSDT")
class TestRaggedStart(unittest.TestCase):
    """Un símbolo que empieza a cotizar a mitad de la muestra (QQQUSDT listó
    en 2026-04-06) NO se recorta la ventana a todos los demás: simplemente no
    existe hasta su primera barra. Sin esto habría que elegir entre tirar años
    de historia del resto o inventar barras que nunca existieron."""

    @classmethod
    def setUpClass(cls):
        df = pd.read_csv(CACHE, index_col=0, parse_dates=True)
        cls.full = df.loc["2025-01-01":"2025-06-30"]
        cls.late = cls.full.loc["2025-04-01":]

    def test_late_symbol_never_trades_before_its_first_bar(self):
        _, tr, fills = run_portfolio_backtest(
            {"BTC": self.full, "LATE": self.late},
            BacktestParams(equity0=100_000.0, max_gross_lev=1e9))
        late_fills = fills[fills.symbol == "LATE"]
        self.assertGreater(len(late_fills), 0, "el símbolo tardío no operó nunca")
        self.assertGreaterEqual(late_fills.time.min(), self.late.index[0])

    def test_equity_is_continuous_across_the_late_listing(self):
        ec, _, _ = run_portfolio_backtest(
            {"BTC": self.full, "LATE": self.late},
            BacktestParams(equity0=100_000.0, max_gross_lev=1e9))
        self.assertEqual(len(ec), len(self.full))
        self.assertTrue(np.isfinite(ec.to_numpy()).all())
        step = ec.pct_change().abs().dropna()
        self.assertLess(step.max(), 0.5, "salto de equity al entrar el tardío")

    def test_the_late_symbol_does_not_move_the_early_one_s_signals(self):
        """Añadir un símbolo tardío no cambia NINGUNA señal del que ya estaba:
        mismas entradas, mismas salidas, mismos motivos. El tamaño sí cambia,
        y debe cambiar — la cuenta es una sola y el riesgo se mide sobre el
        saldo compartido. Lo que sería un error es que se moviera una fecha:
        significaría que los libros se están contaminando entre sí."""
        p = BacktestParams(equity0=100_000.0, max_gross_lev=1e9)
        _, solo, _ = run_portfolio_backtest({"BTC": self.full}, p)
        _, both, _ = run_portfolio_backtest(
            {"BTC": self.full, "LATE": self.late}, p)
        btc = both[both.symbol == "BTC"].reset_index(drop=True)
        self.assertEqual(len(solo), len(btc))
        for col in ("opened", "closed", "side", "reason"):
            self.assertTrue(solo[col].equals(btc[col]), f"cambió {col}")


@unittest.skipUnless(os.path.exists(CACHE), "sin caché de BTCUSDT")
class TestExcursionAndMaeBarrier(unittest.TestCase):
    """MAE/MFE por operación y la barrera `mae_stop_n`, que separa DÓNDE se
    cierra de CUÁNTO vale 1R."""

    @classmethod
    def setUpClass(cls):
        df = pd.read_csv(CACHE, index_col=0, parse_dates=True)
        cls.df = df.loc["2025-01-01":"2025-06-30"]
        cls.p = dict(equity0=100_000.0, stop_n=2.0, tp_mode="take_n",
                     take_n=50.0, max_hold_bars=20)

    def run_(self, **kw):
        return run_backtest(self.df, BacktestParams(**{**self.p, **kw}))

    def test_barrier_equal_to_stop_is_bit_identical(self):
        ec0, _ = self.run_()
        ec1, _ = self.run_(mae_stop_n=2.0)
        self.assertTrue(np.array_equal(ec0.to_numpy(), ec1.to_numpy()))

    def test_excursions_are_non_negative_and_bounded_by_the_barrier(self):
        _, tr = self.run_()
        self.assertTrue((tr.mae_n >= -1e-12).all())
        self.assertTrue((tr.mfe_n >= -1e-12).all())
        # Un stop corta el camino en el stop: la MAE no puede pasarlo.
        self.assertTrue((tr.mae_n <= 2.0 + 1e-9).all())
        stops = tr[(tr.reason == "stop") & (tr.units == 1)]
        self.assertGreater(len(stops), 5)
        self.assertTrue(np.allclose(stops.mae_n, 2.0))

    def test_no_barrier_never_stops_and_lets_mae_run(self):
        _, tr = self.run_(mae_stop_n=float("inf"))
        self.assertGreater(len(tr), 20)
        self.assertEqual((tr.reason == "stop").sum(), 0)
        self.assertGreater(tr.mae_n.max(), 2.0)

    def test_tighter_barrier_keeps_the_risk_unit(self):
        """Con R = 2N y cierre a 1N, un stop de una unidad pierde ~0,5 R, no
        1 R: la barrera se movió, la unidad de riesgo no."""
        # Costes a cero para aislar la geometría: con comisiones, cada stop
        # paga además ~0,06 R de ida y vuelta.
        _, tr = self.run_(mae_stop_n=1.0, fee_bps=0.0, slip_bps=0.0)
        stops = tr[(tr.reason == "stop") & (tr.units == 1)]
        self.assertGreater(len(stops), 5)
        self.assertTrue(np.allclose(stops.r_multiple, -0.5))
        self.assertTrue((tr.mae_n <= 1.0 + 1e-9).all())
        self.assertTrue((tr.mae_r <= 0.5 + 1e-9).all())


@unittest.skipUnless(os.path.exists(CACHE), "sin caché de BTCUSDT")
class TestRestingOrdersOnFullPath(unittest.TestCase):
    """Señales sobre la sesión, stops en reposo sobre el camino 24/7."""

    @classmethod
    def setUpClass(cls):
        from qsCrypto.backtest.binance_data import restrict_to_session
        df = pd.read_csv(CACHE, index_col=0, parse_dates=True)
        cls.full = df.loc["2025-01-01":"2025-06-30"]
        cls.sess = restrict_to_session(cls.full)
        cls.p = BacktestParams(equity0=100_000.0, tp_mode="take_n", take_n=50.0)

    def test_path_without_gaps_changes_nothing(self):
        ec0, _ = run_backtest(self.sess, self.p)
        ec1, _ = run_backtest(self.sess, self.p, path=self.sess)
        self.assertTrue(np.array_equal(ec0.to_numpy(), ec1.to_numpy()))
        ec2, _ = run_backtest(self.full, self.p)
        ec3, _ = run_backtest(self.full, self.p, path=self.full)
        self.assertTrue(np.array_equal(ec2.to_numpy(), ec3.to_numpy()))

    def test_overnight_stops_are_real_touches_outside_the_session(self):
        _, tr = run_backtest(self.sess, self.p, path=self.full)
        sf = tr[tr.reason == "stop_fuera"]
        self.assertGreater(len(sf), 5)
        for _, t in sf.iterrows():
            self.assertNotIn(t.closed, self.sess.index)
            b = self.full.loc[t.closed]
            if t.side == "long":
                self.assertLessEqual(b.low, t.stop_at_exit + 1e-9)
            else:
                self.assertGreaterEqual(b.high, t.stop_at_exit - 1e-9)

    def test_blind_session_never_reports_an_overnight_exit(self):
        _, tr = run_backtest(self.sess, self.p)
        self.assertEqual((tr.reason == "stop_fuera").sum(), 0)

    def test_portfolio_accepts_paths(self):
        _, tr, _ = run_portfolio_backtest({"BTC": self.sess}, self.p,
                                          paths={"BTC": self.full})
        self.assertGreater((tr.reason == "stop_fuera").sum(), 0)


class TestDrawdownFromPeak(unittest.TestCase):
    def test_rule_is_blind_above_equity0_by_default(self):
        from qsCrypto.backtest.engine import _sizing_equity
        # Cuenta que subió a 1M y cayó a 500k: con la regla histórica, al
        # estar por encima de 100k, no reduce nada.
        self.assertEqual(_sizing_equity(500_000, 100_000, True), 500_000)

    def test_peak_reference_cuts_after_a_fall_from_the_top(self):
        from qsCrypto.backtest.engine import _sizing_equity
        # -50 % desde 1M = 5 tramos de 10 % -> 0,8^5
        self.assertAlmostEqual(_sizing_equity(500_000, 100_000, True, ref=1_000_000),
                               500_000 * 0.8 ** 5)

    def test_peak_tracks_the_balance(self):
        from qsCrypto.backtest.engine import _Account
        a = _Account(BacktestParams(equity0=100.0, dd_from_peak=True))
        a.balance = 300.0; self.assertEqual(a.dd_ref(), 300.0)
        a.balance = 150.0; self.assertEqual(a.dd_ref(), 300.0)
        a2 = _Account(BacktestParams(equity0=100.0))
        a2.balance = 300.0; self.assertIsNone(a2.dd_ref())


if __name__ == "__main__":
    unittest.main()


class TestFlatAtSessionEnd(unittest.TestCase):
    """Opción (a): la posición nace y muere dentro de la sesión."""

    @classmethod
    def setUpClass(cls):
        from dataclasses import replace
        from qsCrypto.backtest.binance_data import restrict_to_session
        df = pd.read_csv(CACHE, index_col=0, parse_dates=True)
        cls.full = df.loc["2025-01-01":"2025-06-30"]
        cls.sess = restrict_to_session(cls.full)
        cls.p = BacktestParams(equity0=100_000.0, tp_mode="take_n", take_n=50.0)
        cls.pf = replace(cls.p, flat_at_session_end=True)
        cls.ec, cls.tr = run_backtest(cls.sess, cls.pf, path=cls.full)

    def _day(self, t):
        return pd.Timestamp(t).tz_localize("UTC").tz_convert("America/New_York").date()

    def test_no_trade_crosses_the_night(self):
        self.assertGreater(len(self.tr), 20)
        for _, t in self.tr.iterrows():
            self.assertEqual(self._day(t.opened), self._day(t.closed))
        # Sin cruzar la noche no hay nada que el camino 24/7 pueda tocar.
        self.assertEqual(self.tr.reason.isin(["stop_fuera", "target_fuera"]).sum(), 0)

    def test_session_close_exits_on_the_last_bar_at_its_close(self):
        cs = self.tr[self.tr.reason == "cierre_sesion"]
        self.assertGreater(len(cs), 0)
        idx = self.sess.index
        for _, t in cs.iterrows():
            k = idx.get_loc(t.closed)
            self.assertTrue(k == len(idx) - 1
                            or idx[k + 1] - idx[k] > pd.Timedelta("30min"))
            px = self.sess.close.iloc[k]
            slip = self.pf.slip_bps
            expected = px * (1 - slip) if t.side == "long" else px * (1 + slip)
            self.assertAlmostEqual(t.exit, expected, places=6)

    def test_no_entry_on_the_last_session_bar(self):
        idx = self.sess.index
        for _, t in self.tr.iterrows():
            k = idx.get_loc(t.opened)
            self.assertFalse(k < len(idx) - 1
                             and idx[k + 1] - idx[k] > pd.Timedelta("30min"))
        self.assertIn("fin_sesion", self.tr.attrs["rejected"])

    def test_off_by_default_is_unchanged(self):
        ec0, _ = run_backtest(self.sess, self.p, path=self.full)
        ec1, _ = run_backtest(self.sess, BacktestParams(
            equity0=100_000.0, tp_mode="take_n", take_n=50.0,
            flat_at_session_end=False), path=self.full)
        self.assertTrue(np.array_equal(ec0.to_numpy(), ec1.to_numpy()))
