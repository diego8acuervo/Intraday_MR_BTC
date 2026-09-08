from decimal import Decimal, getcontext, ROUND_HALF_DOWN


class Position(object):
    def __init__(
        self, home_currency, position_type,
        currency_pair, units, ticker, initial_stop=None,
        multiplier=Decimal("1")
    ):
        self.home_currency = home_currency  # Account denomination (e.g. USDT)
        self.position_type = position_type  # Long or short
        self.currency_pair = currency_pair  # Intended traded currency pair (e.g. BTCUSDT)
        self.units = units
        self.ticker = ticker
        # Contract multiplier. Spot pairs are 1; a CME MBT future is 0.1 BTC per
        # contract, so without this the P&L of a futures position sale 10x the
        # real one. Defaults to 1 so every existing caller is unaffected.
        self.multiplier = Decimal(str(multiplier))
        self.set_up_currencies()
        # `initial_stop` is optional and defaults to None so that existing
        # positional callers (Portfolio.add_new_position) keep working. Without
        # it the excursion is still tracked in price, but R is undefined and
        # calculate_mae_r() returns None rather than a misleading zero.
        self.set_up_excursion(initial_stop)
        self.profit_base = self.calculate_profit_base()
        self.profit_perc = self.calculate_profit_perc()

    @staticmethod
    def _looks_like_fx_pair(symbol):
        """True for a six-letter code that splits 3/3 into two currencies."""
        return len(symbol) >= 6 and symbol.isalpha()

    def set_up_currencies(self):
        """Split the instrument into base/quote.

        The 3/3 slice only means anything for a six-character FX-style code
        (GBPUSD, BTCUSDT by luck). A futures root like `MBT` or a local symbol
        like `MBTU6` would yield base `MBT`/quote `""` or base `MBT`/quote `U6`,
        and `calculate_profit_base` would then look up a `quote_home_currency_pair`
        that does not exist and raise a KeyError on the first tick. Anything that
        is not a recognisable pair is treated as denominated in the account
        currency, which is the branch that already sets qh_close = 1.0.
        """
        if self._looks_like_fx_pair(self.currency_pair):
            self.base_currency = self.currency_pair[:3]    # For BTC/USDT, this is BTC
            self.quote_currency = self.currency_pair[3:]   # For BTC/USDT, this is USDT
        else:
            self.base_currency = self.currency_pair
            self.quote_currency = self.home_currency
        # For crypto pairs where quote == home (e.g. BTCUSDT with USDT account),
        # no conversion is needed.
        self.quote_home_currency_pair = "%s%s" % (self.quote_currency, self.home_currency)

        ticker_cur = self.ticker.prices[self.currency_pair]
        if self.position_type == "long":
            self.avg_price = Decimal(str(ticker_cur["ask"]))
            self.cur_price = Decimal(str(ticker_cur["bid"]))    
        else:
            self.avg_price = Decimal(str(ticker_cur["bid"]))
            self.cur_price = Decimal(str(ticker_cur["ask"]))

    def set_up_excursion(self, initial_stop=None):
        """Freeze the excursion frame of reference at the entry fill.

        `entry_anchor` and `r_unit` are frozen deliberately: adds move the
        average price, but the excursion of a trade is measured against where
        that trade started, otherwise pyramiding would retroactively rewrite an
        MAE that was already observed. See qsCrypto/portfolio/mae.py, which
        holds the authoritative definition; this is its Decimal counterpart for
        the live tick path.
        """
        self.entry_anchor = self.avg_price
        self.initial_stop = (
            None if initial_stop is None else Decimal(str(initial_stop))
        )
        # R = entry-to-initial-stop distance, the unit the excursion is read in.
        self.r_unit = (
            None if self.initial_stop is None
            else abs(self.entry_anchor - self.initial_stop)
        )
        # Worst adverse / best favourable price seen so far.
        self.mae_price = self.cur_price
        self.mfe_price = self.cur_price
        self.mae_base = Decimal("0.00000")
        self.mfe_base = Decimal("0.00000")
        self.update_excursions()

    def reanchor_entry(self, vwap):
        """Reset the anchor to a volume-weighted average of partial fills.

        A partial fill changes the risk in account currency (it scales with
        filled quantity) but not the reading in R, which is normalised by a
        price distance. Call this while the ENTRY order is still filling; do
        not call it for pyramiding adds, which must leave the anchor alone.
        """
        self.entry_anchor = Decimal(str(vwap))
        if self.initial_stop is not None:
            self.r_unit = abs(self.entry_anchor - self.initial_stop)
        self.update_excursions()

    def update_excursions(self):
        """Roll the path extremes forward with the current price.

        Called from update_position_price(), which is the single funnel every
        price move passes through, so the excursion cannot silently go stale.
        """
        if self.position_type == "long":
            if self.cur_price < self.mae_price:
                self.mae_price = self.cur_price
            if self.cur_price > self.mfe_price:
                self.mfe_price = self.cur_price
            mae = self.entry_anchor - self.mae_price
            mfe = self.mfe_price - self.entry_anchor
        else:
            if self.cur_price > self.mae_price:
                self.mae_price = self.cur_price
            if self.cur_price < self.mfe_price:
                self.mfe_price = self.cur_price
            mae = self.mae_price - self.entry_anchor
            mfe = self.entry_anchor - self.mfe_price
        # An excursion is never negative: before the price has moved against
        # the position at all, the MAE is zero, not a favourable number.
        self.mae_base = max(mae, Decimal("0")).quantize(
            Decimal("0.00001"), ROUND_HALF_DOWN
        )
        self.mfe_base = max(mfe, Decimal("0")).quantize(
            Decimal("0.00001"), ROUND_HALF_DOWN
        )

    def calculate_mae_r(self):
        """Maximum Adverse Excursion in R. None when no initial stop was set."""
        if self.r_unit is None or self.r_unit == 0:
            return None
        return (self.mae_base / self.r_unit).quantize(
            Decimal("0.00001"), ROUND_HALF_DOWN
        )

    def calculate_mfe_r(self):
        """Maximum Favourable Excursion in R. None when no initial stop was set."""
        if self.r_unit is None or self.r_unit == 0:
            return None
        return (self.mfe_base / self.r_unit).quantize(
            Decimal("0.00001"), ROUND_HALF_DOWN
        )

    def calculate_pnl_r(self):
        """Unrealised P&L in R, the number the live monitor leads with."""
        if self.r_unit is None or self.r_unit == 0:
            return None
        return (self.calculate_pips() / self.r_unit).quantize(
            Decimal("0.00001"), ROUND_HALF_DOWN
        )

    def calculate_pips(self):
        mult = Decimal("1")
        if self.position_type == "long":
            mult = Decimal("1")
        elif self.position_type == "short":
            mult = Decimal("-1")
        pips = (mult * (self.cur_price - self.avg_price)).quantize(
            Decimal("0.00001"), ROUND_HALF_DOWN
        )
        return pips

    def calculate_profit_base(self):
        pips = self.calculate_pips()
        # If quote currency == home currency (e.g. BTCUSDT with USDT account),
        # no conversion is needed — qh_close is simply 1.0
        if self.quote_currency == self.home_currency:
            qh_close = Decimal("1.0")
        else:
            ticker_qh = self.ticker.prices[self.quote_home_currency_pair]
            if self.position_type == "long":
                qh_close = ticker_qh["bid"]
            else:
                qh_close = ticker_qh["ask"]
        profit = pips * qh_close * self.units * self.multiplier
        return profit.quantize(
            Decimal("0.00001"), ROUND_HALF_DOWN
        )   

    def calculate_profit_perc(self):
        if self.units == 0:
            return Decimal("0.00000")
        return (self.profit_base / self.units * Decimal("100.00")).quantize(
            Decimal("0.00001"), ROUND_HALF_DOWN
        )

    def update_position_price(self):
        ticker_cur = self.ticker.prices[self.currency_pair]
        if self.position_type == "long":
            self.cur_price = Decimal(str(ticker_cur["bid"]))
        else:
            self.cur_price = Decimal(str(ticker_cur["ask"]))
        self.profit_base = self.calculate_profit_base()
        self.profit_perc = self.calculate_profit_perc()
        self.update_excursions()

    def add_units(self, units):
        cp = self.ticker.prices[self.currency_pair]
        if self.position_type == "long":
            add_price = cp["ask"]
        else:
            add_price = cp["bid"]
        new_total_units = self.units + units
        new_total_cost = self.avg_price*self.units + add_price*units
        self.avg_price = new_total_cost/new_total_units
        self.units = new_total_units
        self.update_position_price()

    def remove_units(self, units):
        dec_units = Decimal(str(units))
        ticker_cp = self.ticker.prices[self.currency_pair]
        # If quote currency == home currency, no conversion needed
        if self.quote_currency == self.home_currency:
            qh_close = Decimal("1.0")
        else:
            ticker_qh = self.ticker.prices[self.quote_home_currency_pair]
            if self.position_type == "long":
                qh_close = ticker_qh["ask"]
            else:
                qh_close = ticker_qh["bid"]
        if self.position_type == "long":
            remove_price = ticker_cp["bid"]
        else:
            remove_price = ticker_cp["ask"]
        self.units -= dec_units
        self.update_position_price()
        # Calculate PnL
        pnl = self.calculate_pips() * qh_close * dec_units
        getcontext().rounding = ROUND_HALF_DOWN
        return pnl.quantize(Decimal("0.01"))

    def close_position(self):
        ticker_cp = self.ticker.prices[self.currency_pair]
        # If quote currency == home currency, no conversion needed
        if self.quote_currency == self.home_currency:
            qh_close = Decimal("1.0")
        else:
            ticker_qh = self.ticker.prices[self.quote_home_currency_pair]
            if self.position_type == "long":
                qh_close = ticker_qh["ask"]
            else:
                qh_close = ticker_qh["bid"]
        self.update_position_price()
        # Calculate PnL
        pnl = self.calculate_pips() * qh_close * self.units
        getcontext().rounding = ROUND_HALF_DOWN
        return pnl.quantize(Decimal("0.01"))
