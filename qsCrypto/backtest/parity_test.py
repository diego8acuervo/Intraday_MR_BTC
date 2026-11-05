"""Paridad entre la estrategia VIVA y el motor de backtest.

La máquina de tres barras está escrita DOS VECES: una dirigida por eventos en
`MeanReversionFadeVwapStrategy` y otra vectorizada en `_SymbolBook`. El
docstring de `engine.py` prometía una comprobación de paridad en un
`validate.py` que nunca existió, así que hasta ahora nada impedía que las dos
copias divergieran en silencio — el fallo más caro posible: un backtest que
valida una estrategia distinta de la que se opera.

Esto lo cierra para la variante VWAP: las mismas barras por los dos caminos
tienen que producir la MISMA lista de señales, con el mismo instante, el mismo
lado y la misma ancla congelada.

Las dos copias se comparan en su parte de SEÑAL, sin posiciones: en cuanto hay
una posición abierta, ambas dejan de evaluar señales y lo que se estaría
comparando sería la gestión, no la señal.
"""
import os
import queue
import unittest

import numpy as np
import pandas as pd

from qsCrypto.backtest.engine import BacktestParams, _Account, _SymbolBook
from qsCrypto.event.event import BarEvent
from qsCrypto.strategy.strategy import (
    MeanReversionFadeVwapStrategy, vwap_fade_policy,
)

CACHE = os.path.join(os.path.dirname(__file__), "..", "..", "data_cache",
                     "binance", "30m", "BTCUSDT.csv")

MIN_RANGE_N = 2.0
VWAP_BARS = 48
ENTRY_BARS = 20
ATR_PERIOD = 20


@unittest.skipUnless(os.path.exists(CACHE), "sin caché de BTCUSDT")
class TestLiveBacktestParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        df = pd.read_csv(CACHE, index_col=0, parse_dates=True)
        # 500 barras exactas: es el `max_history` de la estrategia viva. Con
        # más, la viva calcularía N sobre una ventana recortada y el backtest
        # sobre la serie entera, y la EMA de Wilder es dependiente del camino:
        # divergirían por el truncado, no por la lógica.
        cls.df = df.loc["2025-03-01":].head(500)

    def engine_signals(self):
        """Señales del motor, sin dejarle abrir posición.

        `fire_pending` se consume a mano en cuanto se arma: así el libro nunca
        tiene posición y su máquina de señal se evalúa en TODAS las barras,
        que es la condición para poder compararla con la viva.
        """
        p = BacktestParams(entry_bars=ENTRY_BARS, atr_period=ATR_PERIOD)
        policy = vwap_fade_policy(min_range_n=MIN_RANGE_N, vwap_bars=VWAP_BARS)
        book = _SymbolBook("BTC", self.df, p, None, _Account(p), policy=policy)
        out = []
        for i in range(len(self.df)):
            book.signals(i)
            if book.fire_pending is not None:
                out.append((self.df.index[i], book.fire_pending["side"],
                            book.fire_pending["mid"]))
                book.fire_pending = None
        return out

    def live_signals(self):
        q = queue.Queue()
        st = MeanReversionFadeVwapStrategy(
            ["BTC"], q, entry_bars=ENTRY_BARS, atr_period=ATR_PERIOD,
            min_range_n=MIN_RANGE_N, vwap_bars=VWAP_BARS)
        for t, r in self.df.iterrows():
            st.calculate_signals(BarEvent("BTC", t, r["open"], r["high"],
                                          r["low"], r["close"], r["volume"]))
        out = []
        while not q.empty():
            s = q.get()
            out.append((s.time, 1 if s.side == "buy" else -1, s.mid))
        return out

    def test_same_signals_by_both_paths(self):
        eng, live = self.engine_signals(), self.live_signals()
        self.assertGreater(len(eng), 5, "muestra sin señales: no prueba nada")

        # La viva empieza a evaluar una barra antes que el motor
        # (`len(bars) < min_bars` frente a `i < min_bars`); se compara desde el
        # primer instante que ambos ven.
        start = max(eng[0][0], live[0][0])
        eng = [s for s in eng if s[0] >= start]
        live = [s for s in live if s[0] >= start]

        self.assertEqual([s[0] for s in eng], [s[0] for s in live],
                         "las señales caen en barras distintas")
        self.assertEqual([s[1] for s in eng], [s[1] for s in live],
                         "las señales van en lados distintos")
        self.assertTrue(np.allclose([s[2] for s in eng], [s[2] for s in live],
                                    rtol=0, atol=1e-9),
                        "el ancla congelada no coincide")

    def test_the_filter_is_what_differs_from_the_unfiltered_run(self):
        """Prueba de que el test anterior no compara dos caminos vacíos."""
        q = queue.Queue()
        st = MeanReversionFadeVwapStrategy(
            ["BTC"], q, entry_bars=ENTRY_BARS, atr_period=ATR_PERIOD,
            min_range_n=0.0, vwap_bars=VWAP_BARS)
        for t, r in self.df.iterrows():
            st.calculate_signals(BarEvent("BTC", t, r["open"], r["high"],
                                          r["low"], r["close"], r["volume"]))
        self.assertGreater(q.qsize(), len(self.live_signals()))


if __name__ == "__main__":
    unittest.main()
