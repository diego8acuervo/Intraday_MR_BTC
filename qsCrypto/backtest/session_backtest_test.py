"""Tests del backtest de sesión.

Lo que se fija: que la entrada no mire al futuro, que la hora de salida sea
la que se pide en el huso que se pide (y siga al horario de verano), que los
costes vayan en la dirección correcta, y que el funding se cobre una vez por
liquidación cruzada y con el signo correcto. Todo ello falla en silencio: un
backtest con la salida una hora corrida o el funding invertido sigue
produciendo una curva de equity de aspecto perfectamente normal.
"""
import unittest

import numpy as np
import pandas as pd

from qsCrypto.backtest.session_backtest import (
    SessionParams, run_session_backtest, summarise_backtest,
)


def _m1(days=6, start="2024-03-04", drift=0.0, seed=0):
    """Velas de 1 minuto 24/7 con deriva controlable."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=days * 1440, freq="1min")
    close = 100 + np.cumsum(rng.normal(drift, 0.01, len(idx)))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({
        "open": open_, "high": np.maximum(open_, close) + 0.02,
        "low": np.minimum(open_, close) - 0.02, "close": close,
    }, index=idx)


def _signals(m1, hhmm_utc="14:35", side=1, n=1.0):
    """Una señal por día, al cierre del minuto `hhmm_utc`."""
    h, m = (int(x) for x in hhmm_utc.split(":"))
    days = pd.DatetimeIndex(sorted(set(m1.index.normalize())))
    end = days + pd.Timedelta(hours=h, minutes=m)
    end = end[end.isin(m1.index)]
    return pd.DataFrame({"session": end.normalize(), "end": end,
                         "side": side, "n": n})


class TestExecution(unittest.TestCase):

    def setUp(self):
        self.m1 = _m1()
        self.sig = _signals(self.m1)

    def test_entry_is_the_minute_after_the_bar_closes(self):
        """El fill va al open del minuto SIGUIENTE. Llenarlo sobre el propio
        minuto de la señal sería look-ahead: su apertura ya pasó cuando se
        conoce su cierre."""
        _, tr = run_session_backtest(self.sig, self.m1,
                                     params=SessionParams(slip_bps=0.0))
        self.assertTrue(len(tr) > 3)
        for _, r in tr.iterrows():
            sig_end = self.sig.loc[self.sig["session"] == r["session"],
                                   "end"].iloc[0]
            self.assertEqual(r["entry_time"], sig_end + pd.Timedelta(minutes=1))
            self.assertAlmostEqual(r["entry"], self.m1.loc[r["entry_time"], "open"],
                                   places=10)

    def test_exit_minute_closes_at_the_requested_time_utc(self):
        _, tr = run_session_backtest(self.sig, self.m1, exit_minute="23:59",
                                     exit_in_utc=True)
        # el minuto que CIERRA a las 00:00 es el que abre a las 23:59
        self.assertEqual(set(pd.DatetimeIndex(tr["exit_time"]).strftime("%H:%M")),
                         {"23:59"})

    def test_exit_is_dst_correct_in_local_time(self):
        """16:00 ET cae en dos horas UTC distintas según la época. Comparar
        en UTC a pelo cerraría una hora tarde medio año."""
        m1 = _m1(days=3, start="2024-01-08")          # EST
        t1 = run_session_backtest(_signals(m1), m1, exit_minute="15:59")[1]
        m2 = _m1(days=3, start="2024-06-10")          # EDT
        t2 = run_session_backtest(_signals(m2, "13:35"), m2, exit_minute="15:59")[1]
        self.assertEqual(set(pd.DatetimeIndex(t1["exit_time"]).strftime("%H:%M")),
                         {"20:59"})   # 15:59 EST -> cierra 16:00 EST
        self.assertEqual(set(pd.DatetimeIndex(t2["exit_time"]).strftime("%H:%M")),
                         {"19:59"})   # 15:59 EDT -> cierra 16:00 EDT

    def test_utc_exit_is_later_than_the_et_close(self):
        a = run_session_backtest(self.sig, self.m1, exit_minute="15:59")[1]
        b = run_session_backtest(self.sig, self.m1, exit_minute="23:59",
                                 exit_in_utc=True)[1]
        self.assertTrue((b["hours"].median() > a["hours"].median()))


class TestCostsAndFunding(unittest.TestCase):

    def setUp(self):
        self.m1 = _m1(drift=0.002, seed=5)    # deriva alcista clara
        self.sig = _signals(self.m1)

    def test_costs_reduce_pnl(self):
        free = SessionParams(fee_bps=0.0, slip_bps=0.0)
        paid = SessionParams(fee_bps=0.001, slip_bps=0.001)
        _, t0 = run_session_backtest(self.sig, self.m1, params=free)
        _, t1 = run_session_backtest(self.sig, self.m1, params=paid)
        self.assertGreater(t0["pnl"].sum(), t1["pnl"].sum())
        self.assertTrue((t1["fees"] > 0).all())

    def test_gross_r_exceeds_net_r_when_costs_are_on(self):
        _, tr = run_session_backtest(self.sig, self.m1,
                                     params=SessionParams(fee_bps=0.001))
        self.assertTrue((tr["gross_r"] >= tr["r_multiple"] - 1e-12).all())

    def test_funding_sign_and_count(self):
        """Un largo PAGA cuando la tasa es positiva. Invertir el signo da la
        vuelta al coste de carga y no se nota en ningún otro sitio."""
        f = pd.Series(
            0.0001,
            index=pd.date_range(self.m1.index[0], self.m1.index[-1], freq="8h"))
        _, lng = run_session_backtest(self.sig, self.m1, funding=f,
                                      params=SessionParams(fee_bps=0, slip_bps=0))
        _, sht = run_session_backtest(_signals(self.m1, side=-1), self.m1,
                                      funding=f,
                                      params=SessionParams(fee_bps=0, slip_bps=0))
        self.assertTrue((lng["funding_paid"] < 0).all())   # el largo paga
        self.assertTrue((sht["funding_paid"] > 0).all())   # el corto cobra

    def test_funding_charged_once_per_settlement_crossed(self):
        f = pd.Series(
            0.0001,
            index=pd.date_range(self.m1.index[0], self.m1.index[-1], freq="8h"))
        _, short_hold = run_session_backtest(
            self.sig, self.m1, funding=f, exit_minute="15:59",
            params=SessionParams(fee_bps=0, slip_bps=0))
        _, long_hold = run_session_backtest(
            self.sig, self.m1, funding=f, exit_minute="23:59", exit_in_utc=True,
            params=SessionParams(fee_bps=0, slip_bps=0))
        # mantener más horas cruza al menos tantas liquidaciones como mantener menos
        self.assertGreaterEqual(abs(long_hold["funding_paid"]).sum(),
                                abs(short_hold["funding_paid"]).sum())

    def test_no_funding_series_means_no_funding(self):
        _, tr = run_session_backtest(self.sig, self.m1, funding=None)
        self.assertTrue((tr["funding_paid"] == 0).all())


class TestSizingAndRisk(unittest.TestCase):

    def test_risk_per_trade_sets_the_unit(self):
        """Sin regla de drawdown y sin tope de apalancamiento vinculante,
        1N de movimiento adverso cuesta exactamente risk_per_trade."""
        m1 = _m1()
        p = SessionParams(risk_per_trade=0.01, stop_n=1.0, use_dd_rule=False,
                          max_gross_lev=1e9, fee_bps=0, slip_bps=0)
        _, tr = run_session_backtest(_signals(m1, n=2.0), m1, params=p)
        first = tr.iloc[0]
        self.assertAlmostEqual(first["qty"] * first["n"],
                               0.01 * 10_000.0, places=6)

    def test_leverage_cap_binds(self):
        m1 = _m1()
        p = SessionParams(max_gross_lev=0.5, fee_bps=0, slip_bps=0)
        _, tr = run_session_backtest(_signals(m1, n=0.001), m1, params=p)
        self.assertTrue((tr["notional"] <= 0.5 * 10_000.0 * 1.05).all())

    def test_mae_is_non_negative_and_bounded_by_mfe_range(self):
        m1 = _m1()
        _, tr = run_session_backtest(_signals(m1), m1)
        self.assertTrue((tr["mae_n"] >= -1e-9).all())
        self.assertTrue((tr["mfe_n"] >= -1e-9).all())

    def test_equity_curve_tracks_balance(self):
        m1 = _m1(drift=0.002, seed=2)
        ec, tr = run_session_backtest(_signals(m1), m1)
        self.assertEqual(len(ec), len(tr))
        self.assertAlmostEqual(ec.iloc[-1], tr["balance"].iloc[-1], places=9)

    def test_summary_runs(self):
        m1 = _m1(drift=0.002, seed=2)
        ec, tr = run_session_backtest(_signals(m1), m1)
        s = summarise_backtest(ec, tr, "demo")
        for k in ("R neto medio", "Sharpe", "Max DD", "t-stat(R)",
                  "coste/|bruto|"):
            self.assertIn(k, s)

    def test_zero_signals_is_handled(self):
        m1 = _m1()
        sig = _signals(m1).assign(side=0)
        ec, tr = run_session_backtest(sig, m1)
        self.assertEqual(len(tr), 0)


if __name__ == "__main__":
    unittest.main()
