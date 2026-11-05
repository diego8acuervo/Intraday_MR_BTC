"""Salida por reloj de sesión y separación unidad-de-riesgo / orden de stop.

Las dos piezas que `IntradayMomentumStrategy` necesita del portfolio y que la
estrategia de reversión no usaba. Ambas fallan en silencio si están mal: sin
`session_flat_at` la posición simplemente se queda abierta toda la noche, y
con `stop_n` haciendo doble trabajo la posición sale varias veces más pequeña
que la validada sin que nada lo avise.
"""
import unittest
from decimal import Decimal

import pandas as pd

from qsCrypto.portfolio.binance_portfolio import BinancePerpetualsPortfolio


class _Ticker:
    interval = "5m"
    _INTERVAL_SECONDS = {"5m": 300, "30m": 1800}

    def __init__(self):
        self.prices = {"BTCUSDT": {"bid": Decimal("100"), "ask": Decimal("100")}}


def _pf(**kw):
    base = dict(ticker=_Ticker(), events=None, equity=Decimal("10000"),
                risk_per_trade=Decimal("0.01"), backtest=False,
                specs={"BTCUSDT": {"stepSize": 0.001, "minNotional": 5.0}})
    base.update(kw)
    return BinancePerpetualsPortfolio(**base)


class TestSessionFlat(unittest.TestCase):

    def test_disabled_by_default(self):
        """El camino de la estrategia de reversión no cambia."""
        p = _pf()
        self.assertIsNone(p.session_flat_at)
        self.assertFalse(p._past_session_close(pd.Timestamp("2026-01-05 20:40")))

    def test_flags_the_last_bar_of_the_session(self):
        p = _pf(session_flat_at="15:45")
        # La vela que ABRE a las 15:40 es la que CIERRA a las 15:45.
        self.assertTrue(p._past_session_close(pd.Timestamp("2026-01-05 20:40")))
        self.assertFalse(p._past_session_close(pd.Timestamp("2026-01-05 20:35")))

    def test_is_dst_correct(self):
        """El mismo 15:40 local cae en dos horas UTC distintas según la época.
        Comparar en UTC a pelo operaría una hora tarde medio año."""
        p = _pf(session_flat_at="15:45")
        self.assertTrue(p._past_session_close(pd.Timestamp("2026-01-05 20:40")))   # EST
        self.assertTrue(p._past_session_close(pd.Timestamp("2026-06-01 19:40")))   # EDT
        # 13:40 EDT está DENTRO de la sesión: no aplana.
        self.assertFalse(p._past_session_close(pd.Timestamp("2026-06-01 17:40")))

    def test_outside_the_session_also_flattens(self):
        """Cualquier barra fuera de [09:30, 15:45) aplana, no solo la última.
        Con solo el borde de cierre, una posición que sobreviviera a la
        medianoche no se cerraría hasta las 15:40 del día siguiente."""
        p = _pf(session_flat_at="15:45", session_open="09:30")
        self.assertTrue(p._past_session_close(pd.Timestamp("2026-01-05 21:00")))  # 16:00
        self.assertTrue(p._past_session_close(pd.Timestamp("2026-01-06 03:00")))  # 22:00 del día previo
        self.assertTrue(p._past_session_close(pd.Timestamp("2026-01-06 13:00")))  # 08:00, antes de abrir
        self.assertFalse(p._past_session_close(pd.Timestamp("2026-01-06 15:00")))  # 10:00, dentro

    def test_bar_size_comes_from_the_handler(self):
        """Con velas de 30 min, la última de una sesión que cierra a las 15:45
        abre a las 15:15 — el umbral tiene que moverse con el tamaño de vela."""
        t = _Ticker(); t.interval = "30m"
        p = _pf(ticker=t, session_flat_at="15:45")
        self.assertTrue(p._past_session_close(pd.Timestamp("2026-01-05 20:15")))
        self.assertFalse(p._past_session_close(pd.Timestamp("2026-01-05 20:10")))


class TestRiskUnitVsStopOrder(unittest.TestCase):

    def test_defaults_to_stop_n(self):
        p = _pf(stop_n=2.0)
        self.assertEqual(p.stop_n_order, 2.0)

    def test_sizing_ignores_the_wide_protective_stop(self):
        """Éste es el fallo que se quiere impedir: un stop ancho como red de
        seguridad NO debe encoger la posición. Con stop_n=1 el tamaño tiene
        que ser el mismo se ponga el stop a 1N o a 4N."""
        n = 10.0
        tight = _pf(stop_n=1.0).calc_risk_position_size("BTCUSDT", n=n)
        wide = _pf(stop_n=1.0, stop_n_order=4.0).calc_risk_position_size("BTCUSDT", n=n)
        self.assertGreater(tight, 0)
        self.assertEqual(tight, wide)
        # ...mientras que meter el 4N en la unidad de riesgo sí la encoge 4x.
        shrunk = _pf(stop_n=4.0).calc_risk_position_size("BTCUSDT", n=n)
        self.assertAlmostEqual(shrunk, tight / 4, places=6)


if __name__ == "__main__":
    unittest.main()
