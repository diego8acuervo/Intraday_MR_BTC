"""Parity: el refactor a reglas de entrada no cambia el motor clásico.

Las fixtures de `fixtures/` se capturaron con el motor ANTERIOR a extraer la
máquina de 3 barras (ruptura -> confirmación -> fade) a `DonchianFadeRule`.
Si estos tests pasan, ese camino es idéntico barra a barra y los dos
backtests ya cerrados (MeanReversion_Binance_Perp, Intraday_Crypto_VWAP_MR)
siguen siendo reproducibles.

Es la única garantía de que añadir una regla de entrada nueva al motor no
movió en silencio el suelo bajo lo que ya estaba publicado.
"""
import os
import unittest

import numpy as np
import pandas as pd

from qsCrypto.backtest.binance_data import fetch_pilot_universe, restrict_to_session
from qsCrypto.backtest.engine import (
    BacktestParams, DonchianFadeRule, run_backtest, run_portfolio_backtest,
)
from qsCrypto.strategy.strategy import vwap_fade_policy

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
PILOT = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT"]
START, END = "2022-01-01", "2026-09-13 00:00:00"

BASELINE = BacktestParams(**{**BacktestParams().__dict__,
                             "tp_mode": "take_n", "take_n": 50.0,
                             "dd_from_peak": True})
# Continuas: se comparan con rtol=1e-12. El CSV de las fixtures no
# round-trippea float64 al bit (el error es ~1e-15 relativo, medido sobre la
# salida del propio motor), así que exigir igualdad exacta aquí mediría la
# serialización y no el motor. 1e-12 sigue siendo doce órdenes de magnitud más
# estricto que cualquier cambio real: una regla de entrada que cambie de
# comportamiento mueve QUÉ operaciones ocurren, no su dígito decimoquinto —
# y eso lo fijan los campos discretos de abajo, que sí se comparan exactos.
NUM = ["qty", "entry", "exit", "pnl", "r_multiple", "n", "mae_n", "mfe_n",
       "fees", "funding_paid"]
# Discretos: cuántas operaciones, cuándo, de qué lado y por qué se cerraron.
# Aquí es donde se vería un cambio de comportamiento, y no admiten tolerancia.
EXACT = ["side", "reason", "units", "bars_held"]
RTOL = 1e-12


def _policy():
    return vwap_fade_policy(min_range_n=2.0, vwap_mode="rolling", vwap_bars=14)


@unittest.skipUnless(os.path.exists(os.path.join(FIX, "golden_trades.csv")),
                     "fixtures doradas no capturadas")
class TestRuleRefactorParity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        d247, funding = fetch_pilot_universe(PILOT, interval="30m",
                                             start=START, end=END)
        cls.D247 = d247
        cls.FUNDING = funding
        cls.DATA = {s: restrict_to_session(x) for s, x in d247.items()}

    def _same(self, got, want):
        self.assertEqual(len(got), len(want))
        for c in NUM:
            np.testing.assert_allclose(got[c].to_numpy(dtype=float),
                                       want[c].to_numpy(dtype=float),
                                       rtol=RTOL, atol=0, err_msg=c)
        for c in EXACT:
            self.assertTrue((got[c].to_numpy() == want[c].to_numpy()).all(), c)
        for c in ("opened", "closed"):
            self.assertTrue((pd.to_datetime(got[c]).to_numpy()
                             == pd.to_datetime(want[c]).to_numpy()).all(), c)

    def test_default_rule_matches_golden(self):
        want_all = pd.read_csv(os.path.join(FIX, "golden_trades.csv"))
        for sym in PILOT:
            with self.subTest(symbol=sym):
                ec, tr = run_backtest(self.DATA[sym], BASELINE,
                                      funding=self.FUNDING[sym],
                                      policy=_policy(), path=self.D247[sym])
                self._same(tr, want_all[want_all["symbol"] == sym]
                           .reset_index(drop=True))
                want_ec = pd.read_csv(os.path.join(FIX, f"golden_ec_{sym}.csv"),
                                      index_col=0)["equity"]
                np.testing.assert_allclose(ec.to_numpy(), want_ec.to_numpy(),
                                           rtol=RTOL, atol=0)

    def test_explicit_rule_equals_implicit(self):
        """Pasar `DonchianFadeRule(policy)` a mano == no pasar regla."""
        sym = "BTCUSDT"
        a, ta = run_backtest(self.DATA[sym], BASELINE, funding=self.FUNDING[sym],
                             policy=_policy(), path=self.D247[sym])
        b, tb = run_backtest(self.DATA[sym], BASELINE, funding=self.FUNDING[sym],
                             path=self.D247[sym], rule=DonchianFadeRule(_policy()))
        # Ambos lados salen del mismo proceso, sin pasar por disco: aquí
        # sí se exige igualdad al bit.
        for c in NUM + EXACT:
            self.assertTrue((tb[c].to_numpy() == ta[c].to_numpy()).all(), c)
        np.testing.assert_allclose(b.to_numpy(), a.to_numpy(), rtol=0, atol=0)

    def test_portfolio_matches_golden(self):
        port = BacktestParams(**{**BASELINE.__dict__, "equity0": 100_000.0,
                                 "max_gross_lev": 4.0})
        ec, tr, _ = run_portfolio_backtest(self.DATA, port, funding=self.FUNDING,
                                           policy=_policy(), paths=self.D247)
        self._same(tr, pd.read_csv(os.path.join(
            FIX, "golden_portfolio_trades.csv")))
        want_ec = pd.read_csv(os.path.join(FIX, "golden_portfolio_ec.csv"),
                              index_col=0)["equity"]
        np.testing.assert_allclose(ec.to_numpy(), want_ec.to_numpy(),
                                   rtol=RTOL, atol=0)

    def test_rule_state_is_per_symbol(self):
        """Una regla CON estado no puede compartirse entre los libros de una
        cartera: la ruptura de un símbolo confirmaría con el cierre de otro.
        El motor la copia por libro, así que el original queda intacto."""
        shared = DonchianFadeRule(_policy())
        port = BacktestParams(**{**BASELINE.__dict__, "equity0": 100_000.0,
                                 "max_gross_lev": 4.0})
        _, tr, _ = run_portfolio_backtest(self.DATA, port, funding=self.FUNDING,
                                          paths=self.D247, rule=shared)
        self.assertIsNone(shared.pending)
        self._same(tr, pd.read_csv(os.path.join(
            FIX, "golden_portfolio_trades.csv")))


if __name__ == "__main__":
    unittest.main()
