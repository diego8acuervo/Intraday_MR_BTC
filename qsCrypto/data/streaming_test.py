"""Tests de IBKRPriceHandler: de las barras de IBKR a los BarEvent.

Reproduce la sesión del 2026-09-10, en la que el proceso corrió de 08:55 a
12:35 (hora local) con ticks sanos y NO publicó una sola barra de 30 min:

* el calentamiento, a las 13:55 UTC, se quedaba con la barra de 13:30 a medio
  formar y, al cerrar, la real se descartaba por "no ser más nueva";
* el watchdog medía la edad desde el INICIO de la barra con un umbral de 45
  min, así que con el feed sano saltaba 15 min antes de cada cierre;
* cada salto reabría la suscripción y la barra nueva llegaba en la carga
  inicial, donde `has_new_bar` nunca es True: se perdía, y el bucle seguía.
"""
import queue
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pandas as pd
from ib_async import BarData

from qsCrypto.data.streaming import IBKRPriceHandler

T0 = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)
HALF = timedelta(minutes=30)


def bar(ts, px=77000.0):
    return BarData(date=ts, open=px, high=px + 50, low=px - 50,
                   close=px + 10, volume=100.0)


def naive(ts):
    return pd.Timestamp(ts).tz_convert("UTC").tz_localize(None)


class FakeEvent(object):
    def __init__(self):
        self.handlers = []

    def __iadd__(self, h):
        self.handlers.append(h)
        return self

    def __isub__(self, h):
        if h in self.handlers:
            self.handlers.remove(h)
        return self

    def emit(self, *args):
        for h in list(self.handlers):
            h(*args)


class FakeBars(list):
    def __init__(self, items):
        super(FakeBars, self).__init__(items)
        self.updateEvent = FakeEvent()


class FakeIB(object):
    """Lo mínimo de `ib_async.IB` que usa el handler. `self.bars` es lo que
    IBKR devolvería ahora mismo: la última es SIEMPRE la barra en curso."""

    def __init__(self, bars):
        self.bars = list(bars)
        self.pendingTickersEvent = FakeEvent()
        self.subs = []
        self.cancelled = 0

    def reqHistoricalData(self, contract, **kw):
        sub = FakeBars(self.bars)
        if kw.get("keepUpToDate"):
            self.subs.append(sub)
        return sub

    def reqMktData(self, *a, **kw):
        return object()

    def cancelHistoricalData(self, bars):
        self.cancelled += 1

    def cancelMktData(self, contract):
        pass

    def new_bar(self, px=77000.0, has_new_bar=True):
        """Abre la barra siguiente: la que estaba en curso queda cerrada."""
        self.bars.append(bar(self.bars[-1].date + HALF, px))
        sub = self.subs[-1]
        sub.append(self.bars[-1])
        sub.updateEvent.emit(sub, has_new_bar)


def session_at_1355():
    """08:00 -> 13:30 UTC; a las 13:55 la de 13:30 lleva 25 min formándose."""
    bars = [bar(T0 + i * HALF, 77000.0 + i) for i in range(12)]
    q = queue.Queue()
    h = IBKRPriceHandler(["MBT"], q, ib=None)
    h.ib = FakeIB(bars)
    h.contracts = {"MBT": SimpleNamespace(conId=1)}
    h.data_contracts = dict(h.contracts)
    h._sessions_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return h, q


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get())
    return out


class TestWarmup(unittest.TestCase):
    def test_in_progress_bar_is_not_seeded(self):
        h, _ = session_at_1355()
        h.warmup()
        self.assertEqual(h.history["MBT"].index[-1], naive(T0 + 10 * HALF))
        self.assertEqual(h._last_bar_time["MBT"], naive(T0 + 10 * HALF))
        self.assertEqual(len(h.history["MBT"]), 11)


class TestBarPublication(unittest.TestCase):
    def setUp(self):
        self.h, self.q = session_at_1355()
        # Como el día 10: ticks de MBT llegando, así que no hay ticks
        # sintéticos intercalados (se prueban en streaming_poll_test.py).
        self.h.last_tick_at["MBT"] = datetime.now(timezone.utc)
        self.h.warmup()
        self.h.subscribe()

    def test_subscribe_publishes_nothing_while_bar_is_live(self):
        self.assertEqual(drain(self.q), [])

    def test_1330_bar_is_published_when_1400_opens(self):
        """El caso de hoy: antes esta barra se tiraba en silencio."""
        self.h.ib.new_bar()
        evs = drain(self.q)
        self.assertEqual([e.time for e in evs], [naive(T0 + 11 * HALF)])
        self.assertTrue(evs[0].complete)
        self.assertFalse(evs[0].backfill)
        self.assertEqual(self.h.history["MBT"].index[-1], naive(T0 + 11 * HALF))

    def test_does_not_depend_on_has_new_bar(self):
        self.h.ib.new_bar(has_new_bar=False)
        self.assertEqual(len(drain(self.q)), 1)

    def test_live_bar_updates_publish_nothing_and_never_duplicate(self):
        self.h.ib.new_bar()
        drain(self.q)
        sub = self.h.ib.subs[-1]
        for _ in range(3):                     # updates de la barra viva
            sub.updateEvent.emit(sub, False)
        self.assertEqual(drain(self.q), [])

    def test_resubscribe_recovers_bars_from_initial_load(self):
        """Con la suscripción cancelada cierran 13:30 y 14:00; la nueva
        carga inicial las trae y ningún `updateEvent` las anunciará."""
        ib = self.h.ib
        ib.bars += [bar(ib.bars[-1].date + HALF), bar(ib.bars[-1].date + 2 * HALF)]
        self.h._resubscribe()
        evs = drain(self.q)
        self.assertEqual([e.time for e in evs],
                         [naive(T0 + 11 * HALF), naive(T0 + 12 * HALF)])
        self.assertEqual([e.backfill for e in evs], [True, False])
        self.assertEqual(ib.cancelled, 1)
        sub = ib.subs[-1]
        sub.updateEvent.emit(sub, False)
        self.assertEqual(drain(self.q), [])


class TestStaleness(unittest.TestCase):
    def setUp(self):
        self.h, _ = session_at_1355()
        self.h.warmup()
        self.h._resubscribe = mock.Mock()

    def check_at(self, now):
        self.h.last_tick_at["MBT"] = now       # ticks sanos: solo cuenta la barra
        self.h._check_staleness(now)

    def test_healthy_feed_before_next_close_is_not_stale(self):
        """Última publicada 13:00; la de 13:30 llegará a las 14:00 (+retraso
        de datos diferidos). A las 13:55 el umbral viejo (45 min) ya saltaba."""
        self.check_at(T0 + 11 * HALF + timedelta(minutes=25))
        self.check_at(T0 + 12 * HALF + timedelta(minutes=15))
        self.h._resubscribe.assert_not_called()

    def test_bar_feed_dead_past_threshold_is_stale(self):
        self.check_at(T0 + 10 * HALF + timedelta(minutes=91))
        self.h._resubscribe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
