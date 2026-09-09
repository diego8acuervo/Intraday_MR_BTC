"""
engine.py
=========

Bar-by-bar backtest engine for the Donchian mean-reversion fade strategy on
Binance USD-M perpetuals. Deliberately reuses `compute_n`/`is_flat_bar` from
qsCrypto.strategy.strategy (the exact functions the live
MeanReversionFadeStrategy calls) rather than re-deriving the indicator math,
so the backtest can never silently diverge from what runs live — the classic
failure mode the backtester skill calls out.

The 3-bar state machine (breakout -> confirm -> fade-entry) and the
sizing/barrier logic (stop_n*N stop, mid-channel or take_n target, Turtle
1/2N pyramiding to max_units, drawdown-reduction sizing_equity) mirror
MeanReversionFadeStrategy.calculate_signals and BinancePerpetualsPortfolio
respectively — see tests/test_engine_matches_live.py-style parity check in
qsCrypto/backtest/validate.py, which runs both paths on the same data and
asserts identical signal timestamps/sides.

Cost model is genuinely different from the IBKR backtest's per-contract USD
commission + slippage-in-ticks: Binance perpetuals charge bps-of-notional
taker fees on both entry and exit, plus a recurring funding settlement every
8h that the CME futures backtest has no analog for at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from qsCrypto.strategy.strategy import compute_n, is_flat_bar


@dataclass
class BacktestParams:
    entry_bars: int = 20
    atr_period: int = 20
    stop_n: float = 1.0
    add_n: float = 0.5
    max_units: int = 4
    max_hold_bars: int = 10
    tp_mode: str = "mid_channel"     # "mid_channel" | "take_n"
    take_n: float = 5.0
    risk_per_trade: float = 0.01
    equity0: float = 10_000.0
    fee_bps: float = 0.0004          # Binance USDM taker, one side
    slip_bps: float = 0.0010         # unfavorable price impact, one side
    max_gross_lev: float = 4.0
    use_dd_rule: bool = True
    allow_short: bool = True
    require_confirmation: bool = True


def _sizing_equity(balance, equity0, use_dd_rule):
    if not use_dd_rule:
        return max(balance, equity0 * 0.05)
    if balance < equity0:
        steps = int((equity0 - balance) / (0.10 * equity0))
        adj = balance * (0.80 ** steps)
    else:
        adj = balance
    return max(adj, equity0 * 0.05)


def run_backtest(df: pd.DataFrame, params: BacktestParams,
                  funding: pd.Series | None = None) -> tuple[pd.Series, pd.DataFrame]:
    """Replay `df` (UTC-naive index, open/high/low/close/volume columns, one
    symbol) bar by bar. Returns (equity_curve, trades) — trades holds only
    CLOSED trades; any position still open at the end of the sample is
    reported separately via the second return value's `.attrs["open_book"]`
    (the "libro abierto" the skill requires: without it you don't know what
    the system has on right now).

    Pessimistic intrabar precedence: if a bar's range could plausibly touch
    both stop and target, the stop wins — there is no intrabar data here to
    know which came first, and assuming the favorable order is exactly the
    bias that inflates a backtest.
    """
    p = params
    highs, lows, closes = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    n_arr = compute_n(highs, lows, closes, period=p.atr_period)
    idx = df.index

    balance = p.equity0
    equity_curve = np.empty(len(df))
    trades = []
    open_trade = None       # dict while a position is open
    pending = None          # breakout/confirm state, mirrors strategy.pending
    fire_pending = None     # frozen {side, n, mid} armed to fill on THIS bar's open
    funding = funding if funding is not None else pd.Series(dtype=float)

    def channel(i):
        lo_idx = i - p.entry_bars
        if lo_idx < 0:
            return None, None, None
        window_hi = highs[lo_idx:i]
        window_lo = lows[lo_idx:i]
        ent_hi, ent_lo = float(window_hi.max()), float(window_lo.min())
        return ent_hi, ent_lo, (ent_hi + ent_lo) / 2.0

    def apply_funding_between(t_prev, t_cur, side, qty, mark_price):
        nonlocal balance
        if funding.empty:
            return 0.0
        window = funding[(funding.index > t_prev) & (funding.index <= t_cur)]
        total = 0.0
        for rate in window.values:
            payment = -side * qty * mark_price * float(rate)
            balance += payment
            total += payment
        return total

    for i in range(len(df)):
        t = idx[i]
        o, h, l, c = float(df["open"].iloc[i]), highs[i], lows[i], closes[i]
        v = float(df["volume"].iloc[i]) if "volume" in df.columns else 0.0
        flat_bar = is_flat_bar(h, l, v)
        n_val = n_arr[i] if i < len(n_arr) else np.nan
        min_bars = max(p.atr_period, p.entry_bars) + 1

        # ── execute a fill armed on the PREVIOUS iteration, at THIS bar's
        # open. Live only fires the entry once the "abrir" state is seen on
        # the bar AFTER confirmation closes (calculate_signals returns
        # without falling through), so the market order goes out right as
        # that later bar closes — numerically indistinguishable from the
        # NEXT bar's open on a contiguous kline series. Filling on the same
        # bar that flips to "abrir" would be a one-bar look-ahead: the
        # decision is only actionable from the following bar's open.
        just_opened = False
        if fire_pending is not None and open_trade is None:
            side = fire_pending["side"]
            n_frozen, mid_frozen = fire_pending["n"], fire_pending["mid"]
            fill_px = o * (1 + side * p.slip_bps)
            qty = _position_size(balance, p, n_frozen, fill_px, p.use_dd_rule)
            fire_pending = None
            if qty > 0:
                fee = abs(qty * fill_px) * p.fee_bps
                balance -= fee
                stop_px = fill_px - side * p.stop_n * n_frozen
                take_px = (mid_frozen if p.tp_mode == "mid_channel"
                           else fill_px + side * p.take_n * n_frozen)
                open_trade = {
                    "side": side, "qty": qty, "entry": fill_px,
                    "opened": t, "n": n_frozen, "stop": stop_px,
                    "take": take_px, "mid": mid_frozen,
                    "units": 1, "last_fill": fill_px, "bars": 0,
                    "entry_fee": fee, "funding_paid": 0.0,
                }
                just_opened = True

        # ── manage an open position first (barriers evaluated on THIS bar).
        # Skipped for a position that just filled THIS bar: live's on_bar()
        # for the fill bar already ran (paired with the BAR event that
        # triggered calculate_signals) BEFORE the fill actually landed —
        # execute_signal/order/fill all resolve off the derived SIGNAL/ORDER
        # events, strictly after that bar's on_bar() already returned. The
        # first barrier check a real position gets is on the NEXT bar. ──
        if open_trade is not None and not just_opened:
            if i > 0:
                open_trade["funding_paid"] += apply_funding_between(
                    idx[i - 1], t, open_trade["side"], open_trade["qty"], c)
            open_trade["bars"] += 1
            side = open_trade["side"]
            stop_px, take_px = open_trade["stop"], open_trade["take"]
            hit_stop = (not flat_bar) and (
                (l <= stop_px) if side > 0 else (h >= stop_px))
            hit_take = (not flat_bar) and take_px is not None and (
                (h >= take_px) if side > 0 else (l <= take_px))

            exit_reason, exit_px = None, None
            if hit_stop:
                exit_reason, exit_px = "stop", stop_px
            elif hit_take:
                exit_reason, exit_px = "target", take_px
            elif open_trade["bars"] >= p.max_hold_bars:
                exit_reason, exit_px = "vertical", c

            if exit_reason is not None:
                fill_px = exit_px * (1 - side * p.slip_bps)
                fee = abs(open_trade["qty"] * fill_px) * p.fee_bps
                pnl = side * open_trade["qty"] * (fill_px - open_trade["entry"]) - fee \
                    - open_trade["entry_fee"] + open_trade["funding_paid"]
                balance += side * open_trade["qty"] * (fill_px - open_trade["entry"]) - fee
                r_unit = p.stop_n * open_trade["n"]
                trades.append({
                    "opened": open_trade["opened"], "closed": t,
                    "side": "long" if side > 0 else "short",
                    "qty": open_trade["qty"], "entry": open_trade["entry"],
                    "exit": fill_px, "reason": exit_reason,
                    "pnl": pnl, "r_multiple": pnl / (r_unit * open_trade["qty"])
                    if r_unit > 0 else np.nan,
                    "bars_held": open_trade["bars"],
                    "funding_paid": open_trade["funding_paid"],
                })
                open_trade = None
            elif not flat_bar and open_trade["units"] < p.max_units:
                # Turtle 1/2N ladder — add-until-mid, same as
                # BinancePerpetualsPortfolio._try_add.
                mid = open_trade.get("mid")
                reached_mid = mid is not None and (
                    (h >= mid) if side > 0 else (l <= mid))
                if not reached_mid:
                    trig = open_trade["last_fill"] + side * p.add_n * open_trade["n"]
                    hit_add = (h >= trig) if side > 0 else (l <= trig)
                    if hit_add:
                        add_qty = _position_size(
                            balance, p, open_trade["n"], c, use_dd_rule=p.use_dd_rule)
                        if add_qty > 0:
                            fill_ref = max(trig, o) if side > 0 else min(trig, o)
                            fee = abs(add_qty * fill_ref) * p.fee_bps
                            balance -= fee
                            total_qty = open_trade["qty"] + add_qty
                            open_trade["entry"] = (
                                open_trade["entry"] * open_trade["qty"]
                                + fill_ref * add_qty) / total_qty
                            open_trade["qty"] = total_qty
                            open_trade["units"] += 1
                            open_trade["last_fill"] = fill_ref
                            open_trade["stop"] = fill_ref - side * p.stop_n * open_trade["n"]
                            open_trade["entry_fee"] += fee

        # ── signal state machine (only when flat) ───────────────────────────
        if open_trade is None and i >= min_bars and not (n_val != n_val) and n_val > 0:
            if pending is not None and pending["etapa"] == "abrir":
                # This bar is the one where live's calculate_signals would
                # see etapa=="abrir" and emit the SignalEvent — but it fires
                # on RECEIPT of this bar's close, so the resulting market
                # order lands at this bar's close, numerically the same
                # instant as the NEXT bar's open on a contiguous kline
                # series. Arm the fill for next iteration's open instead of
                # using this bar's own open (see the fire_pending block
                # above this loop's start).
                fire_pending = {"side": -pending["side"], "n": pending["n"],
                                 "mid": pending["mid"]}
                pending = None
            elif pending is not None and pending["etapa"] == "confirmar":
                if flat_bar:
                    pending = None
                else:
                    ok = (c < pending["level"]) if pending["side"] > 0 else (c > pending["level"])
                    pending = dict(pending, etapa="abrir") if ok else None
            else:
                ent_hi, ent_lo, mid = channel(i)
                if ent_hi is not None:
                    if h > ent_hi:
                        brk, level = 1, ent_hi
                    elif p.allow_short and l < ent_lo:
                        brk, level = -1, ent_lo
                    else:
                        brk = 0
                    if brk != 0:
                        etapa = "confirmar" if p.require_confirmation else "abrir"
                        pending = {"side": brk, "n": float(n_val), "level": float(level),
                                   "mid": float(mid), "etapa": etapa}

        equity_curve[i] = balance + (
            open_trade["side"] * open_trade["qty"] * (c - open_trade["entry"])
            if open_trade is not None else 0.0)

    ec = pd.Series(equity_curve, index=idx, name="equity")
    trades_df = pd.DataFrame(trades)
    open_book = None
    if open_trade is not None:
        open_book = dict(open_trade)
        open_book["mark_price"] = float(closes[-1])
        open_book["unrealized_pnl"] = (
            open_trade["side"] * open_trade["qty"]
            * (closes[-1] - open_trade["entry"]))
    trades_df.attrs["open_book"] = open_book
    return ec, trades_df


def _position_size(balance, p: BacktestParams, n, price, use_dd_rule):
    if n is None or not (n == n) or n <= 0:
        return 0.0
    sizing_eq = _sizing_equity(balance, p.equity0, use_dd_rule)
    denom = p.stop_n * n
    if denom <= 0:
        return 0.0
    qty = p.risk_per_trade * sizing_eq / denom
    # Leverage cap: notional cannot exceed max_gross_lev * balance.
    if price > 0 and qty * price > p.max_gross_lev * balance:
        qty = (p.max_gross_lev * balance) / price
    return max(qty, 0.0)
