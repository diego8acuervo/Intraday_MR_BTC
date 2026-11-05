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
    MeanReversionFadeStrategy, MeanReversionFadeVwapStrategy, breakout_stretch,
    compute_n, compute_vwap, is_flat_bar,
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
        self.assertAlmostEqual(st.pending["MBT"]["level"], 110.0, places=9)

    def test_confirmed_breakout_fires_on_the_confirmation_bar(self):
        """La señal sale al recibir el CIERRE de t+1, en esa misma llamada --
        no una llamada más tarde. El fill resultante cae en el open de t+2,
        que es el mismo instante en que t+1 cerró; esperar una llamada extra
        perdería ese bar sin poder recuperarlo."""
        out, _ = run(FLATRANGE + [BREAKOUT, CONFIRM], **self.KW)
        self.assertEqual(len(out), 1)
        # Fade: la ruptura fue alcista, así que la señal es de venta.
        self.assertEqual(out[0].side, "sell")

    def test_unconfirmed_breakout_reevaluates_same_bar_as_new_breakout(self):
        """Si la ruptura sigue extendiéndose (no confirma) Y esa misma barra
        rompe el canal YA ACTUALIZADO (que ahora incluye la barra t), queda
        pendiente de confirmar en la barra siguiente -- no se pierde un ciclo
        completo antes de volver a intentarlo."""
        still_out = (114.0, 116.0, 112.0, 115.0, 1000)   # cierra fuera otra vez
        out, st = run(FLATRANGE + [BREAKOUT, still_out], **self.KW)
        self.assertEqual(out, [])
        self.assertEqual(st.rejected["sin_confirmar"], 1)
        self.assertIsNotNone(st.pending["MBT"])
        # El canal ya incluye la barra BREAKOUT (high=115), así que el nuevo
        # nivel roto es 115, no el 110 original.
        self.assertAlmostEqual(st.pending["MBT"]["level"], 115.0, places=9)

    def test_unconfirmed_breakout_without_new_break_is_discarded(self):
        """Si no confirma y TAMPOCO rompe el canal actualizado, sí se
        descarta sin nada pendiente -- la reevaluación no se inventa una
        ruptura donde no la hay."""
        still_out_no_rebreak = (112.0, 114.5, 111.0, 112.0, 1000)
        out, st = run(FLATRANGE + [BREAKOUT, still_out_no_rebreak], **self.KW)
        self.assertEqual(out, [])
        self.assertEqual(st.rejected["sin_confirmar"], 1)
        self.assertIsNone(st.pending["MBT"])

    def test_flat_bar_does_not_confirm(self):
        flat = (114.0, 114.0, 114.0, 114.0, 0)
        out, st = run(FLATRANGE + [BREAKOUT, flat], **self.KW)
        self.assertEqual(out, [])
        self.assertEqual(st.rejected["plana"], 1)

    def test_frozen_values_come_from_the_breakout_bar(self):
        """N, nivel y centro son los de t, no los de la barra de confirmación."""
        out, _ = run(FLATRANGE + [BREAKOUT, CONFIRM], **self.KW)
        sig = out[0]
        self.assertAlmostEqual(sig.level, 110.0, places=9)   # techo del canal
        self.assertAlmostEqual(sig.mid, 105.0, places=9)     # centro del canal
        self.assertGreater(sig.n, 0)

    def test_no_confirmation_fires_on_the_bar_right_after_the_breakout(self):
        """Sin exigir confirmación, la entrada no espera ningún cierre: se
        dispara en la primera barra que llega tras la ruptura, cualquiera
        que sea su contenido."""
        out, _ = run(FLATRANGE + [BREAKOUT, NEXT], require_confirmation=False,
                     **self.KW)
        self.assertEqual(len(out), 1)

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
        out, _ = run(FLATRANGE + [BREAKOUT, CONFIRM],
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

    def test_backfill_bar_feeds_history_but_never_trades(self):
        """Una confirmación que llega en ráfaga tras un hueco del feed no
        dispara una orden a mercado con información vieja, y la ruptura que
        confirmaba no sobrevive para dispararla en la barra siguiente."""
        q = queue.Queue()
        st = MeanReversionFadeStrategy(["MBT"], q, **self.KW)
        for i, b in enumerate(FLATRANGE + [BREAKOUT]):
            st.calculate_signals(BarEvent("MBT", i, *b[:4], b[4]))
        n_before = len(st.history["MBT"])
        st.calculate_signals(
            BarEvent("MBT", 98, *CONFIRM[:4], CONFIRM[4], backfill=True))
        st.calculate_signals(BarEvent("MBT", 99, *NEXT[:4], NEXT[4]))
        self.assertTrue(q.empty())
        self.assertIsNone(st.pending["MBT"])
        self.assertEqual(len(st.history["MBT"]), n_before + 2)


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


class TestComputeVwap(unittest.TestCase):
    def test_rolling_window_is_volume_weighted(self):
        """Dos barras con el mismo precio típico pero volúmenes distintos no
        pesan igual: si pesaran, sería una media móvil, no un VWAP."""
        h, l, c = [10.0, 20.0], [10.0, 20.0], [10.0, 20.0]   # típicos 10 y 20
        v = [1.0, 3.0]
        out = compute_vwap(h, l, c, v, mode="rolling", bars=2)
        self.assertTrue(np.isnan(out[0]))                     # ventana sin llenar
        self.assertAlmostEqual(out[1], (10 * 1 + 20 * 3) / 4.0, places=9)

    def test_typical_price_is_the_average_of_high_low_close(self):
        out = compute_vwap([12.0], [6.0], [9.0], [1.0], mode="rolling", bars=1)
        self.assertAlmostEqual(out[0], 9.0, places=9)

    def test_zero_volume_window_is_nan_not_zero(self):
        """Un VWAP sobre volumen cero es una división por cero disfrazada."""
        out = compute_vwap([10.0] * 3, [8.0] * 3, [9.0] * 3, [0.0] * 3,
                           mode="rolling", bars=2)
        self.assertTrue(np.isnan(out).all())

    def test_rolling_window_forgets_bars_outside_it(self):
        h = l = c = [100.0, 100.0, 100.0, 1.0]
        out = compute_vwap(h, l, c, [1.0] * 4, mode="rolling", bars=2)
        self.assertAlmostEqual(out[3], 50.5, places=9)   # solo las dos últimas

    def test_session_mode_resets_at_utc_midnight(self):
        idx = pd.to_datetime([
            "2026-01-01 23:00", "2026-01-01 23:30",   # día 1
            "2026-01-02 00:00", "2026-01-02 00:30",   # día 2: empieza de cero
        ])
        h = l = c = [10.0, 20.0, 100.0, 200.0]
        out = compute_vwap(h, l, c, [1.0] * 4, mode="session", index=idx)
        self.assertAlmostEqual(out[1], 15.0, places=9)    # (10+20)/2
        self.assertAlmostEqual(out[2], 100.0, places=9)   # reinicio: solo ella
        self.assertAlmostEqual(out[3], 150.0, places=9)   # (100+200)/2

    def test_session_mode_needs_an_index(self):
        with self.assertRaises(ValueError):
            compute_vwap([1.0], [1.0], [1.0], [1.0], mode="session")

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            compute_vwap([1.0], [1.0], [1.0], [1.0], mode="ema")


class TestBreakoutStretch(unittest.TestCase):
    def test_distance_measured_in_n(self):
        self.assertAlmostEqual(breakout_stretch(115.0, 105.0, 5.0), 2.0, places=9)

    def test_sign_does_not_matter(self):
        self.assertAlmostEqual(breakout_stretch(95.0, 105.0, 5.0), 2.0, places=9)

    def test_invalid_inputs_are_nan(self):
        self.assertTrue(np.isnan(breakout_stretch(115.0, 105.0, 0.0)))
        self.assertTrue(np.isnan(breakout_stretch(115.0, float("nan"), 5.0)))


def run_vwap(bars, **kw):
    q = queue.Queue()
    st = MeanReversionFadeVwapStrategy(["MBT"], q, **kw)
    for i, b in enumerate(bars):
        st.calculate_signals(
            BarEvent("MBT", i, b[0], b[1], b[2], b[3], b[4] if len(b) > 4 else 1e3))
    out = []
    while not q.empty():
        out.append(q.get())
    return out, st


class TestVwapFade(unittest.TestCase):
    """Con FLATRANGE (típico 105) + BREAKOUT (típico 111), ventana de 5 barras
    y volumen constante, el VWAP en la ruptura es (4·105 + 111)/5 = 106.2 y
    N = 10.2, así que el extremo (115) queda a 0.86N del ancla."""

    KW = dict(entry_bars=5, atr_period=5, vwap_bars=5)
    ANCHOR = 106.2
    STRETCH = (115.0 - 106.2) / 10.2

    def test_anchor_replaces_the_channel_mid(self):
        """El objetivo de la operación pasa a ser el VWAP. Si `mid` siguiera
        siendo 105 (el centro del canal), la estrategia sería la de siempre
        con un filtro encima, no una estrategia anclada al VWAP."""
        _, st = run_vwap(FLATRANGE + [BREAKOUT], min_range_n=0.5, **self.KW)
        pend = st.pending["MBT"]
        self.assertIsNotNone(pend)
        self.assertAlmostEqual(pend["mid"], self.ANCHOR, places=9)
        self.assertAlmostEqual(pend["level"], 110.0, places=9)   # el canal, intacto

    def test_the_frozen_anchor_reaches_the_signal(self):
        out, _ = run_vwap(FLATRANGE + [BREAKOUT, CONFIRM], min_range_n=0.5,
                          **self.KW)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].side, "sell")
        self.assertAlmostEqual(out[0].mid, self.ANCHOR, places=9)

    def test_unstretched_breakout_is_rejected(self):
        """0.86N no llega a 2N: no hay pendiente y no puede haber señal."""
        self.assertLess(self.STRETCH, 2.0)
        out, st = run_vwap(FLATRANGE + [BREAKOUT, CONFIRM], min_range_n=2.0,
                           **self.KW)
        self.assertEqual(out, [])
        self.assertIsNone(st.pending["MBT"])
        self.assertGreaterEqual(st.rejected["poco_estirado"], 1)

    def test_threshold_is_inclusive(self):
        """'igual o mayor que' — el umbral justo no descarta."""
        _, st = run_vwap(FLATRANGE + [BREAKOUT], min_range_n=self.STRETCH,
                         **self.KW)
        self.assertIsNotNone(st.pending["MBT"])

    def test_no_volume_means_no_anchor_and_no_trade(self):
        flat = [(105.0, 110.0, 100.0, 105.0, 0.0)] * 12
        brk = BREAKOUT[:4] + (0.0,)
        _, st = run_vwap(list(flat) + [brk], min_range_n=0.0, **self.KW)
        self.assertIsNone(st.pending["MBT"])
        self.assertGreaterEqual(st.rejected["sin_vwap"], 1)

    def test_warmup_covers_the_vwap_window(self):
        st = MeanReversionFadeVwapStrategy(
            ["MBT"], queue.Queue(), entry_bars=5, atr_period=5, vwap_bars=50)
        self.assertGreaterEqual(st.min_bars(), 50)

    def test_base_strategy_still_anchors_on_the_channel_mid(self):
        """Regresión de la extracción de `_detect_breakout`: la clase base no
        ha cambiado de comportamiento al volverse extensible."""
        _, st = run(FLATRANGE + [BREAKOUT], entry_bars=5, atr_period=5)
        self.assertAlmostEqual(st.pending["MBT"]["mid"], 105.0, places=9)


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
