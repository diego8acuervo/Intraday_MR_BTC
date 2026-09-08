"""Tests de IBKRFuturesPortfolio.

El invariante que se fija aquí es el que se rompe en silencio si alguien
"simplifica" el sizing: una unidad que se mueve 1R tiene que costar exactamente
`risk_per_trade` del equity, y un futuro se compra en contratos ENTEROS.
"""
import queue
import unittest
from decimal import Decimal

from qsCrypto.event.event import BarEvent, FillEvent, SignalEvent
from qsCrypto.portfolio.portfolio import IBKRFuturesPortfolio


class TickerMock(object):
    def __init__(self, pairs, price=100000.0):
        self.pairs = pairs
        self.prices = {
            p: {"bid": Decimal(str(price)), "ask": Decimal(str(price)),
                "time": None}
            for p in pairs
        }


SPECS = {"MBT": {"multiplier": 0.1, "minTick": 5.0}}


def make(**kw):
    q = queue.Queue()
    t = TickerMock(["MBT"])
    p = IBKRFuturesPortfolio(
        t, q, home_currency="USD", equity=Decimal("100000.00"),
        risk_per_trade=Decimal("0.01"), backtest=False, specs=SPECS,
        use_dd_rule=False, **kw
    )
    return p, q, t


class TestSizing(unittest.TestCase):
    def test_whole_contracts_from_risk_and_multiplier(self):
        """floor(0.01 * 100000 / (1.0 * N * 0.1)) con N = 500 -> 20."""
        p, _, _ = make(stop_n=1.0)
        self.assertEqual(p.calc_risk_position_size("MBT", n=500.0), 20)

    def test_floor_never_rounds_up(self):
        """Redondear hacia arriba excedería el riesgo declarado en silencio."""
        p, _, _ = make(stop_n=1.0)
        # 1000 / (1*333*0.1) = 30.03 -> 30, no 31
        self.assertEqual(p.calc_risk_position_size("MBT", n=333.0), 30)

    def test_stop_n_scales_size_inversely(self):
        p1, _, _ = make(stop_n=1.0)
        p2, _, _ = make(stop_n=2.0)
        self.assertEqual(p1.calc_risk_position_size("MBT", n=500.0), 20)
        self.assertEqual(p2.calc_risk_position_size("MBT", n=500.0), 10)

    def test_multiplier_is_applied(self):
        """Sin multiplicador, MBT (0.1 BTC) daría 10x el tamaño correcto."""
        p, _, _ = make(stop_n=1.0)
        p.specs = {"MBT": {"multiplier": 1.0, "minTick": 5.0}}
        self.assertEqual(p.calc_risk_position_size("MBT", n=500.0), 2)

    def test_zero_when_a_single_contract_does_not_fit(self):
        p, _, _ = make(stop_n=1.0)
        self.assertEqual(p.calc_risk_position_size("MBT", n=1e9), 0)

    def test_nan_or_missing_n_returns_zero(self):
        p, _, _ = make(stop_n=1.0)
        self.assertEqual(p.calc_risk_position_size("MBT", n=None), 0)
        self.assertEqual(p.calc_risk_position_size("MBT", n=float("nan")), 0)
        self.assertEqual(p.calc_risk_position_size("MBT", n=-5.0), 0)

    def test_risk_per_unit_equals_declared_risk(self):
        """El invariante completo: qty * R * multiplicador ~= 1% del equity."""
        p, _, _ = make(stop_n=1.0)
        n = 500.0
        qty = p.calc_risk_position_size("MBT", n=n)
        risk_usd = qty * (p.stop_n * n) * p.multiplier("MBT")
        self.assertLessEqual(risk_usd, 0.01 * 100000)
        self.assertGreater(risk_usd, 0.009 * 100000)   # el floor no puede comerse más


class TestDrawdownRule(unittest.TestCase):
    def test_notional_shrinks_after_losses(self):
        p, _, _ = make(stop_n=1.0)
        p.use_dd_rule = True
        full = p.calc_risk_position_size("MBT", n=500.0)
        p.balance = Decimal("80000.00")      # -20% -> dos escalones
        reduced = p.calc_risk_position_size("MBT", n=500.0)
        self.assertLess(reduced, full)

    def test_floor_at_five_percent(self):
        p, _, _ = make(stop_n=1.0)
        p.use_dd_rule = True
        p.balance = Decimal("1000.00")
        self.assertGreaterEqual(p.sizing_equity(), 100000 * 0.05 - 1e-9)


class TestSignalToOrder(unittest.TestCase):
    def test_signal_sets_r_and_emits_order_with_stop(self):
        """El fallo que esto blinda: sin initial_stop, mae_r es None SIEMPRE."""
        p, q, _ = make(stop_n=1.0)
        p.execute_signal(SignalEvent("MBT", "market", "buy", "t",
                                     n=500.0, level=101000.0, mid=99000.0))
        pos = p.positions["MBT"]
        self.assertIsNotNone(pos.r_unit)
        self.assertIsNotNone(pos.calculate_mae_r())
        self.assertEqual(float(pos.r_unit), 500.0)
        order = q.get()
        self.assertEqual(order.type, "ORDER")
        self.assertEqual(order.units, 20)
        self.assertAlmostEqual(order.stop_price, 99500.0, places=6)

    def test_zero_qty_signal_is_rejected(self):
        p, q, _ = make(stop_n=1.0)
        p.execute_signal(SignalEvent("MBT", "market", "buy", "t", n=1e9))
        self.assertNotIn("MBT", p.positions)
        self.assertEqual(p.rejected["qty_cero"], 1)
        self.assertTrue(q.empty())

    def test_second_signal_ignored_while_position_open(self):
        p, q, _ = make(stop_n=1.0)
        sig = SignalEvent("MBT", "market", "buy", "t", n=500.0, mid=99000.0)
        p.execute_signal(sig)
        n_before = q.qsize()
        p.execute_signal(sig)
        self.assertEqual(q.qsize(), n_before)


class TestBarriers(unittest.TestCase):
    def _open(self, stop_n=1.0):
        p, q, t = make(stop_n=stop_n, max_hold_bars=3)
        p.execute_signal(SignalEvent("MBT", "market", "buy", "t",
                                     n=500.0, level=101000.0, mid=101000.0))
        while not q.empty():
            q.get()
        return p, q, t

    def test_stop_wins_when_both_touched_in_one_bar(self):
        """Prelación pesimista: sin datos intrabarra, gana la barrera adversa."""
        p, _, _ = self._open()
        p.on_bar(BarEvent("MBT", 1, 100000.0, 102000.0, 99000.0, 100500.0, 10))
        self.assertNotIn("MBT", p.positions)

    def test_target_at_channel_mid(self):
        p, _, _ = self._open()
        p.on_bar(BarEvent("MBT", 1, 100000.0, 101500.0, 99900.0, 101200.0, 10))
        self.assertNotIn("MBT", p.positions)

    def test_vertical_barrier_after_max_hold_bars(self):
        p, _, _ = self._open()
        quiet = (100000.0, 100100.0, 99900.0, 100000.0)
        for i in range(3):
            self.assertIn("MBT", p.positions)
            p.on_bar(BarEvent("MBT", i, *quiet, 10))
        self.assertNotIn("MBT", p.positions)

    def test_flat_bar_touches_nothing_but_advances_the_clock(self):
        p, _, _ = self._open()
        p.on_bar(BarEvent("MBT", 1, 99000.0, 99000.0, 99000.0, 99000.0, 0))
        self.assertIn("MBT", p.positions)          # no salta el stop
        self.assertEqual(p.meta["MBT"]["bars"], 1)  # pero cuenta


class TestFillReconciliation(unittest.TestCase):
    def test_fill_reanchors_to_the_real_price(self):
        p, q, _ = make(stop_n=1.0)
        p.execute_signal(SignalEvent("MBT", "market", "buy", "t",
                                     n=500.0, mid=99000.0))
        p.update_fill(FillEvent("MBT", 20, "buy", 100050.0, time="t"))
        self.assertAlmostEqual(p.meta["MBT"]["last_fill"], 100050.0, places=6)
        self.assertAlmostEqual(p.meta["MBT"]["stop"], 99550.0, places=6)
        self.assertAlmostEqual(
            float(p.positions["MBT"].entry_anchor), 100050.0, places=6)

    def test_fill_without_local_position_is_survived(self):
        p, _, _ = make(stop_n=1.0)
        p.update_fill(FillEvent("MBT", 5, "buy", 100.0))   # no debe reventar


class TestSnapshot(unittest.TestCase):
    def test_snapshot_reports_excursions_in_r(self):
        p, _, t = make(stop_n=1.0)
        p.execute_signal(SignalEvent("MBT", "market", "buy", "t",
                                     n=500.0, mid=99000.0))
        t.prices["MBT"]["bid"] = Decimal("99800.00")
        t.prices["MBT"]["ask"] = Decimal("99800.00")
        p.positions["MBT"].update_position_price()
        row = p.snapshot()[0]
        self.assertEqual(row["pair"], "MBT")
        self.assertIsNotNone(row["mae_r"])
        self.assertIsNotNone(row["r_unit"])
        self.assertAlmostEqual(row["mae_r"], 0.4, places=6)   # 200 puntos / 500


if __name__ == "__main__":
    unittest.main()
