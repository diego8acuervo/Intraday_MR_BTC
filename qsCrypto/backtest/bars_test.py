"""Tests de los muestreadores de barras por actividad.

Lo que se fija aquí es todo lo que un muestreo alternativo puede falsear sin
que se note: que el umbral se respete, que el acumulador se reinicie en cada
apertura de sesión, que ninguna barra ya cerrada cambie al añadir datos
posteriores (causalidad), y —el más importante— que `kind="time"` reproduzca
las velas de reloj bar por bar. Ese último es el control del experimento: si
el camino de código no devuelve las barras de 5 minutos que ya conocemos,
ninguna de las otras cifras significa nada.
"""
import os
import unittest

import numpy as np
import pandas as pd

from qsCrypto.backtest.bars import (
    imbalance_bars, opening_bars, sample_bars, session_id,
)

CACHE_1M = os.path.join(os.path.dirname(__file__), "..", "..", "data_cache",
                        "binance", "1m_full", "um_BTCUSDT.pkl")
CACHE_5M = os.path.join(os.path.dirname(__file__), "..", "..", "data_cache",
                        "binance", "5m", "BTCUSDT.csv")


def _synth(days=4, per_min_vol=10.0, seed=0):
    """Minutos 24/7 deterministas, para no depender de la caché."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=days * 1440, freq="1min")
    close = 100 + np.cumsum(rng.normal(0, 0.02, len(idx)))
    open_ = np.r_[close[0], close[:-1]]
    vol = np.full(len(idx), per_min_vol)
    tb = vol * rng.uniform(0.3, 0.7, len(idx))
    return pd.DataFrame({
        "open": open_,
        "high": np.maximum(open_, close) + 0.05,
        "low": np.minimum(open_, close) - 0.05, "close": close, "volume": vol,
        "quote_volume": vol * close, "count": np.full(len(idx), 7.0),
        "taker_buy_volume": tb,
    }, index=idx)


class TestSessionId(unittest.TestCase):

    def test_window_and_dst(self):
        """09:30-15:45 locales, y el mismo minuto local cae en dos horas UTC
        distintas según la época del año."""
        s = session_id(pd.DatetimeIndex([
            "2024-01-02 14:30",   # 09:30 EST  -> dentro
            "2024-01-02 14:29",   # 09:29 EST  -> fuera
            "2024-01-02 20:44",   # 15:44 EST  -> dentro
            "2024-01-02 20:45",   # 15:45 EST  -> fuera (semiabierto)
            "2024-06-03 13:30",   # 09:30 EDT  -> dentro
            "2024-06-03 14:30",   # 10:30 EDT  -> dentro
        ]))
        self.assertEqual(list(s.notna()), [True, False, True, False, True, True])
        self.assertEqual(s.iloc[0], pd.Timestamp("2024-01-02"))
        self.assertEqual(s.iloc[4], pd.Timestamp("2024-06-03"))

    def test_weekends_excluded(self):
        s = session_id(pd.DatetimeIndex(["2024-01-06 15:00"]))  # sábado
        self.assertTrue(s.isna().all())


class TestSampleBars(unittest.TestCase):

    def setUp(self):
        self.df = _synth()

    def test_threshold_is_respected(self):
        """Toda barra cerrada acumula >= umbral, y nunca parte un minuto."""
        b = sample_bars(self.df, "volume", 50.0)
        full = b[b["n_minutes"] >= 5]
        self.assertTrue((full["volume"] >= 50.0 - 1e-9).all())
        # con 10 BTC/min y umbral 50, una barra completa son 5 minutos exactos
        self.assertTrue((full["n_minutes"] == 5).all())

    def test_ohlc_is_consistent(self):
        b = sample_bars(self.df, "dollar", 20_000.0)
        self.assertTrue((b["high"] >= b["low"]).all())
        self.assertTrue((b["high"] >= b["close"]).all())
        self.assertTrue((b["high"] >= b["open"]).all())
        self.assertTrue((b["low"] <= b["close"]).all())
        self.assertTrue((b["end"] >= b["start"]).all())

    def test_session_reset(self):
        """Ninguna barra cruza la frontera de sesión, y cada sesión empieza
        con el acumulador a cero."""
        b = sample_bars(self.df, "volume", 50.0)
        self.assertTrue(b["session"].notna().all())
        s_start = session_id(pd.DatetimeIndex(b["start"]))
        s_end = session_id(pd.DatetimeIndex(b["end"]))
        self.assertTrue((s_start.to_numpy() == s_end.to_numpy()).all())
        # la primera barra de cada sesión arranca en el primer minuto de ella
        op = opening_bars(b)
        for _, r in op.iterrows():
            loc = (pd.Timestamp(r["start"]).tz_localize("UTC")
                   .tz_convert("America/New_York"))
            self.assertEqual(loc.strftime("%H:%M"), "09:30")

    def test_one_opening_bar_per_session(self):
        b = sample_bars(self.df, "volume", 50.0)
        op = opening_bars(b)
        self.assertEqual(len(op), b["session"].nunique())
        self.assertEqual(len(op), len(set(op["session"])))

    def test_causality_under_truncation(self):
        """Añadir minutos POSTERIORES no puede cambiar una barra ya cerrada.
        Es la propiedad que hace que el muestreo no mire al futuro."""
        full = sample_bars(self.df, "dollar", 20_000.0)
        cut_at = self.df.index[len(self.df) * 2 // 3]
        cut = sample_bars(self.df.loc[:cut_at], "dollar", 20_000.0)
        # todas las barras de `cut` salvo la última (que puede estar a medias)
        # deben existir idénticas en `full`
        k = len(cut) - 1
        for col in ("start", "end", "open", "high", "low", "close"):
            np.testing.assert_array_equal(
                cut[col].to_numpy()[:k], full[col].to_numpy()[:k], err_msg=col)

    def test_time_kind_equals_resample(self):
        """`kind="time"` con umbral 5 == remuestrear a 5 minutos."""
        b = sample_bars(self.df, "time", 5)
        sid = session_id(self.df.index)
        sess = self.df[sid.notna()]
        want = sess.resample("5min").agg({"open": "first", "high": "max",
                                          "low": "min", "close": "last",
                                          "volume": "sum"}).dropna()
        got = b[b["n_minutes"] == 5]
        want = want.loc[pd.DatetimeIndex(got["start"])]
        for col in ("open", "high", "low", "close", "volume"):
            np.testing.assert_allclose(got[col].to_numpy(),
                                       want[col].to_numpy(), rtol=1e-12,
                                       err_msg=col)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            sample_bars(self.df, "nope", 10)
        with self.assertRaises(ValueError):
            sample_bars(self.df, "volume", 0)
        with self.assertRaises(ValueError):
            sample_bars(self.df.drop(columns=["volume"]), "volume", 10)


class TestImbalanceBars(unittest.TestCase):

    def setUp(self):
        self.df = _synth(days=8, seed=3)

    def test_produces_bars_and_respects_sessions(self):
        b = imbalance_bars(self.df, ewma_span=50, max_minutes=60)
        self.assertGreater(len(b), 10)
        s_start = session_id(pd.DatetimeIndex(b["start"]))
        s_end = session_id(pd.DatetimeIndex(b["end"]))
        self.assertTrue((s_start.to_numpy() == s_end.to_numpy()).all())
        self.assertTrue((b["n_minutes"] <= 60).all())

    def test_signed_volume_has_both_signs(self):
        """Si el flujo firmado saliera siempre del mismo signo, el signo
        estaría mal calculado (2*taker_buy - volume, no taker_buy)."""
        b = imbalance_bars(self.df, ewma_span=50, max_minutes=60)
        self.assertTrue((b["signed_volume"] > 0).any())
        self.assertTrue((b["signed_volume"] < 0).any())

    def test_signed_volume_bounded_by_volume(self):
        b = imbalance_bars(self.df, ewma_span=50, max_minutes=60)
        self.assertTrue((b["signed_volume"].abs() <= b["volume"] + 1e-9).all())

    def test_causality_under_truncation(self):
        full = imbalance_bars(self.df, ewma_span=50, max_minutes=60)
        cut_at = self.df.index[len(self.df) * 2 // 3]
        cut = imbalance_bars(self.df.loc[:cut_at], ewma_span=50, max_minutes=60)
        k = len(cut) - 1
        for col in ("start", "end", "close"):
            np.testing.assert_array_equal(
                cut[col].to_numpy()[:k], full[col].to_numpy()[:k], err_msg=col)

    def test_fixed_threshold_mode(self):
        """Con umbral fijo, toda barra cerrada alcanza |theta| >= umbral."""
        # El flujo firmado sintético es de +-4 BTC/min, así que |theta| se
        # mueve en decenas: umbrales del orden de los datos reales (cientos)
        # no se alcanzarían nunca aquí.
        b = imbalance_bars(self.df, threshold=15.0, max_minutes=600)
        # La ÚLTIMA barra de cada sesión se vacía en el cierre sin haber
        # alcanzado el umbral (igual que en `sample_bars`): es parcial por
        # construcción y no debe exigírsele el umbral.
        last = b.groupby("session")["start"].transform("max") == b["start"]
        done = b[(b["n_minutes"] < 600) & ~last]
        self.assertGreater(len(done), 5)
        self.assertTrue((done["signed_volume"].abs() >= 15.0 - 1e-9).all())

    def test_fixed_threshold_controls_duration(self):
        """Subir el umbral alarga las barras — la propiedad que permite
        calibrar la duración y compararlas con volumen/dólar a igualdad de
        tamaño de barra."""
        short = imbalance_bars(self.df, threshold=10.0, max_minutes=600)
        long_ = imbalance_bars(self.df, threshold=30.0, max_minutes=600)
        self.assertLess(short["n_minutes"].median(), long_["n_minutes"].median())

    def test_requires_taker_volume(self):
        with self.assertRaises(ValueError):
            imbalance_bars(self.df.drop(columns=["taker_buy_volume"]))


@unittest.skipUnless(os.path.exists(CACHE_1M) and os.path.exists(CACHE_5M),
                     "sin caché de 1m/5m de BTCUSDT")
class TestAgainstRealData(unittest.TestCase):
    """El control del experimento, sobre datos reales.

    `kind="time"` con umbral 5 tiene que devolver EXACTAMENTE las velas de
    5 minutos que ya usaron los cuadernos anteriores. Si esto pasa, el
    muestreador, el filtro de sesión y la agregación OHLCV están bien, y la
    comparación entre esquemas de muestreo mide lo que dice medir.
    """

    @classmethod
    def setUpClass(cls):
        cls.m1 = pd.read_pickle(CACHE_1M)
        cls.m5 = pd.read_csv(CACHE_5M, index_col=0, parse_dates=True)

    def test_no_missing_minutes(self):
        full = pd.date_range(self.m1.index[0], self.m1.index[-1], freq="1min")
        self.assertEqual(len(full.difference(self.m1.index)), 0)

    def test_1m_resamples_to_the_cached_5m(self):
        """Integridad del marco nuevo contra el que ya está validado.

        No se afloja la tolerancia: se exige que casi toda discrepancia esté
        EXPLICADA por minutos de relleno del archivo (volumen 0, precio
        congelado: 2023-11-10, 2024-03-27, 2024-10-28...), y se acota el
        residuo a un puñado de barras sobre ~490.000.

        Distinguir "artefacto conocido y acotado" de "tolerancia generosa"
        es lo que hace que este test sirva de algo: un desplazamiento de
        columnas o un desfase de huso produciría discrepancias en barras CON
        volumen y por todo el rango, y haría fallar el test aunque el error
        relativo fuese pequeño.

        Las ~3 barras que no encajan (2024-03-27/28) difieren en VOLUMEN: el
        archivo trae ~3x más que la caché REST. El archivo es el conjunto
        canónico corregido a posteriori y es el que usa el experimento; la
        caché de 5 min se pobló en vivo y ahí capturó datos incompletos.
        """
        lo = max(self.m1.index[0], self.m5.index[0])
        hi = min(self.m1.index[-1], self.m5.index[-1])
        a = self.m1.loc[lo:hi]
        got = a.resample("5min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last",
             "volume": "sum"}).dropna()
        flat = ((a["volume"] <= 0).resample("5min").sum()
                .reindex(got.index).fillna(0))
        want = self.m5.loc[lo:hi]
        common = got.index.intersection(want.index)
        self.assertGreater(len(common), 100_000)

        bad = np.zeros(len(common), dtype=bool)
        for col in ("open", "high", "low", "close"):
            bad |= ~np.isclose(got.loc[common, col].to_numpy(),
                               want.loc[common, col].to_numpy(), rtol=1e-9)
        has_filler = flat.loc[common].to_numpy() > 0
        unexplained = common[bad & ~has_filler]

        self.assertLess(bad.mean(), 1e-3, f"{100*bad.mean():.3f}% discrepante")
        self.assertLessEqual(
            len(unexplained), 5,
            f"{len(unexplained)} barras sin explicar por relleno: "
            f"{list(unexplained[:10])}")

        # Volumen: se comprueba el AGREGADO y se acota el número de barras
        # discrepantes, en vez de exigir igualdad elemento a elemento. Las dos
        # fuentes difieren en ~53 barras (relleno del archivo más un puñado de
        # tramos que el REST capturó incompletos), pero el volumen total
        # cuadra a 1 parte entre 11.000 — que es la afirmación que de verdad
        # importa para un muestreo por volumen acumulado.
        gv = got.loc[common, "volume"].to_numpy()
        wv = want.loc[common, "volume"].to_numpy()
        vol_bad = ~np.isclose(gv, wv, rtol=1e-6)
        self.assertLess(vol_bad.mean(), 5e-4,
                        f"{100*vol_bad.mean():.4f}% de barras con volumen distinto")
        self.assertLess(abs(gv.sum() - wv.sum()) / wv.sum(), 1e-3,
                        "el volumen agregado no cuadra entre archivo y REST")

    def test_time_bars_match_the_5m_session_frame(self):
        """EL control del experimento: `kind="time"` con umbral 5 devuelve
        las velas de 5 minutos que ya usaron los cuadernos anteriores.

        Se excluyen las barras con CUALQUIER minuto de relleno del archivo
        (`flat_minutes > 0`), no solo las de volumen total cero: la barra que
        mezcla relleno y mercado tiene volumen > 0 pero su apertura sale de
        un minuto congelado.
        """
        b = sample_bars(self.m1, "time", 5)
        b = b[(b["n_minutes"] == 5) & (b["flat_minutes"] == 0)]
        want = self.m5.loc[pd.DatetimeIndex(b["start"])]
        self.assertGreater(len(b), 80_000)
        for col in ("open", "high", "low", "close"):
            np.testing.assert_allclose(b[col].to_numpy(),
                                       want[col].to_numpy(), rtol=1e-9,
                                       err_msg=col)

    def test_opening_bar_is_always_0930(self):
        for kind, thr in (("time", 5), ("volume", 2250.0), ("dollar", 115e6)):
            with self.subTest(kind=kind):
                op = opening_bars(sample_bars(self.m1, kind, thr))
                loc = (pd.DatetimeIndex(op["start"]).tz_localize("UTC")
                       .tz_convert("America/New_York"))
                self.assertEqual(set(loc.strftime("%H:%M")), {"09:30"})
                self.assertGreater(len(op), 1000)


if __name__ == "__main__":
    unittest.main()
