from __future__ import print_function

import logging
import math
from decimal import Decimal

from qsCrypto.event.event import OrderEvent, PortfolioEvent
from qsCrypto.portfolio.portfolio import Portfolio


class BinancePerpetualsPortfolio(Portfolio):
    """Portfolio for Binance USDⓈ-M perpetuals, mirroring IBKRFuturesPortfolio
    (qsCrypto/portfolio/portfolio.py) but replacing everything that class
    assumes about a futures contract:

    1. **No multiplier.** Perpetuals are quoted directly in the base asset —
       quantity is base-asset units (fractional, exchange-step-rounded), not
       integer contracts times a fixed multiplier. `multiplier()` has no
       analog here at all; `tick_size()`/`step_size()` replace `min_tick()`.
    2. **No expiry/rollover.** There is no `front_month()` analog — specs
       come from a single exchangeInfo fetch, never re-resolved for a roll.
    3. **Broker-managed stop AND target.** IBKR only needed a native stop
       (the mid-channel target was software-checked in on_bar()). Here both
       STOP_MARKET and TAKE_PROFIT_MARKET are placed as broker orders at
       entry (see BinancePerpetualExecutionHandler.execute_order), so
       on_bar()'s stop/target checks become a fallback/logging path only —
       the authoritative exit is the FillEvent from the broker, not on_bar()
       calling _exit() directly. Mixing the two up would double-count exits.
    4. **Funding.** Perpetuals carry a recurring funding-rate cash flow with
       no CME analog; apply_funding() accounts for it explicitly rather than
       ignoring it as the IBKR class implicitly does (no funding on futures).
    5. **Simpler reconciliation.** Binance's positionRisk `entryPrice` is
       already exchange-VWAP-averaged, so there is no need for IBKR's
       `_vwap_from_executions` walk over the day's fills.

    Safety note (explicit user instruction for the testnet/live validation
    phase, not a permanent default): pass `max_order_notional` to cap every
    order at a fixed USDT notional, and `max_units=1` to disable pyramiding
    so a stopped-out position never risks more than 1N. Both are ordinary
    constructor kwargs — loosen them only after the go/no-go checklist in
    the port plan is satisfied.
    """

    def __init__(self, ticker, events, home_currency="USDT", leverage=1,
                 equity=Decimal("100000.00"), risk_per_trade=Decimal("0.01"),
                 backtest=False, specs=None, stop_n=1.0, add_n=0.5,
                 max_units=4, max_hold_bars=10, tp_mode="mid_channel",
                 take_n=5.0, max_gross_lev=4.0, max_dir_units=12,
                 max_corr_units=6, corr_groups=None, use_dd_rule=True,
                 execution=None, strategy=None, max_order_notional=None):
        super(BinancePerpetualsPortfolio, self).__init__(
            ticker, events, home_currency=home_currency, leverage=leverage,
            equity=equity, risk_per_trade=risk_per_trade, backtest=backtest
        )
        self.specs = specs or {}
        self.stop_n = float(stop_n)
        self.add_n = float(add_n)
        self.max_units = int(max_units)
        self.max_hold_bars = int(max_hold_bars)
        self.tp_mode = tp_mode
        self.take_n = float(take_n)
        self.max_gross_lev = float(max_gross_lev)
        self.max_dir_units = int(max_dir_units)
        self.max_corr_units = int(max_corr_units)
        self.corr_groups = corr_groups or {}
        self.use_dd_rule = bool(use_dd_rule)
        self.execution = execution
        self.strategy = strategy
        self.equity0 = Decimal(str(equity))
        # Hard per-order notional cap. None (default) leaves sizing
        # unconstrained beyond risk/leverage; the testnet validation phase
        # must pass an explicit USDT value here (see class docstring).
        self.max_order_notional = (
            float(max_order_notional) if max_order_notional is not None else None
        )
        self.logger = logging.getLogger(__name__)
        # Per-symbol state: frozen N/level/mid, ladder units, last fill, bars
        # held — the same shape as IBKRFuturesPortfolio.meta.
        self.meta = {}
        self.rejected = {"qty_cero": 0, "apalancamiento": 0, "topes": 0}
        self.funding_paid = Decimal("0")

    # ── sizing ──────────────────────────────────────────────────────────────
    def tick_size(self, pair):
        return float(self.specs.get(pair, {}).get("tickSize", 0.0))

    def step_size(self, pair):
        return float(self.specs.get(pair, {}).get("stepSize", 0.0))

    def min_notional(self, pair):
        return float(self.specs.get(pair, {}).get("minNotional", 0.0))

    def sizing_equity(self):
        """Turtle drawdown-reduction rule, copied verbatim from
        IBKRFuturesPortfolio — pure equity-curve math, no venue dependency.
        Each -10% of equity reduces notional by 20%, floored at 5% of the
        initial equity."""
        eq = float(self.balance)
        if not self.use_dd_rule:
            return max(eq, float(self.equity0) * 0.05)
        eq0 = float(self.equity0)
        adj = eq
        if eq < eq0:
            steps = int((eq0 - eq) / (0.10 * eq0))
            adj = eq * (0.80 ** steps)
        return max(adj, eq0 * 0.05)

    def _round_step(self, pair, qty):
        """Round down to the exchange step size. Delegates to the execution
        handler's Decimal-precise round_quantity() when one is wired in;
        falls back to a local floor-round otherwise (e.g. isolated tests)."""
        if self.execution is not None and hasattr(self.execution, "round_quantity"):
            return float(self.execution.round_quantity(pair, qty))
        step = self.step_size(pair)
        if step <= 0:
            return qty
        return math.floor(qty / step) * step

    def calc_risk_position_size(self, currency_pair, n=None):
        """Fractional base-asset quantity whose 1N move costs risk_per_trade
        of sizing_equity() — no multiplier term (see class docstring point 1).
        Floor-rounded to the exchange step size; rejected (0) if it would
        fall below minQty/minNotional after rounding, or if it exceeds
        max_order_notional when that safety cap is set.
        """
        if n is None or not (n == n) or n <= 0:
            return 0.0
        denom = self.stop_n * float(n)
        if denom <= 0:
            return 0.0

        raw_qty = float(self.risk_per_trade) * self.sizing_equity() / denom

        px = self._ref_price(currency_pair, "buy")
        if self.max_order_notional is not None and px is not None and float(px) > 0:
            cap_qty = self.max_order_notional / float(px)
            raw_qty = min(raw_qty, cap_qty)

        qty = self._round_step(currency_pair, raw_qty)
        min_qty = float(self.specs.get(currency_pair, {}).get("minQty", 0.0))
        if qty <= 0 or qty < min_qty:
            return 0.0

        if px is not None and self.min_notional(currency_pair) > 0:
            if qty * float(px) < self.min_notional(currency_pair):
                return 0.0

        return qty

    def _fits_leverage(self, pair, qty, price):
        """No multiplier term (perpetuals: notional = qty * price directly).
        Also checks Binance's tiered margin brackets on top of the flat
        max_gross_lev cap, when the execution handler exposes them — this
        is genuinely new logic with no IBKR analog (CME margin has no
        notion of a leverage-tier schedule in this codebase)."""
        gross = sum(
            abs(float(p.units)) * float(p.cur_price)
            for sym, p in self.positions.items()
        )
        gross += abs(qty) * float(price)
        if gross > self.max_gross_lev * float(self.balance):
            return False

        if self.execution is not None and hasattr(self.execution, "get_leverage_brackets"):
            try:
                brackets = self.execution.get_leverage_brackets(pair)
                notional = abs(qty) * float(price)
                applicable = next(
                    (b for b in brackets
                     if b.get("notionalFloor", 0) <= notional <= b.get("notionalCap", float("inf"))),
                    None,
                )
                if applicable is not None:
                    max_lev = float(applicable.get("initialLeverage", self.leverage))
                    required_margin = notional / max_lev
                    if required_margin > float(self.balance):
                        return False
            except Exception as exc:
                self.logger.warning(
                    "No se pudo verificar el bracket de margen para %s: %s "
                    "(se sigue solo con max_gross_lev).", pair, exc)

        return True

    def _unit_counts(self):
        longs = sum(m["units"] for s, m in self.meta.items()
                    if s in self.positions and m["side"] > 0)
        shorts = sum(m["units"] for s, m in self.meta.items()
                     if s in self.positions and m["side"] < 0)
        return longs, shorts

    def _corr_unit_count(self, pair, side):
        group = self.corr_groups.get(pair)
        if group is None:
            return 0
        return sum(
            m["units"] for s, m in self.meta.items()
            if s in self.positions and m["side"] == side
            and self.corr_groups.get(s) == group
        )

    # ── entry ───────────────────────────────────────────────────────────────
    def execute_signal(self, signal_event):
        pair = signal_event.instrument
        side = 1 if signal_event.side == "buy" else -1
        n = signal_event.n

        if pair in self.positions:
            return  # one position per symbol

        px = self._ref_price(pair, signal_event.side)
        if px is None:
            self.logger.info("No hay precio utilizable para %s: no se opera.", pair)
            return

        qty = self.calc_risk_position_size(pair, n=n)
        if qty <= 0:
            self.rejected["qty_cero"] += 1
            self.logger.warning("qty=0 para %s con N=%s: señal descartada.", pair, n)
            return

        longs, shorts = self._unit_counts()
        if (side > 0 and longs + 1 > self.max_dir_units) or \
           (side < 0 and shorts + 1 > self.max_dir_units):
            self.rejected["topes"] += 1
            return

        corr_units = self._corr_unit_count(pair, side)
        if corr_units + 1 > self.max_corr_units:
            self.rejected["topes"] += 1
            self.logger.warning(
                "Tope de grupo correlacionado alcanzado para %s (%d unidades).",
                pair, corr_units)
            return

        if not self._fits_leverage(pair, qty, px):
            self.rejected["apalancamiento"] += 1
            self.logger.warning("Apalancamiento: %s qty=%s rechazado.", pair, qty)
            return

        stop_px = float(px) - side * self.stop_n * float(n)
        take_px = None
        if self.tp_mode == "mid_channel" and signal_event.mid is not None:
            take_px = float(signal_event.mid)
        elif self.tp_mode != "mid_channel":
            take_px = float(px) + side * self.take_n * float(n)

        self.add_new_position(
            "long" if side > 0 else "short", pair, Decimal(str(qty)), self.ticker,
            initial_stop=stop_px,
        )
        self.meta[pair] = {
            "side": side, "n": float(n), "level": signal_event.level,
            "mid": signal_event.mid, "units": 1, "last_fill": float(px),
            "stop": stop_px, "take": take_px, "bars": 0, "qty": qty,
        }
        order = OrderEvent(pair, qty, "market", signal_event.side)
        order.stop_price = stop_px
        order.take_price = take_px
        order.n = float(n)
        self.events.put(order)
        self._sync_strategy()
        self._emit_state("entry")
        self.logger.info("ENTRADA %s %s qty=%s stop=%.6f take=%s R=%.6f",
                          signal_event.side, pair, qty, stop_px, take_px,
                          self.stop_n * float(n))

    def _ref_price(self, pair, side):
        p = self.ticker.prices.get(pair, {})
        px = p.get("ask") if side == "buy" else p.get("bid")
        if px is None:
            return None
        px = Decimal(str(px))
        return None if not px.is_finite() else px

    # ── bars: fallback barrier checks + vertical exit + pyramiding ─────────
    def on_bar(self, bar_event):
        """Vertical time-barrier exit and Turtle unit-adding, evaluated on
        closed bars. Stop/target are broker-managed orders (see class
        docstring point 3) — this method logs a warning if it *looks* like
        stop/take should have been hit but no FillEvent has closed the
        position yet (a genuine anomaly worth surfacing, e.g. the broker
        order failed to place), but it does not call _exit() for those
        cases: the FillEvent from update_fill() is the only authoritative
        exit signal for stop/target hits.
        """
        pair = bar_event.instrument
        if pair not in self.positions or pair not in self.meta:
            return
        m = self.meta[pair]
        m["bars"] += 1
        side, sd = m["side"], float(m["side"])
        hi, lo = float(bar_event.high), float(bar_event.low)
        flat = hi <= lo and (bar_event.volume or 0) <= 0

        stop_px = m.get("stop")
        looks_stopped = (not flat) and stop_px is not None and (
            (lo <= stop_px) if side > 0 else (hi >= stop_px))
        take_px = m.get("take")
        looks_taken = (not flat) and take_px is not None and (
            (hi >= take_px) if side > 0 else (lo <= take_px))

        if looks_stopped or looks_taken:
            self.logger.warning(
                "%s: la barra cruzó %s (stop=%s take=%s) pero la posición "
                "sigue local — esperando el FillEvent del broker antes de "
                "cerrarla. Si esto persiste, la orden bracket puede no "
                "haberse colocado (ver reconcile_from_broker).",
                pair, "stop" if looks_stopped else "take", stop_px, take_px)
            return

        if m["bars"] >= self.max_hold_bars:
            self._exit(pair, float(bar_event.close), "vertical")
        elif not flat:
            self._try_add(pair, bar_event)

    def _try_add(self, pair, bar_event):
        """Turtle ladder: +1 unit each 1/2N, up to max_units. Identical logic
        to IBKRFuturesPortfolio._try_add — venue-agnostic, just no
        multiplier term in the sizing call it makes."""
        m = self.meta[pair]
        if m["units"] >= self.max_units or m.get("n") is None:
            return
        side, sd = m["side"], float(m["side"])
        if m["mid"] is not None:
            reached = (float(bar_event.high) >= m["mid"] if side > 0
                       else float(bar_event.low) <= m["mid"])
            if reached:
                return
        trig = m["last_fill"] + sd * self.add_n * m["n"]
        hit = (float(bar_event.high) >= trig if side > 0
               else float(bar_event.low) <= trig)
        if not hit:
            return
        qty = self.calc_risk_position_size(pair, n=m["n"])
        if qty <= 0:
            return
        fill_ref = max(trig, float(bar_event.open)) if side > 0 else \
            min(trig, float(bar_event.open))
        if not self._fits_leverage(pair, qty, fill_ref):
            self.rejected["apalancamiento"] += 1
            return
        order = OrderEvent(pair, qty, "market", "buy" if side > 0 else "sell")
        order.is_add = True
        order.stop_price = fill_ref - sd * self.stop_n * m["n"]
        order.take_price = m.get("take")
        self.events.put(order)
        self.logger.info("ADD %s unidad %d en %.6f", pair, m["units"] + 1, fill_ref)

    def _exit(self, pair, price, reason):
        m = self.meta.pop(pair, None)
        if pair in self.positions:
            try:
                self.close_position(pair)
            except Exception as exc:
                self.logger.error(
                    "close_position(%s) falló: %s — se purga localmente "
                    "sin PnL exacto.", pair, exc)
                self.positions.pop(pair, None)
        if self.execution is not None:
            try:
                self.execution.flatten(pair, reason=reason)
            except Exception as exc:
                self.logger.error("flatten(%s) falló: %s", pair, exc)
        self._sync_strategy()
        self._emit_state("exit:%s" % reason)
        self.logger.info("SALIDA %s por %s en %.6f", pair, reason, price)

    # ── real broker fills ────────────────────────────────────────────────────
    def update_fill(self, fill_event):
        """Reanchor to the REALLY executed price. Copied in spirit from
        IBKRFuturesPortfolio.update_fill — identical logic, since this math
        is price-space only (no multiplier anywhere). This is also the
        authoritative exit signal for stop/target hits (see on_bar()'s
        docstring): a fill for the full remaining quantity in the closing
        direction should be treated as a stop/take exit by the caller
        wiring this into the event loop, closing the position here via
        close_position() rather than waiting on a bar to notice.
        """
        pair = fill_event.instrument
        if pair not in self.positions or pair not in self.meta:
            self.logger.warning("FILL de %s sin posición local: revisar cuenta.", pair)
            return
        m, ps = self.meta[pair], self.positions[pair]
        vwap = float(fill_event.fill_price)
        sd = float(m["side"])

        # A fill on the opposite side of the open position's direction, for
        # the full remaining quantity, means the broker-side bracket
        # (STOP_MARKET/TAKE_PROFIT_MARKET) closed it — this is the
        # authoritative exit path referenced in on_bar()'s docstring.
        closing_side = "sell" if m["side"] > 0 else "buy"
        if fill_event.side == closing_side and \
                float(fill_event.units) >= float(m["qty"]) - 1e-9:
            reason = "stop" if m.get("stop") is not None and \
                abs(vwap - float(m["stop"])) <= abs(vwap - (m.get("take") or vwap)) \
                else "target"
            self._exit(pair, vwap, reason)
            return

        if fill_event.is_add:
            m["units"] += 1
            m["qty"] += float(fill_event.units)
            m["last_fill"] = vwap
            ps.add_units(Decimal(str(fill_event.units)))
            ps.reanchor_entry(Decimal(str(vwap)))
            m["stop"] = vwap - sd * self.stop_n * m["n"]
            if self.execution is not None:
                self.execution.modify_stop(pair, m["stop"], m["qty"])
        else:
            m["last_fill"] = vwap
            m["qty"] = float(fill_event.units)
            ps.units = Decimal(str(fill_event.units))
            ps.reanchor_entry(Decimal(str(vwap)))
            m["stop"] = vwap - sd * self.stop_n * m["n"]
        self._emit_state("fill")

    # ── funding (no IBKR analog — perpetuals only) ──────────────────────────
    def apply_funding(self, symbol, funding_rate, mark_price):
        """Debit/credit self.balance for a funding settlement and log it as
        a distinct journal event (see qsCrypto/monitor.py), so live P&L can
        separately attribute funding vs. price P&L, and the re-validated
        backtest's cost model can be checked against real accounting."""
        pos = self.positions.get(symbol)
        if pos is None:
            return
        side = 1 if pos.position_type == "long" else -1
        notional = float(pos.units) * float(mark_price)
        # Longs pay shorts when funding_rate > 0 (and vice versa) — the same
        # sign convention Binance uses for its own funding settlement.
        payment = Decimal(str(-side * notional * float(funding_rate)))
        self.balance += payment
        self.funding_paid += payment
        self.logger.info(
            "FUNDING %s rate=%.6f mark=%.6f notional=%.2f pago=%.6f",
            symbol, funding_rate, mark_price, notional, payment)
        self._emit_state("funding")

    # ── broker reconciliation ────────────────────────────────────────────────
    def _find_resting_bracket(self, execution_handler, symbol):
        """STOP_MARKET/TAKE_PROFIT_MARKET reduceOnly orders resting on the
        exchange for `symbol`. Returns (stop_price, take_price), either
        None if not found — analogous to IBKRFuturesPortfolio's
        _find_resting_stop, but Binance has no multi-clientId concept to
        worry about (get_open_orders() already returns the account's
        full open-order set)."""
        stop_px, take_px = None, None
        try:
            orders = execution_handler.get_open_orders(symbol)
        except Exception as exc:
            self.logger.warning("get_open_orders(%s) falló: %s", symbol, exc)
            return None, None
        for o in orders:
            if not o.get("reduceOnly"):
                continue
            if o.get("type") == "STOP_MARKET":
                stop_px = float(o.get("stopPrice"))
            elif o.get("type") == "TAKE_PROFIT_MARKET":
                take_px = float(o.get("stopPrice"))
        return stop_px, take_px

    def reconcile_from_broker(self, execution_handler):
        """Rebuild self.positions/self.meta from Binance's live account
        state. Simpler than IBKRFuturesPortfolio's version: positionRisk's
        entryPrice is already exchange-VWAP-averaged, so there is no
        _vwap_from_executions walk over the day's fills to do here."""
        try:
            broker_positions = execution_handler.get_position_info()
        except Exception as exc:
            self.logger.error("get_position_info() falló: %s — no se pudo "
                               "reconciliar.", exc)
            return

        broker_by_symbol = {
            p["symbol"]: p for p in broker_positions
            if abs(float(p.get("positionAmt", 0))) > 0
        }

        # Purge local positions with no broker counterpart.
        for pair in list(self.positions.keys()):
            if pair not in broker_by_symbol:
                self.logger.critical(
                    "%s: posición local sin contrapartida en el broker "
                    "(huérfana de un cierre previo). Se purga localmente "
                    "sin PnL exacto.", pair)
                self.positions.pop(pair, None)
                self.meta.pop(pair, None)
                self._sync_strategy()

        for pair, bp in broker_by_symbol.items():
            if pair in self.positions:
                continue
            qty = float(bp["positionAmt"])
            side = 1 if qty > 0 else -1
            entry_px = float(bp["entryPrice"])

            stop_px, take_px = self._find_resting_bracket(execution_handler, pair)

            n_est, level, mid = None, None, None
            hist = getattr(self.strategy, "history", {}).get(pair) \
                if self.strategy is not None else None
            if hist is not None and len(hist):
                if hasattr(self.strategy, "_n"):
                    try:
                        n_est = float(self.strategy._n(hist))
                    except Exception:
                        n_est = None
                if hasattr(self.strategy, "_channel"):
                    try:
                        ent_hi, ent_lo, mid_val = self.strategy._channel(hist)
                        if mid_val is not None:
                            level, mid = ent_hi, mid_val
                    except Exception:
                        pass

            snap_bid = self.ticker.prices.get(pair, {}).get("bid")
            snap_ask = self.ticker.prices.get(pair, {}).get("ask")
            self.ticker.prices.setdefault(pair, {})["bid"] = snap_bid or entry_px
            self.ticker.prices[pair]["ask"] = snap_ask or entry_px

            self.add_new_position(
                "long" if side > 0 else "short", pair, Decimal(str(abs(qty))),
                self.ticker, initial_stop=stop_px,
            )
            self.positions[pair].reanchor_entry(entry_px)
            self.meta[pair] = {
                "side": side, "n": n_est, "level": level, "mid": mid,
                "units": 1, "last_fill": entry_px, "stop": stop_px,
                "take": take_px, "bars": 0, "qty": abs(qty),
                "reconciled": True,
            }
            self._sync_strategy()
            tag = "%s %s qty=%s @ %.6f" % (
                pair, "long" if side > 0 else "short", abs(qty), entry_px)
            if stop_px is None:
                self.logger.critical(
                    "RECONCILIADA %s SIN STOP detectado en el broker: sin "
                    "barrera automática hasta colocar uno manualmente.", tag)
            else:
                self.logger.warning(
                    "RECONCILIADA %s stop=%.6f take=%s mid≈%s (aproximado).",
                    tag, stop_px, take_px, mid)
        self._emit_state("reconcile")

    # ── state for the monitor/journal ────────────────────────────────────────
    def _sync_strategy(self):
        if self.strategy is not None and hasattr(self.strategy, "set_open_symbols"):
            self.strategy.set_open_symbols(self.positions.keys())

    def snapshot(self):
        rows = []
        for pair, ps in self.positions.items():
            m = self.meta.get(pair, {})
            mae_r, mfe_r = ps.calculate_mae_r(), ps.calculate_mfe_r()
            r_unit = ps.r_unit
            dist_r = None
            if r_unit and m.get("stop") is not None:
                dist_r = float(abs(ps.cur_price - Decimal(str(m["stop"])))) / float(r_unit)
            rows.append({
                "pair": pair, "side": ps.position_type, "qty": float(ps.units),
                "units": m.get("units"), "entry": float(ps.entry_anchor),
                "price": float(ps.cur_price), "stop": m.get("stop"),
                "take": m.get("take"), "n": m.get("n"),
                "r_unit": None if r_unit is None else float(r_unit),
                "mae_r": None if mae_r is None else float(mae_r),
                "mfe_r": None if mfe_r is None else float(mfe_r),
                "pnl_r": None if ps.r_unit is None else float(ps.calculate_pnl_r()),
                "mae_price": float(ps.mae_price), "mfe_price": float(ps.mfe_price),
                "dist_stop_r": dist_r, "bars": m.get("bars"),
                "pnl_base": float(ps.profit_base),
            })
        return rows

    def _emit_state(self, reason=""):
        self.events.put(PortfolioEvent(
            time=None, equity=float(self.equity), balance=float(self.balance),
            snapshot=self.snapshot(), reason=reason,
        ))
