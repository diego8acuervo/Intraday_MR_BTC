"""Tests de OpeningRangeVwapRule y de su paridad con la estrategia en vivo.

Lo que se fija aquí es lo que un backtest de esta estrategia puede falsear
sin que se note: que la señal se lea en la barra de apertura y no en otra,
que el ancla y la N salgan del marco 24/7 y no de la serie filtrada, que
nada de lo que entra en la decisión sea posterior a ella, y que la regla del
motor y la clase en vivo den literalmente el mismo lado sobre los mismos
datos.
"""
import os
import queue
import unittest

import numpy as np
import pandas as pd

from qsCrypto.backtest.binance_data import restrict_to_session
from qsCrypto.backtest.engine import (
    BacktestParams, OpeningRangeVwapRule, run_backtest,
)
from qsCrypto.event.event import BarEvent
from qsCrypto.strategy.strategy import (
    IntradayMomentumStrategy, opening_range_signal, rolling_range_n,
)

CACHE = os.path.join(os.path.dirname(__file__), "..", "..", "data_cache",
                     "binance", "5m", "BTCUSDT.csv")

PARAMS = BacktestParams(
    atr_period=288, stop_n=1.0, mae_stop_n=float("inf"), max_units=1,
    max_hold_bars=10 ** 9, tp_mode="take_n", take_n=1e9,
    flat_at_session_end=True, risk_per_trade=0.01, equity0=10_000.0,
    fee_bps=0.0002, slip_bps=0.0001, dd_from_peak=True)


def _synthetic(days=40, tz_hour=14):
    """Serie 24/7 de 5 minutos, determinista, con sesiones reconocibles."""
    idx = pd.date_range("2024-01-01", periods=days * 288, freq="5min")
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 0.05, len(idx)))
    high = close + 0.10
    low = close - 0.10
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": 1000.0}, index=idx)


class TestOpeningRangeRule(unittest.TestCase):

    def setUp(self):
        self.ctx = _synthetic()
        self.df = restrict_to_session(self.ctx, start="09:30", end="15:45")

    def test_one_signal_bar_per_session(self):
        _, tr = run_backtest(self.df, PARAMS, rule=OpeningRangeVwapRule(or_bars=1),
                             ctx=self.ctx)
        rule = tr.attrs["rule"]
        marks = pd.Series(rule.signal_bar, index=self.df.index)
        per_day = marks.groupby(marks.index.date).sum()
        self.assertTrue((per_day == 1).all())
        # Y es SIEMPRE la primera barra de la sesión.
        firsts = self.df.groupby(self.df.index.date).head(1).index
        self.assertTrue(marks.loc[firsts].all())

    def test_or_bars_shifts_the_signal_bar(self):
        """Con or_bars=3 la señal se lee en la tercera barra y la apertura
        del rango sigue siendo la de la PRIMERA barra de esa sesión."""
        _, tr = run_backtest(self.df, PARAMS, rule=OpeningRangeVwapRule(or_bars=3),
                             ctx=self.ctx)
        rule = tr.attrs["rule"]
        marks = pd.Series(rule.signal_bar, index=self.df.index)
        self.assertTrue((marks.groupby(marks.index.date).sum() == 1).all())
        i = int(np.flatnonzero(rule.signal_bar)[3])
        day = self.df.index[i].date()
        first_open = self.df[self.df.index.date == day]["open"].iloc[0]
        self.assertAlmostEqual(rule.or_open[i], first_open, places=12)
        # la tercera barra de su sesión
        self.assertEqual((self.df.index.date == day).argmax() + 2, i)

    def test_anchor_comes_from_the_24_7_frame(self):
        """El ancla calculada sobre `ctx` difiere de la que saldría de `df`.

        Es la diferencia entre "el VWAP de las últimas 24 h" y "el de las
        últimas 288 barras de sesión" (casi cuatro días de solo horario
        regular). Si esto empatara, el filtro de sesión sería inocuo y el
        `ctx` sobraría."""
        _, a_tr = run_backtest(self.df, PARAMS, rule=OpeningRangeVwapRule(),
                               ctx=self.ctx)
        _, b_tr = run_backtest(self.df, PARAMS, rule=OpeningRangeVwapRule(),
                               ctx=self.df)
        rule, only_session = a_tr.attrs["rule"], b_tr.attrs["rule"]
        a = pd.Series(rule.anchor).dropna()
        b = pd.Series(only_session.anchor).dropna()
        common = a.index.intersection(b.index)
        self.assertTrue(len(common) > 50)
        self.assertFalse(np.allclose(a.loc[common], b.loc[common]))

    def test_ctx_is_required_and_must_contain_df(self):
        with self.assertRaises(ValueError):
            run_backtest(self.df, PARAMS, rule=OpeningRangeVwapRule())
        foreign = self.ctx.iloc[:100].copy()
        foreign.index = foreign.index + pd.Timedelta("1s")
        with self.assertRaises(ValueError):
            run_backtest(self.df, PARAMS, rule=OpeningRangeVwapRule(),
                         ctx=foreign)

    def test_no_lookahead_truncating_the_context(self):
        """Truncar `ctx` DESPUÉS de una barra de señal no cambia ni el ancla
        ni la N en esa barra. Si cambiaran, la decisión estaría usando velas
        posteriores a sí misma."""
        _, ftr = run_backtest(self.df, PARAMS, rule=OpeningRangeVwapRule(),
                              ctx=self.ctx)
        full = ftr.attrs["rule"]
        sig = np.flatnonzero(full.signal_bar & np.isfinite(full.anchor))
        i = int(sig[len(sig) // 2])
        t = self.df.index[i]
        cut_ctx = self.ctx.loc[:t]
        cut_df = self.df.loc[:t]
        _, ctr = run_backtest(cut_df, PARAMS, rule=OpeningRangeVwapRule(),
                              ctx=cut_ctx)
        cut = ctr.attrs["rule"]
        self.assertAlmostEqual(full.anchor[i], cut.anchor[-1], places=10)
        self.assertAlmostEqual(full.n_ctx[i], cut.n_ctx[-1], places=10)

    def test_entry_fills_on_the_bar_after_the_signal(self):
        _, tr = run_backtest(self.df, PARAMS, rule=OpeningRangeVwapRule(),
                             ctx=self.ctx)
        rule = tr.attrs["rule"]
        self.assertTrue(len(tr) > 10)
        sig_times = self.df.index[np.asarray(rule.signal_bar)]
        step = pd.Timedelta(minutes=5)
        for t in tr["opened"]:
            self.assertIn(t - step, set(sig_times))

    def test_every_exit_is_the_session_close(self):
        _, tr = run_backtest(self.df, PARAMS, rule=OpeningRangeVwapRule(),
                             ctx=self.ctx, path=self.ctx)
        self.assertEqual(set(tr["reason"]), {"cierre_sesion"})
        # ...y ninguna posición cruza la noche.
        self.assertTrue((pd.to_datetime(tr["closed"]).dt.date
                         == pd.to_datetime(tr["opened"]).dt.date).all())

    def test_sides_follow_opening_range_signal(self):
        _, tr = run_backtest(self.df, PARAMS, rule=OpeningRangeVwapRule(),
                             ctx=self.ctx)
        rule = tr.attrs["rule"]
        opened = set(pd.to_datetime(tr["opened"]))
        step = pd.Timedelta(minutes=5)
        want = {}
        for i in np.flatnonzero(rule.signal_bar):
            side = opening_range_signal(rule.or_open[i], self.df["close"].iloc[i],
                                        rule.anchor[i], rule.n_ctx[i])
            if side is not None:
                want[self.df.index[i] + step] = "long" if side > 0 else "short"
        got = dict(zip(pd.to_datetime(tr["opened"]), tr["side"]))
        self.assertEqual(set(got), set(want) & opened)
        for t, s in got.items():
            self.assertEqual(s, want[t])


class TestLiveBacktestParity(unittest.TestCase):
    """La clase en vivo y la regla del motor sobre los MISMOS datos.

    Es la comprobación que impide la divergencia silenciosa entre lo que se
    valida y lo que se opera: ambas llaman a `opening_range_signal`, pero por
    caminos distintos (búfer de barras contra arrays precalculados).
    """

    def test_same_signals_and_sides(self):
        ctx = _synthetic()
        df = restrict_to_session(ctx, start="09:30", end="15:45")
        _, tr = run_backtest(df, PARAMS, rule=OpeningRangeVwapRule(), ctx=ctx)
        rule = tr.attrs["rule"]

        events = queue.Queue()
        strat = IntradayMomentumStrategy(["BTCUSDT"], events, max_history=2000)
        for ts, r in ctx.iterrows():       # la estrategia ve el marco 24/7
            strat.calculate_signals(BarEvent("BTCUSDT", ts, r["open"], r["high"],
                                             r["low"], r["close"], r["volume"],
                                             complete=True))
        live = {}
        while not events.empty():
            e = events.get()
            live[e.time] = e.side

        bt = {}
        for i in np.flatnonzero(rule.signal_bar):
            side = opening_range_signal(rule.or_open[i], df["close"].iloc[i],
                                        rule.anchor[i], rule.n_ctx[i])
            if side is not None:
                bt[df.index[i]] = "buy" if side > 0 else "sell"

        self.assertTrue(len(bt) > 10)
        self.assertEqual(set(live), set(bt))
        self.assertEqual(live, bt)

    def test_live_n_matches_rolling_range_n(self):
        ctx = _synthetic(days=5)
        strat = IntradayMomentumStrategy(["X"], queue.Queue(), max_history=2000)
        bars = [{"time": ts, "open": r["open"], "high": r["high"],
                 "low": r["low"], "close": r["close"], "volume": r["volume"]}
                for ts, r in ctx.iterrows()]
        want = rolling_range_n(ctx["high"], ctx["low"], window=288)[-1]
        self.assertAlmostEqual(strat._n(bars), want, places=12)


@unittest.skipUnless(os.path.exists(CACHE), "sin caché de 5m de BTCUSDT")
class TestOnRealData(unittest.TestCase):

    def test_sessions_are_complete_and_signal_is_at_0935(self):
        ctx = pd.read_csv(CACHE, index_col=0, parse_dates=True)
        df = restrict_to_session(ctx, start="09:30", end="15:45")
        sizes = df.groupby(df.index.date).size()
        self.assertEqual(sizes.unique().tolist(), [75])   # 09:30->15:45 en 5m
        _, tr = run_backtest(df, PARAMS, ctx=ctx, rule=OpeningRangeVwapRule(),
                             path=ctx)
        local = (pd.to_datetime(tr["opened"]).dt.tz_localize("UTC")
                 .dt.tz_convert("America/New_York"))
        self.assertEqual(set(local.dt.strftime("%H:%M")), {"09:35"})
        closed = (pd.to_datetime(tr["closed"]).dt.tz_localize("UTC")
                  .dt.tz_convert("America/New_York"))
        self.assertEqual(set(closed.dt.strftime("%H:%M")), {"15:40"})

    def test_n_is_horizon_scaled_not_bar_scaled(self):
        """La N debe estar en el orden del recorrido de una sesión, no en el
        de una vela de 5 minutos: es lo que hacía que R saliera en +-20."""
        ctx = pd.read_csv(CACHE, index_col=0, parse_dates=True)
        df = restrict_to_session(ctx, start="09:30", end="15:45")
        _, tr = run_backtest(df, PARAMS, ctx=ctx, rule=OpeningRangeVwapRule(),
                             path=ctx)
        self.assertLess(tr["r_multiple"].abs().max(), 6.0)
        self.assertGreater(tr["r_multiple"].abs().median(), 0.05)


if __name__ == "__main__":
    unittest.main()
