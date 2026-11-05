"""Tests de IBKRExecutionHandler.flatten con divisas al contado.

IBKR no reporta la posición de EUR.USD en `reqPositions` (es un saldo en
divisa). El `flatten` genérico cancelaba el stop y luego no encontraba nada
que cerrar: la posición quedaba abierta y sin stop. Aquí se fija que en CASH
se cierra el neto PROPIO acumulado de las ejecuciones, y que en futuros
todo sigue como estaba.
"""
import queue
import unittest
from types import SimpleNamespace

from qsCrypto.execution.execution import IBKRExecutionHandler

EUR = SimpleNamespace(conId=12087792, localSymbol="EUR.USD")
MBT = SimpleNamespace(conId=772435596, localSymbol="MBTU6")


class Ev(object):
    def __iadd__(self, h):
        return self


class FakeTrade(object):
    def __init__(self, order, done=False):
        self.order, self.done = order, done

    def isDone(self):
        return self.done


class FakeIB(object):
    def __init__(self, positions=()):
        self.execDetailsEvent = Ev()
        self.client = SimpleNamespace(getReqId=iter(range(1000, 2000)).__next__)
        self.placed, self._positions, self.cancelled = [], list(positions), 0

    def placeOrder(self, contract, order):
        t = FakeTrade(order)
        self.placed.append((contract, t))
        return t

    def reqOpenOrders(self):
        return [SimpleNamespace(contract=EUR, order="stop-eur")]

    def cancelOrder(self, order):
        self.cancelled += 1

    def sleep(self, s):
        pass

    def reqPositions(self):
        pass

    def positions(self):
        return self._positions


def fill(side, shares, acct="DU496961", exec_id="e1", contract=EUR):
    return SimpleNamespace(
        contract=contract, commissionReport=None,
        execution=SimpleNamespace(execId=exec_id, side=side, shares=shares,
                                  price=1.16, time="t", acctNumber=acct,
                                  orderId=1))


def handler(ib, pair="EURUSD", contract=EUR, sec_type="CASH"):
    return IBKRExecutionHandler(
        queue.Queue(), ib, {pair: contract},
        {pair: {"minTick": 5e-05, "secType": sec_type}}, account="DU496961")


class TestFlattenCash(unittest.TestCase):
    def test_closes_own_net_even_though_positions_is_empty(self):
        ib = FakeIB(positions=[])
        ex = handler(ib)
        ex._on_exec_details(None, fill("BOT", 25000))
        flat = ex.flatten("EURUSD", reason="target")
        self.assertEqual(flat, 25000)
        self.assertEqual(ib.cancelled, 1)                 # el stop, ANTES de cerrar
        (_, t), = ib.placed
        self.assertEqual((t.order.action, t.order.totalQuantity, t.order.account),
                         ("SELL", 25000, "DU496961"))

    def test_nothing_to_close_when_the_stop_already_filled(self):
        ib = FakeIB()
        ex = handler(ib)
        ex._on_exec_details(None, fill("BOT", 25000, exec_id="e1"))
        ex._on_exec_details(None, fill("SLD", 25000, exec_id="e2"))   # stop
        with self.assertLogs("qsCrypto.execution.execution", "WARNING"):
            self.assertEqual(ex.flatten("EURUSD", reason="vertical"), 0)
        self.assertEqual(ib.placed, [])

    def test_no_second_close_while_the_first_is_pending(self):
        ib = FakeIB()
        ex = handler(ib)
        ex._on_exec_details(None, fill("SLD", 30000))                  # corto
        ex.flatten("EURUSD", reason="target")
        with self.assertLogs("qsCrypto.execution.execution", "WARNING"):
            self.assertEqual(ex.flatten("EURUSD", reason="kill"), 0)
        self.assertEqual(len(ib.placed), 1)
        self.assertEqual(ib.placed[0][1].order.action, "BUY")

    def test_duplicate_exec_ids_do_not_double_the_net(self):
        ex = handler(FakeIB())
        ex._on_exec_details(None, fill("BOT", 25000, exec_id="e1"))
        ex._on_exec_details(None, fill("BOT", 25000, exec_id="e1"))
        self.assertEqual(ex.net["EURUSD"], {"DU496961": 25000.0})

    def test_closes_each_account_where_it_was_filled(self):
        ib = FakeIB()
        ex = handler(ib)
        ex._on_exec_details(None, fill("BOT", 25000, acct="DU1", exec_id="a"))
        ex._on_exec_details(None, fill("BOT", 10000, acct="DU2", exec_id="b"))
        ex.flatten("EURUSD")
        self.assertEqual(sorted((t.order.account, t.order.totalQuantity) for _, t in ib.placed),
                         [("DU1", 25000), ("DU2", 10000)])


class TestFlattenFuturesUnchanged(unittest.TestCase):
    def test_futures_still_close_from_broker_positions(self):
        pos = SimpleNamespace(contract=MBT, position=3, account="DU496961")
        ib = FakeIB(positions=[pos])
        ex = handler(ib, pair="MBT", contract=MBT, sec_type="FUT")
        self.assertEqual(ex.flatten("MBT"), 3)
        self.assertEqual(ib.placed[0][1].order.action, "SELL")


if __name__ == "__main__":
    unittest.main()
