"""Tests del calendario de sesiones y de la resolución de CFDs.

Las cadenas de horario son las que devolvió TWS para IBIT (CFD y acción) el
2026-09-11: sesión regular 09:30-16:00 US/Eastern, sábado y domingo CLOSED.
"""
import queue
import unittest
from datetime import datetime
from types import SimpleNamespace

from qsCrypto.ibkr.contracts import resolve_instrument
from qsCrypto.ibkr.sessions import TradingSessions, floor_to

IBIT_LIQUID = ("20260911:0930-20260911:1600;20260912:CLOSED;20260913:CLOSED;"
               "20260914:0930-20260914:1600;20260915:0930-20260915:1600")


class TestTradingSessions(unittest.TestCase):
    def setUp(self):
        self.s = TradingSessions.from_hours(IBIT_LIQUID, "US/Eastern")

    def test_eastern_daylight_time_maps_to_1330_2000_utc(self):
        self.assertEqual(len(self.s), 3)
        self.assertEqual(self.s.sessions[0],
                         (datetime(2026, 9, 11, 13, 30), datetime(2026, 9, 11, 20, 0)))

    def test_standard_time_shifts_one_hour(self):
        s = TradingSessions.from_hours("20261214:0930-20261214:1600", "US/Eastern")
        self.assertEqual(s.sessions[0][0], datetime(2026, 12, 14, 14, 30))

    def test_open_closed_and_weekend(self):
        self.assertTrue(self.s.is_open(datetime(2026, 9, 11, 14, 44)))
        self.assertFalse(self.s.is_open(datetime(2026, 9, 11, 20, 0)))   # [inicio, fin)
        self.assertFalse(self.s.is_open(datetime(2026, 9, 12, 15, 0)))   # sábado
        self.assertEqual(self.s.next_open(datetime(2026, 9, 12, 15, 0)),
                         datetime(2026, 9, 14, 13, 30))
        self.assertEqual(self.s.last_close(datetime(2026, 9, 13, 0, 0)),
                         datetime(2026, 9, 11, 20, 0))

    def test_horizon(self):
        self.assertTrue(self.s.covers(datetime(2026, 9, 15, 19, 0)))
        self.assertFalse(self.s.covers(datetime(2026, 9, 16, 12, 0)))

    def test_old_format_and_alias_timezone(self):
        s = TradingSessions.from_hours("20260911:0930-1600", "EST (Eastern Standard Time)")
        self.assertEqual(s.sessions[0][1], datetime(2026, 9, 11, 20, 0))

    def test_floor_to_half_hour(self):
        self.assertEqual(floor_to(datetime(2026, 9, 11, 14, 44, 9), 1800),
                         datetime(2026, 9, 11, 14, 30))


class FakeIB(object):
    def __init__(self, by_conid, by_symbol):
        self.by_conid, self.by_symbol = by_conid, by_symbol

    def reqContractDetails(self, c):
        if c.conId:
            return self.by_conid.get(c.conId, [])
        return self.by_symbol.get((c.secType, c.symbol), [])


def detail(conId, secType, local, exchange, under=0):
    return SimpleNamespace(
        contract=SimpleNamespace(conId=conId, secType=secType, localSymbol=local,
                                 exchange=exchange, multiplier=""),
        underConId=under, minTick=0.01, liquidHours=IBIT_LIQUID,
        tradingHours=IBIT_LIQUID, timeZoneId="US/Eastern")


class TestResolveInstrument(unittest.TestCase):
    def test_cfd_trades_the_cfd_and_takes_bars_from_the_underlying_on_smart(self):
        cfd = detail(699185298, "CFD", "IBITn", "SMART", under=677037673)
        stk = detail(677037673, "STK", "IBIT", "NASDAQ")
        ib = FakeIB({677037673: [stk]}, {("CFD", "IBIT"): [cfd]})
        trade, _, data, data_det, expiry = resolve_instrument(
            ib, "IBIT", "CFD", "SMART", "USD")
        self.assertEqual(trade.conId, 699185298)
        self.assertEqual(data.conId, 677037673)
        self.assertEqual(data.exchange, "SMART")
        self.assertIs(data_det, stk)
        self.assertIsNone(expiry)

    def test_cash_pair_resolves_on_idealpro_with_base_and_quote(self):
        eur = detail(12087792, "CASH", "EUR.USD", "IDEALPRO")
        seen = []

        class IB(FakeIB):
            def reqContractDetails(self, c):
                seen.append(c)
                return super(IB, self).reqContractDetails(c)
        ib = IB({}, {("CASH", "EUR"): [eur]})
        trade, _, data, _, expiry = resolve_instrument(ib, "EUR.USD", "CASH", "SMART")
        self.assertIs(trade, data)
        self.assertEqual(trade.conId, 12087792)
        self.assertEqual((seen[0].symbol, seen[0].currency, seen[0].exchange),
                         ("EUR", "USD", "IDEALPRO"))
        self.assertIsNone(expiry)

    def test_ambiguous_symbol_is_refused(self):
        ib = FakeIB({}, {("CFD", "X"): [detail(1, "CFD", "Xa", "SMART", 9),
                                        detail(2, "CFD", "Xb", "SMART", 9)]})
        with self.assertRaises(RuntimeError):
            resolve_instrument(ib, "X", "CFD", "SMART")


class TestOutsideRthPerInstrument(unittest.TestCase):
    """Un stop GTC del CFD no debe poder dispararse con un print del
    pre-market (el backtest solo ve RTH); el del futuro, sí fuera de RTH."""

    def orders_for(self, specs):
        from qsCrypto.event.event import OrderEvent
        from qsCrypto.execution.execution import IBKRExecutionHandler

        placed = []
        ib = SimpleNamespace(
            execDetailsEvent=SimpleNamespace(__iadd__=None),
            client=SimpleNamespace(getReqId=iter(range(100, 200)).__next__),
            placeOrder=lambda c, o: placed.append(o) or SimpleNamespace(order=o))

        class Ev(object):
            def __iadd__(self, h):
                return self
        ib.execDetailsEvent = Ev()
        ex = IBKRExecutionHandler(queue.Queue(), ib, {"X": SimpleNamespace(conId=1)},
                                  specs, account="DU1")
        order = OrderEvent("X", 10, "market", "buy")
        order.stop_price = 44.123
        ex.execute_order(order)
        return placed

    def test_cfd_orders_stay_inside_rth(self):
        placed = self.orders_for({"X": {"minTick": 0.01, "outside_rth": False}})
        self.assertEqual([o.outsideRth for o in placed], [False, False])
        self.assertEqual(placed[1].auxPrice, 44.12)
        self.assertEqual([o.tif for o in placed], ["GTC", "GTC"])

    def test_futures_default_keeps_outside_rth(self):
        placed = self.orders_for({"X": {"minTick": 5.0}})
        self.assertEqual([o.outsideRth for o in placed], [True, True])


if __name__ == "__main__":
    unittest.main()
