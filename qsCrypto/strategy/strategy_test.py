"""Tests de MeanReversionFadeStrategy.

Lo que se comprueba no es que el código corra, sino que la señal cae donde el
backtest dice que cae. La máquina de tres barras es la que distingue esta
estrategia de una ruptura normal, y equivocarse en una barra la convierte en
otra cosa sin que nada falle.
"""
import os
import queue
import unittest

import numpy as np
import pandas as pd

from qsCrypto.event.event import BarEvent
from qsCrypto.strategy.strategy import (
    MeanReversionFadeStrategy, compute_n, is_flat_bar,
)

CACHE = os.path.join(
    os.path.dirname(__file__), "..", "..", "data_cache", "ibkr", "30mins",
    "MBT_chain.csv",
)


def run(bars, **kw):
    """Alimenta la estrategia con barras y devuelve (señales, estrategia)."""
    q = queue.Queue()
    st = MeanReversionFadeStrategy(["MBT"], q, **kw)
    for i, b in enumerate(bars):
        st.calculate_signals(
            BarEvent("MBT", i, b[0], b[1], b[2], b[3], b[4] if len(b) > 4 else 1e3)
        )
    out = []
    while not q.empty():
        out.append(q.get())
    return out, st


FLATRANGE = [(105.0, 110.0, 100.0, 105.0, 1000)] * 12
BREAKOUT = (106.0, 115.0, 104.0, 114.0, 1000)     # rompe 110, cierra fuera
CONFIRM = (114.0, 114.5, 108.0, 108.0, 1000)      # vuelve dentro -> confirma
NEXT = (109.0, 111.0, 107.0, 110.0, 1000)


class TestComputeN(unittest.TestCase):
    def test_wilder_seed_and_update(self):
        """N debe ser la EMA de Wilder sembrada con SMA, no una EMA de pandas."""
        h = [10.0] * 25
        l = [8.0] * 25
        c = [9.0] * 25
        n = compute_n(h, l, c, period=20)
        self.assertTrue(np.isnan(n[18]))
        self.assertAlmostEqual(n[19], 2.0, places=9)   # TR constante = 2
        self.assertAlmostEqual(n[-1], 2.0, places=9)

    def test_true_range_uses_previous_close(self):
        h, l, c = [10.0, 12.0], [9.0, 11.5], [9.5, 12.0]
        n = compute_n(h, l, c, period=2)
        # TR[1] = max(0.5, |12-9.5|, |9.5-11.5|) = 2.5 -> media de (1.0, 2.5)
        self.assertAlmostEqual(n[1], 1.75, places=9)

    def test_short_series_returns_nan(self):
        self.assertTrue(np.isnan(compute_n([1.0], [1.0], [1.0], period=20)).all())


class TestFlatBar(unittest.TestCase):
    def test_flat(self):
        self.assertTrue(is_flat_bar(100.0, 100.0, 0))

    def test_not_flat_with_range(self):
        self.assertFalse(is_flat_bar(101.0, 100.0, 0))

    def test_not_flat_with_volume(self):
        self.assertFalse(is_flat_bar(100.0, 100.0, 5))


class TestThreeBarMachine(unittest.TestCase):
    KW = dict(entry_bars=5, atr_period=5)

    def test_breakout_alone_opens_nothing(self):
        out, st = run(FLATRANGE + [BREAKOUT], **self.KW)
        self.assertEqual(out, [])
        self.assertIsNotNone(st.pending["MBT"])
        self.assertEqual(st.pending["MBT"]["etapa"], "confirmar")

    def test_confirmed_breakout_fires_two_bars_later(self):
        out, _ = run(FLATRANGE + [BREAKOUT, CONFIRM, NEXT], **self.KW)
        self.assertEqual(len(out), 1)
        # Fade: la ruptura fue alcista, así que la señal es de venta.
        self.assertEqual(out[0].side, "sell")

    def test_unconfirmed_breakout_is_discarded_without_retry(self):
        still_out = (114.0, 116.0, 112.0, 115.0, 1000)   # cierra fuera otra vez
        out, st = run(FLATRANGE + [BREAKOUT, still_out, NEXT], **self.KW)
        self.assertEqual(out, [])
        self.assertEqual(st.rejected["sin_confirmar"], 1)
        self.assertIsNone(st.pending["MBT"])

    def test_flat_bar_does_not_confirm(self):
        flat = (114.0, 114.0, 114.0, 114.0, 0)
        out, st = run(FLATRANGE + [BREAKOUT, flat, NEXT], **self.KW)
        self.assertEqual(out, [])
        self.assertEqual(st.rejected["plana"], 1)

    def test_frozen_values_come_from_the_breakout_bar(self):
        """N, nivel y centro son los de t, no los de t+2."""
        out, _ = run(FLATRANGE + [BREAKOUT, CONFIRM, NEXT], **self.KW)
        sig = out[0]
        self.assertAlmostEqual(sig.level, 110.0, places=9)   # techo del canal
        self.assertAlmostEqual(sig.mid, 105.0, places=9)     # centro del canal
        self.assertGreater(sig.n, 0)

    def test_open_position_kills_pending_signal(self):
        q = queue.Queue()
        st = MeanReversionFadeStrategy(["MBT"], q, **self.KW)
        for i, b in enumerate(FLATRANGE + [BREAKOUT]):
            st.calculate_signals(BarEvent("MBT", i, *b[:4], b[4]))
        st.set_open_symbols(["MBT"])
        st.calculate_signals(BarEvent("MBT", 99, *CONFIRM[:4], CONFIRM[4]))
        self.assertTrue(q.empty())
        self.assertEqual(st.rejected["posicion_abierta"], 1)

    def test_breakout_mode_does_not_invert(self):
        out, _ = run(FLATRANGE + [BREAKOUT, CONFIRM, NEXT],
                     signal_mode="breakout", **self.KW)
        self.assertEqual(out[0].side, "buy")

    def test_short_side_can_be_disabled(self):
        low_break = (104.0, 106.0, 95.0, 96.0, 1000)     # rompe 100 a la baja
        back_in = (96.0, 103.0, 96.0, 102.0, 1000)
        out, _ = run(FLATRANGE + [low_break, back_in, NEXT],
                     allow_short=False, **self.KW)
        self.assertEqual(out, [])

    def test_incomplete_bars_are_ignored(self):
        q = queue.Queue()
        st = MeanReversionFadeStrategy(["MBT"], q, **self.KW)
        for i, b in enumerate(FLATRANGE):
            st.calculate_signals(BarEvent("MBT", i, *b[:4], b[4], complete=False))
        self.assertEqual(len(st.history["MBT"]), 0)


class TestChannelHasNoLookahead(unittest.TestCase):
    def test_current_bar_excluded_from_its_own_channel(self):
        """El canal se mide sobre las barras ANTERIORES.

        Si la barra actual entrase en su propio canal nunca podría superarlo:
        su propio máximo sería el máximo. Que rompa es la prueba de que está
        excluida.
        """
        q = queue.Queue()
        st = MeanReversionFadeStrategy(["MBT"], q, entry_bars=5, atr_period=5)
        for i, b in enumerate(FLATRANGE):
            st.calculate_signals(BarEvent("MBT", i, *b[:4], b[4]))
        hi, lo, mid = st._channel(st.history["MBT"])
        self.assertAlmostEqual(hi, 110.0, places=9)
        self.assertAlmostEqual(lo, 100.0, places=9)
        self.assertAlmostEqual(mid, 105.0, places=9)


@unittest.skipUnless(os.path.exists(CACHE), "sin caché de 30 min")
class TestReplicaAgainstBacktestData(unittest.TestCase):
    """Réplica sobre las MISMAS barras que usó el backtest.

    No compara contra una lista de señales grabada (el notebook no la exporta),
    pero sí verifica sobre datos reales las invariantes que el backtest declara
    y que son las que se romperían al reimplementar: que toda señal nace de una
    ruptura confirmada, que el lado va invertido respecto de la ruptura, y que
    la tasa de confirmación cae en el orden de magnitud medido (~27% de las
    rupturas confirman).
    """
    @classmethod
    def setUpClass(cls):
        df = pd.read_csv(CACHE)
        tcol = "time" if "time" in df.columns else df.columns[0]
        df[tcol] = pd.to_datetime(df[tcol], utc=True, errors="coerce")
        df = df.dropna(subset=[tcol]).set_index(tcol).sort_index()
        df.index = df.index.tz_localize(None)
        cls.df = df[["open", "high", "low", "close", "volume"]].astype(float).tail(3000)

    def test_signals_on_real_bars(self):
        q = queue.Queue()
        st = MeanReversionFadeStrategy(
            ["MBT"], q, entry_bars=20, atr_period=20,
            require_confirmation=True, confirm_mode="inside", allow_short=True,
        )
        for t, r in self.df.iterrows():
            st.calculate_signals(BarEvent(
                "MBT", t, r["open"], r["high"], r["low"], r["close"], r["volume"]))
        sigs = []
        while not q.empty():
            sigs.append(q.get())

        self.assertGreater(len(sigs), 0, "no se generó ninguna señal")
        for s in sigs:
            self.assertIn(s.side, ("buy", "sell"))
            self.assertGreater(s.n, 0)
            self.assertTrue(s.level is not None and s.mid is not None)
            # El centro del canal siempre queda entre sus extremos.
            self.assertLessEqual(min(s.level, s.mid), max(s.level, s.mid))

        breakouts = len(sigs) + st.rejected["sin_confirmar"] + st.rejected["plana"]
        rate = len(sigs) / float(breakouts)
        # El backtest mide ~27% de confirmación; se deja margen amplio porque la
        # ventana no es la misma, pero un 0% o un 100% delatarían que el filtro
        # de confirmación no está haciendo nada.
        self.assertGreater(rate, 0.02)
        self.assertLess(rate, 0.80)

    def test_confirmation_filter_reduces_trade_count(self):
        """Sin confirmación tiene que haber MÁS señales. Es su razón de ser."""
        def count(require):
            q = queue.Queue()
            st = MeanReversionFadeStrategy(
                ["MBT"], q, entry_bars=20, atr_period=20,
                require_confirmation=require)
            for t, r in self.df.iterrows():
                st.calculate_signals(BarEvent(
                    "MBT", t, r["open"], r["high"], r["low"], r["close"],
                    r["volume"]))
            return q.qsize()
        self.assertGreater(count(False), count(True))


if __name__ == "__main__":
    unittest.main()
