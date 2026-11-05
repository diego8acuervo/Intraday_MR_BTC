"""Tests de IBKRPriceHandler en modo `poll` y del watchdog con calendario.

Caso: CFD de IBIT por SMART, barras de la acción subyacente. Calendario de
la semana del 2026-09-04: viernes abierto, fin de semana y lunes 7 (Labor
Day) CLOSED, martes 8 abierto. En septiembre la sesión 09:30-16:00 ET es
13:30-20:00 UTC.

Lo que se fija:
* la última barra de la sesión se publica al cierre (20:00:05 UTC), no a la
  mañana del siguiente día hábil;
* el sondeo se programa en el siguiente cierre de barra CON sesión abierta
  (saltando fin de semana y festivo), reintenta si IBKR aún no tiene la
  barra y se rinde a los 3 minutos;
* el watchdog no reabre nada con el mercado cerrado, en `poll` solo reabre
  los ticks, y con un error de permisos conocido no martillea;
* sin ticks en vivo, cada barra llega precedida de ticks de máximo, mínimo y
  cierre, para que el portfolio tenga precio y el MAE se mueva.
"""
import queue
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pandas as pd
from ib_async import BarData

from qsCrypto.data.streaming import IBKRPriceHandler
from qsCrypto.ibkr.sessions import TradingSessions

HOURS = ("20260904:0930-20260904:1600;20260905:CLOSED;20260906:CLOSED;"
         "20260907:CLOSED;20260908:0930-20260908:1600")
FRI = datetime(2026, 9, 4)
HALF = timedelta(minutes=30)
OPEN = FRI + timedelta(hours=13, minutes=30)          # 09:30 ET en UTC


def bar(ts, px=44.0):
    return BarData(date=ts.replace(tzinfo=timezone.utc), open=px, high=px + 0.3,
                   low=px - 0.2, close=px + 0.1, volume=1000.0)


class FakeIB(object):
    def __init__(self, bars):
        self.bars = list(bars)
        self.pendingTickersEvent = SimpleNamespace()
        self.requests = 0

    def reqHistoricalData(self, contract, **kw):
        assert not kw.get("keepUpToDate"), "en modo poll no hay suscripción de barras"
        assert contract.conId == 677037673, "las barras salen de la ACCIÓN"
        self.requests += 1
        return list(self.bars)


def make(bars_until, last_published):
    """Barras de 13:30 a `bars_until` (inclusive) y la última publicada."""
    bars, t = [], OPEN
    while t <= bars_until:
        bars.append(bar(t))
        t += HALF
    q = queue.Queue()
    h = IBKRPriceHandler(["IBIT"], q, ib=None, sec_type="CFD", exchange="SMART",
                         bar_mode="poll")
    h.ib = FakeIB(bars)
    h.contracts = {"IBIT": SimpleNamespace(conId=699185298, localSymbol="IBITn")}
    h.data_contracts = {"IBIT": SimpleNamespace(conId=677037673, localSymbol="IBIT")}
    h.sessions = {"IBIT": TradingSessions.from_hours(HOURS, "US/Eastern")}
    h._refresh_sessions = lambda now: None
    h.history = {"IBIT": pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                                      dtype=float)}
    h._last_bar_time = {"IBIT": pd.Timestamp(last_published)}
    h.last_tick_at["IBIT"] = datetime.now(timezone.utc)   # ticks vivos por defecto
    return h, q


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get())
    return out


class TestPollSchedule(unittest.TestCase):
    def setUp(self):
        self.h, _ = make(OPEN, OPEN - HALF)

    def due(self, now):
        return self.h._poll_due("IBIT", now)

    def test_in_session_polls_right_after_the_next_bar_close(self):
        self.assertEqual(self.due(FRI.replace(hour=14, minute=10)),
                         FRI.replace(hour=14, minute=30, second=5))

    def test_last_bar_of_the_session_is_polled_at_the_close(self):
        self.assertEqual(self.due(FRI.replace(hour=19, minute=45)),
                         FRI.replace(hour=20, minute=0, second=5))

    def test_after_close_skips_weekend_and_labor_day(self):
        tue = datetime(2026, 9, 8, 14, 0, 5)       # 10:00 ET: cierre de la 1ª barra
        self.assertEqual(self.due(FRI.replace(hour=20, minute=10)), tue)
        self.assertEqual(self.due(datetime(2026, 9, 7, 15, 0)), tue)


class TestPollPublication(unittest.TestCase):
    def test_session_last_bar_is_published_at_the_close_not_next_day(self):
        h, q = make(FRI.replace(hour=19, minute=30), FRI.replace(hour=19))
        n = h._poll_once("IBIT", FRI.replace(hour=20, minute=0, second=5))
        evs = [e for e in drain(q) if e.type == "BAR"]
        self.assertEqual(n, 1)
        self.assertEqual([e.time for e in evs], [pd.Timestamp(FRI.replace(hour=19, minute=30))])
        self.assertFalse(evs[0].backfill)

    def test_bar_in_progress_is_not_published(self):
        h, q = make(FRI.replace(hour=14), OPEN - HALF)    # la de 14:00 está en curso
        h._poll_once("IBIT", FRI.replace(hour=14, minute=0, second=5))
        self.assertEqual([e.time for e in drain(q) if e.type == "BAR"],
                         [pd.Timestamp(OPEN)])

    def test_step_retries_then_gives_up(self):
        h, q = make(OPEN, OPEN - HALF)
        h.ib.bars = []                          # a las 14:00:05 IBKR aún no tiene la de 13:30
        t = FRI.replace(hour=14, minute=0, second=5)
        h._next_poll_at["IBIT"] = t
        h._poll_step(t)
        self.assertEqual(h._next_poll_at["IBIT"], t + timedelta(seconds=15))
        with self.assertLogs("qsCrypto.data.streaming", "WARNING"):
            h._poll_step(t + timedelta(seconds=181))
        self.assertEqual(h._next_poll_at["IBIT"],
                         FRI.replace(hour=14, minute=30, second=5))

    def test_step_moves_on_once_the_expected_bar_arrives(self):
        h, q = make(FRI.replace(hour=14), OPEN - HALF)
        t = FRI.replace(hour=14, minute=0, second=5)
        h._next_poll_at["IBIT"] = t
        h._poll_step(t)
        self.assertEqual(h._last_bar_time["IBIT"], pd.Timestamp(OPEN))
        self.assertEqual(h._next_poll_at["IBIT"], FRI.replace(hour=14, minute=30, second=5))


class TestSyntheticTicks(unittest.TestCase):
    def test_without_live_ticks_bar_is_preceded_by_high_low_close(self):
        h, q = make(FRI.replace(hour=14), OPEN - HALF)
        h.last_tick_at["IBIT"] = None
        h._poll_once("IBIT", FRI.replace(hour=14, minute=0, second=5))
        evs = drain(q)
        self.assertEqual([e.type for e in evs], ["TICK", "TICK", "TICK", "BAR"])
        self.assertEqual([float(e.bid) for e in evs[:3]], [44.3, 43.8, 44.1])
        self.assertEqual(float(h.prices["IBIT"]["ask"]), 44.1)

    def test_with_live_ticks_no_synthetic_ticks(self):
        h, q = make(FRI.replace(hour=14), OPEN - HALF)
        h._poll_once("IBIT", FRI.replace(hour=14, minute=0, second=5))
        self.assertEqual([e.type for e in drain(q)], ["BAR"])


class TestSessionAwareWatchdog(unittest.TestCase):
    def setUp(self):
        self.h, _ = make(OPEN, OPEN - HALF)
        self.h.last_tick_at["IBIT"] = None
        self.h._resubscribe = mock.Mock()
        self.h._resubscribe_ticks = mock.Mock()

    def check(self, now):
        self.h._check_staleness(now.replace(tzinfo=timezone.utc))

    def test_closed_market_never_resubscribes(self):
        for t in (FRI.replace(hour=21), datetime(2026, 9, 5, 12), datetime(2026, 9, 7, 15)):
            self.check(t)
        self.h._resubscribe_ticks.assert_not_called()
        self.h._resubscribe.assert_not_called()

    def test_age_counts_from_the_open(self):
        self.check(OPEN + timedelta(seconds=60))          # recién abierto: no
        self.h._resubscribe_ticks.assert_not_called()
        self.check(OPEN + timedelta(minutes=5))           # 5 min sin ticks: sí
        self.h._resubscribe_ticks.assert_called_once()
        self.h._resubscribe.assert_not_called()           # en poll, solo ticks

    def test_known_entitlement_error_backs_off(self):
        self.h.md_errors["IBIT"] = (10197, "sesión competidora", OPEN)
        self.check(OPEN + timedelta(minutes=5))
        self.check(OPEN + timedelta(minutes=20))          # < 30 min: no reintenta
        self.assertEqual(self.h._resubscribe_ticks.call_count, 1)
        self.check(OPEN + timedelta(minutes=36))
        self.assertEqual(self.h._resubscribe_ticks.call_count, 2)

    def test_ib_error_is_mapped_to_the_symbol_by_either_contract(self):
        self.h.md_errors.clear()
        with self.assertLogs("qsCrypto.data.streaming", "CRITICAL"):
            self.h._on_ib_error(17, 10089, "requiere suscripción",
                                SimpleNamespace(conId=677037673, localSymbol="IBIT"))
        self.assertEqual(self.h.md_errors["IBIT"][0], 10089)
        self.h._on_ib_error(18, 2104, "farm ok", SimpleNamespace(conId=677037673))
        self.assertEqual(self.h.md_errors["IBIT"][0], 10089)


if __name__ == "__main__":
    unittest.main()
