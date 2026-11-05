"""Tests del filtro de sesión regular.

Lo que se fija aquí no es que el filtro recorte filas, sino DÓNDE recorta y,
sobre todo, que el orden sea el correcto: primero filtrar, después calcular.
Calcular los indicadores sobre 24/7 y quedarse luego con las señales de la
sesión deja la madrugada dentro de N y del canal, que es exactamente el efecto
que el filtro existe para eliminar.
"""
import os
import unittest

import numpy as np
import pandas as pd

from qsCrypto.backtest.binance_data import restrict_to_session
from qsCrypto.strategy.strategy import compute_n, compute_vwap

CACHE = os.path.join(os.path.dirname(__file__), "..", "..", "data_cache",
                     "binance", "30m", "BTCUSDT.csv")


def frame(index):
    n = len(index)
    return pd.DataFrame({"open": np.ones(n), "high": np.ones(n) * 2,
                         "low": np.ones(n) * 0.5, "close": np.ones(n),
                         "volume": np.ones(n)}, index=index)


class TestRestrictToSession(unittest.TestCase):
    def test_summer_window_is_1330_2015_utc(self):
        idx = pd.date_range("2025-07-07 00:00", "2025-07-07 23:30", freq="30min")
        out = restrict_to_session(frame(idx))
        self.assertEqual(out.index[0], pd.Timestamp("2025-07-07 13:30"))
        self.assertEqual(out.index[-1], pd.Timestamp("2025-07-07 20:00"))
        self.assertEqual(len(out), 14)

    def test_winter_window_shifts_one_hour_with_dst(self):
        """La ventana se fija en hora local, no en UTC: si se fijara en UTC,
        medio año se estaría operando una hora corrida."""
        idx = pd.date_range("2025-01-07 00:00", "2025-01-07 23:30", freq="30min")
        out = restrict_to_session(frame(idx))
        self.assertEqual(out.index[0], pd.Timestamp("2025-01-07 14:30"))
        self.assertEqual(out.index[-1], pd.Timestamp("2025-01-07 21:00"))

    def test_right_edge_is_exclusive(self):
        idx = pd.date_range("2025-07-07 20:00", "2025-07-07 20:30", freq="15min")
        out = restrict_to_session(frame(idx))
        self.assertNotIn(pd.Timestamp("2025-07-07 20:15"), out.index)
        self.assertIn(pd.Timestamp("2025-07-07 20:00"), out.index)

    def test_weekends_are_dropped(self):
        idx = pd.date_range("2025-07-05 00:00", "2025-07-06 23:30", freq="30min")
        self.assertEqual(len(restrict_to_session(frame(idx))), 0)

    def test_empty_frame_survives(self):
        empty = frame(pd.DatetimeIndex([]))
        self.assertEqual(len(restrict_to_session(empty)), 0)


@unittest.skipUnless(os.path.exists(CACHE), "sin caché de BTCUSDT")
class TestIndicatorsComputedAfterFiltering(unittest.TestCase):
    """El orden importa, y esto lo demuestra con números."""

    @classmethod
    def setUpClass(cls):
        df = pd.read_csv(CACHE, index_col=0, parse_dates=True)
        cls.full = df.loc["2025-01-01":"2025-06-30"]
        cls.sess = restrict_to_session(cls.full)

    def n_of(self, d):
        return compute_n(d["high"].to_numpy(float), d["low"].to_numpy(float),
                         d["close"].to_numpy(float), period=20)

    def test_filtering_first_gives_a_different_n(self):
        after = pd.Series(self.n_of(self.sess), index=self.sess.index)
        before = pd.Series(self.n_of(self.full), index=self.full.index).loc[self.sess.index]
        common = after.dropna().index.intersection(before.dropna().index)
        self.assertGreater(len(common), 500)
        rel = ((after[common] - before[common]).abs() / before[common]).median()
        self.assertGreater(rel, 0.05,
                           "filtrar antes o después daría casi el mismo N: "
                           "el test no estaría midiendo nada")

    def test_filtering_first_widens_the_channel_relative_to_n(self):
        """La medida que explica el resultado: el semiancho del canal en N."""
        def half_width_in_n(d):
            h, l = d["high"].to_numpy(float), d["low"].to_numpy(float)
            n = self.n_of(d)
            out = []
            for i in range(21, len(d)):
                if np.isfinite(n[i]) and n[i] > 0:
                    out.append(((h[i - 20:i].max() - l[i - 20:i].min()) / 2) / n[i])
            return float(np.median(out))
        self.assertGreater(half_width_in_n(self.sess), half_width_in_n(self.full))

    def test_vwap_window_covers_session_bars_only(self):
        """48 barras de sesión son 3.5 días de mercado, no 24 h corridas: el
        ancla deja de estar contaminada por la madrugada."""
        a = compute_vwap(self.sess["high"].to_numpy(float),
                         self.sess["low"].to_numpy(float),
                         self.sess["close"].to_numpy(float),
                         self.sess["volume"].to_numpy(float),
                         mode="rolling", bars=48)
        self.assertTrue(np.isfinite(a[48:]).all())
        self.assertTrue(np.isnan(a[:47]).all())


if __name__ == "__main__":
    unittest.main()
