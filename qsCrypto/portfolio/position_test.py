from decimal import Decimal
import unittest

from qsCrypto.portfolio.position import Position


class TickerMock(object):
    """
    A mock object that allows a representation of the
    ticker/pricing handler.
    """

    def __init__(self):
        self.pairs = ["GBPUSD", "EURUSD"]
        self.prices = {
            "GBPUSD": {"bid": Decimal("1.50328"), "ask": Decimal("1.50349")},
            "USDGBP": {"bid": Decimal("0.66521"), "ask": Decimal("0.66512")},
            "EURUSD": {"bid": Decimal("1.07832"), "ask": Decimal("1.07847")}
        }



# =====================================
# GBP Home Currency with GBP/USD traded
# =====================================

class TestLongGBPUSDPosition(unittest.TestCase):
    """
    Unit tests that cover going long GBP/USD with an account
    denominated currency of GBP, using 2,000 units of GBP/USD.
    """
    def setUp(self):
        home_currency = "GBP"
        position_type = "long"
        currency_pair = "GBPUSD"
        units = Decimal("2000")
        ticker = TickerMock()
        self.position = Position(
            home_currency, position_type, 
            currency_pair, units, ticker
        )

    def test_calculate_init_pips(self):
        pos_pips = self.position.calculate_pips()
        self.assertEqual(pos_pips, Decimal("-0.00021"))

    def test_calculate_init_profit_base(self):
        profit_base = self.position.calculate_profit_base()
        self.assertEqual(profit_base, Decimal("-0.27939"))

    def test_calculate_init_profit_perc(self):
        profit_perc = self.position.calculate_profit_perc()
        self.assertEqual(profit_perc, Decimal("-0.01397"))

    def test_calculate_updated_values(self):
        """
        Check that after the bid/ask prices move, that the updated
        pips, profit and percentage profit calculations are correct.
        """
        prices = self.position.ticker.prices
        prices["GBPUSD"] = {"bid": Decimal("1.50486"), "ask": Decimal("1.50586")}
        prices["USDGBP"] = {"bid": Decimal("0.66451"), "ask": Decimal("0.66407")}
        self.position.update_position_price()

        # Check pips
        pos_pips = self.position.calculate_pips()
        self.assertEqual(pos_pips, Decimal("0.00137"))
        # Check profit base
        profit_base = self.position.calculate_profit_base()
        self.assertEqual(profit_base, Decimal("1.82076"))
        # Check profit percentage
        profit_perc = self.position.calculate_profit_perc()
        self.assertEqual(profit_perc, Decimal("0.09104"))


class TestShortGBPUSDPosition(unittest.TestCase):
    """
    Unit tests that cover going short GBP/USD with an account
    denominated currency of GBP, using 2,000 units of GBP/USD.
    """
    def setUp(self):
        home_currency = "GBP"
        position_type = "short"
        currency_pair = "GBPUSD"
        units = Decimal("2000")
        ticker = TickerMock()
        self.position = Position(
            home_currency, position_type, 
            currency_pair, units, ticker
        )

    def test_calculate_init_pips(self):
        pos_pips = self.position.calculate_pips()
        self.assertEqual(pos_pips, Decimal("-0.00021"))

    def test_calculate_init_profit_base(self):
        profit_base = self.position.calculate_profit_base()
        self.assertEqual(profit_base, Decimal("-0.27935"))

    def test_calculate_init_profit_perc(self):
        profit_perc = self.position.calculate_profit_perc()
        self.assertEqual(profit_perc, Decimal("-0.01397"))

    def test_calculate_updated_values(self):
        """
        Check that after the bid/ask prices move, that the updated
        pips, profit and percentage profit calculations are correct.
        """
        prices = self.position.ticker.prices
        prices["GBPUSD"] = {"bid": Decimal("1.50486"), "ask": Decimal("1.50586")}
        prices["USDGBP"] = {"bid": Decimal("0.66451"), "ask": Decimal("0.66407")}
        self.position.update_position_price()

        # Check pips
        pos_pips = self.position.calculate_pips()
        self.assertEqual(pos_pips, Decimal("-0.00258"))
        # Check profit base
        profit_base = self.position.calculate_profit_base()
        self.assertEqual(profit_base, Decimal("-3.42660"))
        # Check profit percentage
        profit_perc = self.position.calculate_profit_perc()
        self.assertEqual(profit_perc, Decimal("-0.17133"))


# =====================================
# GBP Home Currency with EUR/USD traded
# =====================================

class TestLongEURUSDPosition(unittest.TestCase):
    """
    Unit tests that cover going long EUR/USD with an account
    denominated currency of GBP, using 2,000 units of EUR/USD.
    """
    def setUp(self):
        home_currency = "GBP"
        position_type = "long"
        currency_pair = "EURUSD"
        units = Decimal("2000")
        ticker = TickerMock()
        self.position = Position(
            home_currency, position_type, 
            currency_pair, units, ticker
        )

    def test_calculate_init_pips(self):
        pos_pips = self.position.calculate_pips()
        self.assertEqual(pos_pips, Decimal("-0.00015"))

    def test_calculate_init_profit_base(self):
        profit_base = self.position.calculate_profit_base()
        self.assertEqual(profit_base, Decimal("-0.19956"))

    def test_calculate_init_profit_perc(self):
        profit_perc = self.position.calculate_profit_perc()
        self.assertEqual(profit_perc, Decimal("-0.00998"))

    def test_calculate_updated_values(self):
        """
        Check that after the bid/ask prices move, that the updated
        pips, profit and percentage profit calculations are correct.
        """
        prices = self.position.ticker.prices
        prices["GBPUSD"] = {"bid": Decimal("1.50486"), "ask": Decimal("1.50586")}
        prices["USDGBP"] = {"bid": Decimal("0.66451"), "ask": Decimal("0.66407")}
        prices["EURUSD"] = {"bid": Decimal("1.07811"), "ask": Decimal("1.07827")}
        self.position.update_position_price()

        # Check pips
        pos_pips = self.position.calculate_pips()
        self.assertEqual(pos_pips, Decimal("-0.00036"))
        # Check profit base
        profit_base = self.position.calculate_profit_base()
        self.assertEqual(profit_base, Decimal("-0.47845"))
        # Check profit percentage
        profit_perc = self.position.calculate_profit_perc()
        self.assertEqual(profit_perc, Decimal("-0.02392"))


class TestLongEURUSDPosition(unittest.TestCase):
    """
    Unit tests that cover going short EUR/USD with an account
    denominated currency of GBP, using 2,000 units of EUR/USD.
    """
    def setUp(self):
        home_currency = "GBP"
        position_type = "short"
        currency_pair = "EURUSD"
        units = Decimal("2000")
        ticker = TickerMock()
        self.position = Position(
            home_currency, position_type, 
            currency_pair, units, ticker
        )

    def test_calculate_init_pips(self):
        pos_pips = self.position.calculate_pips()
        self.assertEqual(pos_pips, Decimal("-0.00015"))

    def test_calculate_init_profit_base(self):
        profit_base = self.position.calculate_profit_base()
        self.assertEqual(profit_base, Decimal("-0.19954"))

    def test_calculate_init_profit_perc(self):
        profit_perc = self.position.calculate_profit_perc()
        self.assertEqual(profit_perc, Decimal("-0.00998"))

    def test_calculate_updated_values(self):
        """
        Check that after the bid/ask prices move, that the updated
        pips, profit and percentage profit calculations are correct.
        """
        prices = self.position.ticker.prices
        prices["GBPUSD"] = {"bid": Decimal("1.50486"), "ask": Decimal("1.50586")}
        prices["USDGBP"] = {"bid": Decimal("0.66451"), "ask": Decimal("0.66407")}
        prices["EURUSD"] = {"bid": Decimal("1.07811"), "ask": Decimal("1.07827")}
        self.position.update_position_price()

        # Check pips
        pos_pips = self.position.calculate_pips()
        self.assertEqual(pos_pips, Decimal("0.00005"))
        # Check profit base
        profit_base = self.position.calculate_profit_base()
        self.assertEqual(profit_base, Decimal("0.06641"))
        # Check profit percentage
        profit_perc = self.position.calculate_profit_perc()
        self.assertEqual(profit_perc, Decimal("0.00332"))


# =====================================
# MAE / MFE tracking (Quant_test Part A)
# =====================================

class TestPositionExcursions(unittest.TestCase):
    """MAE/MFE monitoring, the live counterpart of qsCrypto/portfolio/mae.py."""

    def setUp(self):
        self.ticker = TickerMock()
        # Long at the ask (1.50349) with a 1N stop 0.00500 below it.
        self.position = Position(
            "GBP", "long", "GBPUSD", Decimal("2000"), self.ticker,
            initial_stop="1.49849",
        )

    def _move(self, bid, ask):
        self.ticker.prices["GBPUSD"] = {"bid": Decimal(bid), "ask": Decimal(ask)}
        self.position.update_position_price()

    def test_r_unit_is_entry_to_stop_distance(self):
        self.assertEqual(self.position.entry_anchor, Decimal("1.50349"))
        self.assertEqual(self.position.r_unit, Decimal("0.00500"))

    def test_excursion_tracks_adverse_move(self):
        self._move("1.49900", "1.49920")
        self.assertEqual(self.position.mae_base, Decimal("0.00449"))
        self.assertEqual(self.position.calculate_mae_r(), Decimal("0.89800"))

    def test_mae_ratchets_and_does_not_recover(self):
        """Once seen, an adverse excursion is history and must not shrink."""
        self._move("1.49900", "1.49920")
        worst = self.position.calculate_mae_r()
        self._move("1.50900", "1.50920")
        self.assertEqual(self.position.calculate_mae_r(), worst)
        self.assertEqual(self.position.calculate_mfe_r(), Decimal("1.10200"))

    def test_pnl_in_r(self):
        self._move("1.50900", "1.50920")
        self.assertEqual(self.position.calculate_pnl_r(), Decimal("1.10200"))

    def test_excursion_never_negative(self):
        """A favourable move cannot drive MAE below its floor of zero.

        The floor here is the entry spread, not zero: a long fills at the ask
        (1.50349) and is marked at the bid (1.50328), so it opens 0.00021
        underwater. That is a real adverse excursion and the live monitor
        should show it from the first tick.
        """
        self.assertEqual(self.position.mae_base, Decimal("0.00021"))
        self._move("1.50900", "1.50920")
        self.assertEqual(self.position.mae_base, Decimal("0.00021"))

    def test_excursion_is_zero_when_marked_at_entry(self):
        pos = Position("GBP", "long", "GBPUSD", Decimal("2000"), self.ticker,
                       initial_stop="1.49849")
        pos.entry_anchor = pos.cur_price      # no spread to pay
        pos.update_excursions()
        self.assertEqual(pos.mae_base, Decimal("0.00000"))

    def test_short_position_is_mirrored(self):
        pos = Position("GBP", "short", "GBPUSD", Decimal("2000"), self.ticker,
                       initial_stop="1.50828")
        self.ticker.prices["GBPUSD"] = {"bid": Decimal("1.50700"),
                                        "ask": Decimal("1.50720")}
        pos.update_position_price()
        # a short loses as price rises, so the ask moving up is adverse
        self.assertGreater(pos.mae_base, Decimal("0"))
        self.assertEqual(pos.mfe_base, Decimal("0.00000"))

    def test_no_stop_leaves_r_undefined(self):
        """Without an initial stop, R has no meaning — return None, not zero."""
        pos = Position("GBP", "long", "GBPUSD", Decimal("2000"), self.ticker)
        self._move("1.49900", "1.49920")
        pos.update_position_price()
        self.assertIsNone(pos.r_unit)
        self.assertIsNone(pos.calculate_mae_r())
        self.assertGreater(pos.mae_base, Decimal("0"))   # still tracked in price

    def test_partial_fill_reanchor_keeps_r_reading_consistent(self):
        self.position.reanchor_entry("1.50449")
        self.assertEqual(self.position.entry_anchor, Decimal("1.50449"))
        self.assertEqual(self.position.r_unit, Decimal("0.00600"))

    def test_matches_the_shared_mae_module(self):
        """Position (Decimal) and Excursion (float) must agree on the same path.

        Two implementations of one definition is a bug waiting to happen; this
        is the test that would catch them drifting apart.
        """
        from qsCrypto.portfolio.mae import Excursion

        path = [("1.50100", "1.50120"), ("1.49900", "1.49920"),
                ("1.50900", "1.50920")]
        for bid, ask in path:
            self._move(bid, ask)

        exc = Excursion.open(side=1, fill=1.50349, n=0.00500, stop_n=1.0)
        for bid, _ in path:
            exc.update_tick(float(bid))

        self.assertAlmostEqual(float(self.position.calculate_mae_r()), exc.mae_r, places=5)
        self.assertAlmostEqual(float(self.position.calculate_mfe_r()), exc.mfe_r, places=5)


if __name__ == "__main__":
    unittest.main()
