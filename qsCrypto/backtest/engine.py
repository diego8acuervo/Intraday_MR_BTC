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

import copy

from qsCrypto.strategy.strategy import (
    compute_n, compute_vwap, is_flat_bar, opening_range_signal,
    rolling_range_n,
)


@dataclass
class BacktestParams:
    entry_bars: int = 20
    atr_period: int = 20
    stop_n: float = 2.0
    add_n: float = 0.5
    max_units: int = 6
    max_hold_bars: int = 20 # more time to cross the channel
    tp_mode: str = "mid_channel"     # "mid_channel" | "take_n"
    take_n: float = 4.0
    risk_per_trade: float = 0.01
    equity0: float = 10_000.0
    fee_bps: float = 0.0002          # 2 pb taker por lado
    # 1 pb por lado. El valor anterior (10 pb) era un supuesto genérico para
    # todo el universo piloto -- incluidos nombres finos como AVAX o LINK -- y
    # aplicado a BTCUSDT, el par más líquido del exchange, costaba 0.42 R por
    # round-trip frente a los 0.03 R de MBT en CME: por sí solo hundía el
    # backtest. Sigue siendo un supuesto: medirlo contra el libro antes de
    # darlo por bueno, y revisarlo al alza para los pares menos líquidos.
    slip_bps: float = 0.0001
    max_gross_lev: float = 10.0
    use_dd_rule: bool = True
    allow_short: bool = True
    require_confirmation: bool = True
    # Barrera inferior efectiva, en N, si difiere de la que define la R.
    # `stop_n` fija la UNIDAD de riesgo (1R = stop_n·N: sizing y r_multiple);
    # `mae_stop_n` fija DÓNDE se cierra. `None` = cerrar en `stop_n` (el
    # comportamiento de siempre); `float("inf")` = sin stop, para observar la
    # excursión adversa sin que la trunque la propia barrera. Separarlos es lo
    # que permite calibrar un cierre temprano por MAE dentro de un stop fijo.
    mae_stop_n: float | None = None
    # Referencia de la regla de drawdown. False (histórico): los tramos del
    # 10 % se cuentan desde `equity0`, así que en cuanto la cuenta supera su
    # capital inicial la regla no vuelve a reducir nunca, aunque caiga un 50 %
    # desde el pico. True: se cuentan desde el MÁXIMO del saldo alcanzado, que
    # es la regla Turtle tal como protege una cuenta que ha crecido.
    dd_from_peak: bool = False
    # Cerrar toda posición al cierre de la última barra de cada sesión
    # (motivo "cierre_sesion") y no abrir en esa barra. Para series filtradas
    # a una sesión: la señal nace y muere dentro de ella y la posición nunca
    # cruza la noche. La última barra de sesión se reconoce por el hueco hasta
    # la siguiente (> el paso habitual de la serie), que es el horario, no el
    # precio: se conoce de antemano y no es look-ahead.
    flat_at_session_end: bool = False


def _barrier_n(p):
    """Distancia en N a la que se coloca el stop (ver `mae_stop_n`)."""
    return p.stop_n if p.mae_stop_n is None else p.mae_stop_n


def _sizing_equity(balance, equity0, use_dd_rule, ref=None):
    """Equity sobre la que se dimensiona: por cada 10 % por debajo de `ref`,
    el nocional se multiplica por 0,80. `ref` es `equity0` salvo que se pase
    el máximo del saldo (`dd_from_peak`). El suelo sigue siendo el 5 % del
    capital inicial."""
    if not use_dd_rule:
        return max(balance, equity0 * 0.05)
    ref = equity0 if ref is None else ref
    if balance < ref:
        steps = int((ref - balance) / (0.10 * ref))
        adj = balance * (0.80 ** steps)
    else:
        adj = balance
    return max(adj, equity0 * 0.05)


class _Account:
    """Saldo de la cuenta y regla de tamaño de `run_backtest` (un símbolo).

    `size()` devuelve la cantidad a abrir, o 0 si la orden no se hace. Aquí
    es el sizing histórico de este motor: riesgo sobre la equity ajustada y
    el tope de apalancamiento RECORTANDO la unidad, unidad a unidad. Se
    conserva tal cual para que todos los resultados ya publicados con
    `run_backtest` sigan siendo reproducibles; `_PortfolioAccount` es el que
    replica los topes de la cartera en vivo.
    """

    def __init__(self, params: BacktestParams):
        self.p = params
        self.balance = params.equity0
        self.peak = params.equity0     # máximo del saldo, para `dd_from_peak`
        # Por qué NO se abrió lo que no se abrió. Sin este contador, una
        # política que rechaza el 95 % de las rupturas y una serie que no las
        # tiene producen el mismo backtest vacío.
        self.rejected = {}

    def reject(self, why):
        if why:
            self.rejected[why] = self.rejected.get(why, 0) + 1

    def dd_ref(self):
        """Referencia de la regla de drawdown en el instante de dimensionar.
        None = la histórica (`equity0`). El máximo se mide sobre el SALDO
        realizado, igual que la regla lo compara con el saldo."""
        if not self.p.dd_from_peak:
            return None
        self.peak = max(self.peak, self.balance)
        return self.peak

    def size(self, book, n, price, kind):
        return _position_size(self.balance, book.p, n, price, book.p.use_dd_rule,
                              ref=self.dd_ref())


class _PortfolioAccount(_Account):
    """Una sola cuenta para varios símbolos, con los topes de
    `BinancePerpetualsPortfolio` en vivo:

    * apalancamiento BRUTO de toda la cuenta (Σ|qty|·precio de todas las
      posiciones + la orden nueva) ≤ `max_gross_lev` × saldo; si no cabe,
      la orden se RECHAZA entera, no se recorta (`_fits_leverage`);
    * unidades por dirección ≤ `max_dir_units` y por grupo de correlación y
      dirección ≤ `max_corr_units` — solo en entradas, como `execute_signal`;
      los adds solo pasan el filtro de apalancamiento, como `_try_add`.
    """

    def __init__(self, params, max_dir_units=12, max_corr_units=6,
                 corr_groups=None):
        super().__init__(params)
        self.max_dir_units = max_dir_units
        self.max_corr_units = max_corr_units
        self.corr_groups = corr_groups or {}
        self.books = []
        self.rejected.update({"apalancamiento": 0, "topes": 0})

    def gross(self):
        return sum(abs(b.open_trade["qty"]) * b.mark for b in self.books
                   if b.open_trade is not None)

    def size(self, book, n, price, kind):
        if n is None or not (n == n) or n <= 0:
            return 0.0
        denom = book.p.stop_n * n
        if denom <= 0:
            return 0.0
        qty = self.p.risk_per_trade * _sizing_equity(
            self.balance, self.p.equity0, self.p.use_dd_rule,
            ref=self.dd_ref()) / denom
        if qty <= 0:
            return 0.0
        if kind == "entry":
            side = book.fire_pending["side"]
            open_books = [b for b in self.books if b.open_trade is not None
                          and b.open_trade["side"] == side]
            dir_units = sum(b.open_trade["units"] for b in open_books)
            group = self.corr_groups.get(book.symbol)
            corr_units = sum(b.open_trade["units"] for b in open_books
                             if group is not None
                             and self.corr_groups.get(b.symbol) == group)
            if dir_units + 1 > self.max_dir_units or \
                    corr_units + 1 > self.max_corr_units:
                self.rejected["topes"] += 1
                return 0.0
        if self.gross() + qty * price > self.p.max_gross_lev * self.balance:
            self.rejected["apalancamiento"] += 1
            return 0.0
        return qty


class _SymbolBook:
    """Estado y reglas de UN símbolo: la máquina de 3 barras, las barreras
    y la escalera de adds. El saldo vive en `account`, compartido si hay
    varios libros. Cada barra se procesa en tres fases (`fill_at_open`,
    `manage`, `signals`) para que una cartera pueda ejecutar las entradas
    al open de TODOS sus símbolos antes de las salidas intrabarra de
    cualquiera de ellos — el orden en que ocurren en el mercado.
    """

    def __init__(self, symbol, df, params, funding, account, policy=None,
                 path=None, rule=None, ctx=None):
        self.symbol = symbol
        self.p = params
        self.acct = account
        # Política de ruptura opcional (p. ej. VwapFadePolicy): cambia el ancla
        # y puede vetar la ruptura. `None` = sistema clásico, camino intacto.
        self.policy = policy
        self.idx = df.index
        self.opens = df["open"].to_numpy(dtype=float)
        self.highs = df["high"].to_numpy()
        self.lows = df["low"].to_numpy()
        self.closes = df["close"].to_numpy()
        self.volumes = (df["volume"].to_numpy(dtype=float)
                        if "volume" in df.columns else np.zeros(len(df)))
        self.n_arr = compute_n(self.highs, self.lows, self.closes,
                               period=params.atr_period)
        # La regla de entrada. `None` = el sistema clásico, para que todas
        # las llamadas y los resultados ya publicados sigan siendo idénticos.
        # Se COPIA por libro: una regla con estado (la `pending` de la máquina
        # de 3 barras) compartida entre los símbolos de una cartera mezclaría
        # la ruptura de uno con la confirmación de otro.
        self.rule = copy.deepcopy(rule) if rule is not None \
            else DonchianFadeRule(policy)
        # `detect_breakout` sigue consultando la política a través del libro;
        # una regla que no la use deja esto en None y ese camino queda inerte.
        self.policy = getattr(self.rule, "policy", None)
        policy = self.policy
        self.anchor_arr = policy.prepare(df) if policy is not None else None
        # Primera y última barra de cada sesión, por el HUECO de tiempo hasta
        # la barra vecina. Es el horario, no el precio: se conoce de antemano
        # y no es look-ahead. `sess_first` se calcula siempre (una regla de
        # apertura lo necesita aunque no se cierre por sesión); `sess_last`
        # solo si se pide cerrar, para no cambiar el camino existente.
        self.sess_first = None
        self.sess_last = None
        if len(df) > 1:
            step = df.index.to_series().diff().dropna()
            med = step.median().to_timedelta64()
            gap_prev = np.r_[np.timedelta64(0, "ns"), np.diff(df.index.values)]
            self.sess_first = gap_prev > med
            # La primera barra de la muestra abre una sesión que no vimos
            # entera, pero sí es la primera que tenemos: cuenta como apertura.
            self.sess_first[0] = True
            if params.flat_at_session_end:
                gap_next = np.r_[np.diff(df.index.values),
                                 np.timedelta64(0, "ns")]
                self.sess_last = gap_next > med
                # La última barra de la muestra no es un fin de sesión conocido.
                self.sess_last[-1] = False
        self.funding = funding if funding is not None else pd.Series(dtype=float)
        self.ctx = ctx
        self.min_bars = max(params.atr_period, params.entry_bars) + 1
        # El ancla también necesita calentamiento: sin esto la política
        # vetaría por "sin_vwap" las primeras barras y ese veto se contaría
        # como un rechazo de la regla, no como falta de muestra.
        self.min_bars = max(self.min_bars, self.rule.warmup_bars())
        self.rule.prepare(self, df, ctx)
        self.trades = []
        # Cada ejecución (entrada, add, salida) con su nocional: de aquí sale
        # el volumen negociado, que `trades` no puede reconstruir — solo
        # guarda la cantidad final y el precio medio.
        self.fills = []
        self.open_trade = None    # dict mientras hay posición
        self.pending = None       # ruptura esperando confirmación (strategy.pending)
        self.fire_pending = None  # {side, n, mid} congelado para el open de ESTA barra
        self.just_opened = False
        self.mark = np.nan        # último cierre visto, para marcar la posición
        # Camino completo opcional (p. ej. 24/7) para las órdenes en reposo.
        # Las señales, N, el canal y el VWAP salen SOLO de `df`; `path` solo
        # sirve para ver si un stop o un objetivo que están puestos en el
        # exchange se habrían tocado ENTRE dos barras de `df` (la noche, el fin
        # de semana). Sin esto, filtrar la serie a una sesión deja al motor
        # ciego a las excursiones que ocurren fuera de ella.
        self.path = path
        if path is not None:
            pidx = path.index.values
            sidx = df.index.values
            self.p_idx = path.index
            self.p_open = path["open"].to_numpy(dtype=float)
            self.p_high = path["high"].to_numpy(dtype=float)
            self.p_low = path["low"].to_numpy(dtype=float)
            self.p_vol = (path["volume"].to_numpy(dtype=float)
                          if "volume" in path.columns else np.zeros(len(path)))
            # Para la barra i: barras de `path` estrictamente entre idx[i-1] e
            # idx[i]. Las que coinciden con una barra de `df` son la misma vela
            # y ya se evalúan como tal.
            prev = np.r_[sidx[:1], sidx[:-1]]
            self.gap_lo = np.searchsorted(pidx, prev, side="right")
            self.gap_hi = np.searchsorted(pidx, sidx, side="left")
            if len(sidx):
                self.gap_lo[0] = self.gap_hi[0]

    def _fill(self, t, kind, side, qty, price, fee, reason=None):
        self.fills.append({"time": t, "symbol": self.symbol, "kind": kind,
                           "side": side, "qty": qty, "price": price,
                           "notional": abs(qty * price), "fee": fee,
                           "reason": reason})

    def channel(self, i):
        lo_idx = i - self.p.entry_bars
        if lo_idx < 0:
            return None, None, None
        ent_hi = float(self.highs[lo_idx:i].max())
        ent_lo = float(self.lows[lo_idx:i].min())
        return ent_hi, ent_lo, (ent_hi + ent_lo) / 2.0

    def detect_breakout(self, i, h, l, n_val):
        """{side, n, level, mid} de una ruptura del canal en la barra `i`, o
        None. Se usa tanto para una detección nueva como para reevaluar, en
        la MISMA barra, una ruptura que acaba de fallar su confirmación.

        Con una `policy`, `mid` deja de ser el centro del canal y pasa a ser
        el ancla que ella calcula, y la ruptura puede quedar vetada. Sin
        política el camino es exactamente el de siempre.
        """
        ent_hi, ent_lo, mid = self.channel(i)
        if ent_hi is None:
            return None
        if h > ent_hi:
            brk = {"side": 1, "n": float(n_val), "level": float(ent_hi), "mid": float(mid)}
        elif self.p.allow_short and l < ent_lo:
            brk = {"side": -1, "n": float(n_val), "level": float(ent_lo), "mid": float(mid)}
        else:
            return None
        if self.policy is None:
            return brk
        anchor = self.anchor_arr[i] if i < len(self.anchor_arr) else np.nan
        extreme = h if brk["side"] > 0 else l
        ok, why = self.policy.accept(brk, float(extreme), float(anchor))
        if not ok:
            self.acct.reject(why)
            return None
        brk["mid"] = float(anchor)
        return brk

    def apply_funding_between(self, t_prev, t_cur, side, qty, mark_price):
        if self.funding.empty:
            return 0.0
        f = self.funding
        window = f[(f.index > t_prev) & (f.index <= t_cur)]
        total = 0.0
        for rate in window.values:
            payment = -side * qty * mark_price * float(rate)
            self.acct.balance += payment
            total += payment
        return total

    def fill_at_open(self, i):
        """Ejecuta al open de ESTA barra el fill armado en la anterior.

        `fire_pending` se arma en cuanto se resuelve la confirmación (en
        `signals`, en la misma barra que evalúa el cierre de confirmación):
        ese cierre es numéricamente el mismo instante que el open de esta
        barra en una serie de klines contigua, así que no se pierde latencia.
        Llenar sobre la propia barra de confirmación sería look-ahead de una
        barra: su open ya pasó cuando se conoce su cierre.
        """
        p, t, o = self.p, self.idx[i], self.opens[i]
        self.just_opened = False
        if self.fire_pending is None or self.open_trade is not None:
            return
        if self.sess_last is not None and self.sess_last[i]:
            # Abrir en la última barra de la sesión sería una posición de una
            # sola barra, cerrada a la fuerza al cierre: se descarta la señal.
            self.fire_pending = None
            self.acct.reject("fin_sesion")
            return
        side = self.fire_pending["side"]
        n_frozen, mid_frozen = self.fire_pending["n"], self.fire_pending["mid"]
        fill_px = o * (1 + side * p.slip_bps)
        qty = self.acct.size(self, n_frozen, fill_px, "entry")
        self.fire_pending = None
        if qty > 0:
            fee = abs(qty * fill_px) * p.fee_bps
            self.acct.balance -= fee
            self._fill(t, "entry", side, qty, fill_px, fee)
            stop_px = fill_px - side * _barrier_n(p) * n_frozen
            take_px = (mid_frozen if p.tp_mode == "mid_channel"
                       else fill_px + side * p.take_n * n_frozen)
            self.open_trade = {
                "side": side, "qty": qty, "entry": fill_px,
                "opened": t, "n": n_frozen, "stop": stop_px,
                "take": take_px, "mid": mid_frozen,
                "units": 1, "last_fill": fill_px, "bars": 0,
                "entry_fee": fee, "funding_paid": 0.0,
                # Deslizamiento pagado, medido contra el precio sin
                # deslizar. No es un cargo aparte -- ya está dentro de
                # fill_px -- pero sin contabilizarlo no se puede separar
                # comisión de deslizamiento al comparar contra CME.
                "slip_paid": abs(qty * (fill_px - o)),
                # Cantidad de la PRIMERA unidad, congelada. Es el
                # denominador correcto de la R: 1R = lo que se arriesgaba
                # al abrir. Usar la qty final (que crece con cada add)
                # divide la R de un ganador piramidado por 3-4 y hace que
                # un sistema rentable en dólares reporte esperanza en R
                # negativa.
                "qty0": qty, "entry0": fill_px,
                # Excursión del camino, en precio absoluto. Se normaliza al
                # cerrar contra `entry0` y el N congelado, para que un add no
                # reescriba una excursión ya medida (mismo convenio que
                # qsCrypto.portfolio.mae).
                "mae_px": fill_px, "mfe_px": fill_px,
                "bars_to_mae": 0, "bars_to_mfe": 0, "flat_bars": 0,
            }
            self.just_opened = True
            self.mark = fill_px

    def _exit(self, t, exit_reason, exit_px):
        """Cierra la posición abierta en `exit_px` (antes de deslizamiento) y
        la registra. Común a las salidas en barra de `df` y a las órdenes en
        reposo que se tocan en el camino entre barras."""
        p, tr = self.p, self.open_trade
        side = tr["side"]
        fill_px = exit_px * (1 - side * p.slip_bps)
        fee = abs(tr["qty"] * fill_px) * p.fee_bps
        pnl = side * tr["qty"] * (fill_px - tr["entry"]) - fee \
            - tr["entry_fee"] + tr["funding_paid"]
        self.acct.balance += side * tr["qty"] * (fill_px - tr["entry"]) - fee
        self._fill(t, "exit", -side, tr["qty"], fill_px, fee, exit_reason)
        r_unit = p.stop_n * tr["n"]
        n0 = tr["n"]
        mae_n = side * (tr["entry0"] - tr["mae_px"]) / n0 if n0 > 0 else np.nan
        mfe_n = side * (tr["mfe_px"] - tr["entry0"]) / n0 if n0 > 0 else np.nan
        self.trades.append({
            "opened": tr["opened"], "closed": t,
            "side": "long" if side > 0 else "short",
            "qty": tr["qty"], "entry": tr["entry"],
            "exit": fill_px, "reason": exit_reason,
            "pnl": pnl, "r_multiple": pnl / (r_unit * tr["qty0"])
            if r_unit > 0 else np.nan,
            "units": tr["units"],
            "bars_held": tr["bars"],
            "funding_paid": tr["funding_paid"],
            "fees": tr["entry_fee"] + fee,
            "slip_paid": tr["slip_paid"]
            + abs(tr["qty"] * (fill_px - exit_px)),
            "notional0": tr["qty0"] * tr["entry0"],
            "entry0": tr["entry0"], "n": n0,
            "stop_at_exit": tr["stop"],
            "mae_n": mae_n, "mfe_n": mfe_n,
            "mae_r": mae_n / p.stop_n if p.stop_n > 0 else np.nan,
            "mfe_r": mfe_n / p.stop_n if p.stop_n > 0 else np.nan,
            "bars_to_mae": tr["bars_to_mae"],
            "bars_to_mfe": tr["bars_to_mfe"],
            "flat_frac": tr["flat_bars"] / tr["bars"] if tr["bars"] else 0.0,
        })
        self.open_trade = None

    def _scan_gap(self, i):
        """Órdenes en reposo entre la barra i-1 y la i (solo con `path`).

        El stop y el objetivo son órdenes que en vivo están puestas en el
        exchange las 24 horas, así que se evalúan en cada vela del camino
        intermedio. Los adds y la barrera temporal NO: son decisiones que el
        sistema toma sobre sus propias barras. Prelación pesimista igual que
        en barra: si una vela toca stop y objetivo, gana el stop. Un stop se
        ejecuta en su nivel o, si la vela ya abrió más allá, en su apertura;
        el objetivo, en su nivel (sin acreditar la mejora de un hueco).
        Devuelve True si la posición se cerró.
        """
        if self.path is None or i == 0:
            return False
        tr = self.open_trade
        side = tr["side"]
        for k in range(self.gap_lo[i], self.gap_hi[i]):
            h, l, o = self.p_high[k], self.p_low[k], self.p_open[k]
            if is_flat_bar(h, l, self.p_vol[k]):
                continue
            stop_px, take_px = tr["stop"], tr["take"]
            hit_stop = (l <= stop_px) if side > 0 else (h >= stop_px)
            hit_take = take_px is not None and (
                (h >= take_px) if side > 0 else (l <= take_px))
            adverse = l if side > 0 else h
            favorable = h if side > 0 else l
            reason, px = None, None
            if hit_stop:
                px = min(stop_px, o) if side > 0 else max(stop_px, o)
                reason, adverse, favorable = "stop_fuera", px, None
            elif hit_take:
                reason, px, favorable = "target_fuera", take_px, take_px
            if side * (adverse - tr["mae_px"]) < 0:
                tr["mae_px"], tr["bars_to_mae"] = adverse, tr["bars"]
            if favorable is not None and side * (favorable - tr["mfe_px"]) > 0:
                tr["mfe_px"], tr["bars_to_mfe"] = favorable, tr["bars"]
            if reason is not None:
                t = self.p_idx[k]
                tr["funding_paid"] += self.apply_funding_between(
                    self.idx[i - 1], t, side, tr["qty"], px)
                self._exit(t, reason, px)
                return True
        return False

    def manage(self, i):
        """Barreras sobre ESTA barra, para una posición ya abierta.

        Se salta la posición que se llenó en esta misma barra: en vivo, el
        on_bar() de la barra del fill ya corrió (emparejado con el BAR que
        disparó calculate_signals) ANTES de que el fill llegara. La primera
        comprobación de barreras de una posición real es en la barra
        SIGUIENTE.
        """
        p = self.p
        t, o, h, l, c = (self.idx[i], self.opens[i], self.highs[i],
                         self.lows[i], self.closes[i])
        flat_bar = is_flat_bar(h, l, self.volumes[i])
        tr = self.open_trade
        if tr is None or self.just_opened:
            return
        if self._scan_gap(i):
            return
        if i > 0:
            tr["funding_paid"] += self.apply_funding_between(
                self.idx[i - 1], t, tr["side"], tr["qty"], c)
        tr["bars"] += 1
        side = tr["side"]
        stop_px, take_px = tr["stop"], tr["take"]
        hit_stop = (not flat_bar) and (
            (l <= stop_px) if side > 0 else (h >= stop_px))
        hit_take = (not flat_bar) and take_px is not None and (
            (h >= take_px) if side > 0 else (l <= take_px))

        exit_reason, exit_px = None, None
        if hit_stop:
            exit_reason, exit_px = "stop", stop_px
        elif hit_take:
            exit_reason, exit_px = "target", take_px
        elif tr["bars"] >= p.max_hold_bars:
            exit_reason, exit_px = "vertical", c
        elif self.sess_last is not None and self.sess_last[i]:
            exit_reason, exit_px = "cierre_sesion", c

        # Excursión sobre las MISMAS barras en que se evalúan las barreras.
        # En la barra de salida el camino se corta donde se cerró: un stop
        # fija la MAE en el nivel del stop (lo que haya más allá ya no es de
        # la posición) y, con la prelación pesimista, no se le acredita MFE a
        # esa barra; un objetivo fija la MFE en el objetivo.
        if flat_bar:
            tr["flat_bars"] += 1
        else:
            adverse = l if side > 0 else h
            favorable = h if side > 0 else l
            if exit_reason == "stop":
                adverse, favorable = stop_px, None
            elif exit_reason == "target":
                favorable = take_px
            if side * (adverse - tr["mae_px"]) < 0:
                tr["mae_px"], tr["bars_to_mae"] = adverse, tr["bars"]
            if favorable is not None and side * (favorable - tr["mfe_px"]) > 0:
                tr["mfe_px"], tr["bars_to_mfe"] = favorable, tr["bars"]

        if exit_reason is not None:
            self._exit(t, exit_reason, exit_px)
        elif not flat_bar and tr["units"] < p.max_units:
            # Turtle 1/2N ladder — add-until-mid, same as
            # BinancePerpetualsPortfolio._try_add.
            mid = tr.get("mid")
            reached_mid = mid is not None and (
                (h >= mid) if side > 0 else (l <= mid))
            if not reached_mid:
                trig = tr["last_fill"] + side * p.add_n * tr["n"]
                hit_add = (h >= trig) if side > 0 else (l <= trig)
                if hit_add:
                    add_qty = self.acct.size(self, tr["n"], c, "add")
                    if add_qty > 0:
                        fill_ref = max(trig, o) if side > 0 else min(trig, o)
                        fee = abs(add_qty * fill_ref) * p.fee_bps
                        self.acct.balance -= fee
                        self._fill(t, "add", side, add_qty, fill_ref, fee)
                        total_qty = tr["qty"] + add_qty
                        tr["entry"] = (
                            tr["entry"] * tr["qty"]
                            + fill_ref * add_qty) / total_qty
                        tr["qty"] = total_qty
                        tr["units"] += 1
                        tr["last_fill"] = fill_ref
                        tr["stop"] = fill_ref - side * _barrier_n(p) * tr["n"]
                        tr["entry_fee"] += fee

    def signals(self, i):
        """Delega la entrada en `self.rule` y arma el fill para el open de la
        barra siguiente.

        La regla se resuelve en la MISMA barra que la recibe: diferirla pierde
        un bar entero de latencia, porque para cuando llega la siguiente la
        barra de señal ya cerró y su open ya no es ejecutable. Con una
        posición abierta no se evalúa nada — un símbolo, una posición.
        """
        self.mark = self.closes[i]
        if self.open_trade is not None:
            return
        fire = self.rule.evaluate(self, i)
        if fire is not None:
            self.fire_pending = fire

    def unrealized(self):
        tr = self.open_trade
        if tr is None:
            return 0.0
        return tr["side"] * tr["qty"] * (self.mark - tr["entry"])

    def open_book(self):
        if self.open_trade is None:
            return None
        ob = dict(self.open_trade)
        ob["mark_price"] = float(self.closes[-1])
        ob["unrealized_pnl"] = (self.open_trade["side"] * self.open_trade["qty"]
                                * (self.closes[-1] - self.open_trade["entry"]))
        return ob


class DonchianFadeRule:
    """Máquina de 3 barras: ruptura -> confirmación -> entrada a contrapié.

    Es el cuerpo que `_SymbolBook.signals` tuvo siempre, movido tal cual a un
    objeto para que el motor admita otras reglas de entrada sin tocar nada de
    lo que hay alrededor (sizing, costes, funding, barreras, MAE/MFE, topes de
    cartera). `run_backtest(..., rule=None)` la construye por defecto, así que
    el camino existente es idéntico barra a barra.

    `policy` (p. ej. VwapFadePolicy) sigue viajando con ella: cambia el ancla
    de la operación y puede vetar rupturas.
    """

    def __init__(self, policy=None):
        self.policy = policy
        self.pending = None   # ruptura esperando confirmación

    def warmup_bars(self):
        return self.policy.warmup_bars() if self.policy is not None else 0

    def prepare(self, book, df, ctx=None):
        # El ancla ya la precalcula el libro (self.anchor_arr) para no cambiar
        # el orden de las operaciones del camino histórico.
        return None

    def evaluate(self, book, i):
        p = book.p
        h, l, c = book.highs[i], book.lows[i], book.closes[i]
        n_val = book.n_arr[i] if i < len(book.n_arr) else np.nan
        if i < book.min_bars or (n_val != n_val) or n_val <= 0:
            return None
        flat_bar = is_flat_bar(h, l, book.volumes[i])
        if self.pending is not None:
            if flat_bar:
                # Un cierre plano no confirma nada.
                self.pending = None
                return None
            pend = self.pending
            confirmed = ((c < pend["level"]) if pend["side"] > 0
                         else (c > pend["level"]))
            if confirmed:
                # Arma el fill para el open de la SIGUIENTE barra —
                # el mismo instante en que ÉSTA cerró.
                self.pending = None
                return {"side": -pend["side"], "n": pend["n"],
                        "mid": pend["mid"]}
            # No confirmó: la ruptura sigue extendiéndose. Esta MISMA barra
            # se reevalúa como una ruptura nueva contra el canal ya
            # actualizado (que ahora incluye la barra que originó la ruptura
            # anterior) — sin esto se pierde un ciclo completo antes de
            # volver a intentarlo.
            self.pending = book.detect_breakout(i, h, l, n_val)
            return None

        brk = book.detect_breakout(i, h, l, n_val)
        if brk is None:
            return None
        if p.require_confirmation:
            self.pending = brk
            return None
        # Sin confirmación: se entra pase lo que pase, al open de la
        # barra siguiente.
        return {"side": -brk["side"], "n": brk["n"], "mid": brk["mid"]}


class OpeningRangeVwapRule:
    """Momentum del rango de apertura contra el VWAP de 24 h.

    La cara de backtest de `IntradayMomentumStrategy`: ambas llaman a
    `opening_range_signal`, `compute_vwap` y `compute_n`, así que no pueden
    divergir en la regla, solo en el camino por el que llegan los datos.

    El ancla y la N se calculan sobre `ctx` — el marco 24/7 SIN filtrar — y se
    reindexan sobre las barras de sesión. Calcularlas sobre `df` daría un VWAP
    de las últimas 288 barras DE SESIÓN, es decir casi cuatro días de solo
    horario regular, que no es lo que dice la estrategia y además se saltaría
    justo el tramo overnight que el ancla pretende resumir.

    Causalidad: `compute_vwap` y `compute_n` son ventanas móviles hacia atrás,
    así que el valor en la marca t usa velas <= t. La barra de señal es la que
    ABRE a las 09:30 y CIERRA a las 09:35, y su ancla incluye esa misma vela:
    todo lo que entra en la decisión ya había ocurrido cuando se toma.
    """

    def __init__(self, or_bars=1, vwap_bars=288, vwap_mode="rolling",
                 n_window=288, n_mode="roll24h", n_period=20,
                 allow_short=True):
        self.or_bars = int(or_bars)
        self.vwap_bars = int(vwap_bars)
        self.vwap_mode = vwap_mode
        self.n_window = int(n_window)
        # "roll24h": recorrido de las últimas `n_window` velas (lo que mide el
        # horizonte de la posición y lo que usa el motor en vivo, porque sale
        # del mismo búfer que el ancla). "daily": la N Turtle clásica, ATR(20)
        # sobre velas diarias del marco 24/7, leída con un día de retraso.
        # Las dos dan ~2 000 $ con BTC a 46 000 $; `compute_n` sobre velas de
        # 5 minutos da ~104 $ y no es comparable (ver `rolling_range_n`).
        self.n_mode = n_mode
        self.n_period = int(n_period)
        self.allow_short = allow_short
        self.policy = None
        self.anchor = None
        self.n_ctx = None
        self.signal_bar = None

    def warmup_bars(self):
        # El calentamiento se resuelve con los NaN de `ctx`, que es donde se
        # miden el ancla y la N; exigir barras de sesión aquí descartaría
        # sesiones que sí tienen 24 h de historia detrás.
        return 0

    def prepare(self, book, df, ctx=None):
        if ctx is None:
            raise ValueError(
                "OpeningRangeVwapRule necesita `ctx`: el marco 24/7 sobre el "
                "que se miden el VWAP de 24 h y la N. Sin él el ancla se "
                "calcularía sobre barras de sesión y no sería la de la "
                "estrategia.")
        if not df.index.isin(ctx.index).all():
            raise ValueError(
                "`df` debe ser un subconjunto de `ctx` (la serie de sesión "
                "sale de filtrar la 24/7). Reindexar con relleno aquí "
                "arrastraría un ancla vieja sin avisar.")
        h, l, c = (ctx["high"].to_numpy(dtype=float),
                   ctx["low"].to_numpy(dtype=float),
                   ctx["close"].to_numpy(dtype=float))
        v = (ctx["volume"].to_numpy(dtype=float) if "volume" in ctx.columns
             else np.zeros(len(ctx)))
        anchor = compute_vwap(h, l, c, v, mode=self.vwap_mode,
                              bars=self.vwap_bars, index=ctx.index)
        if self.n_mode == "roll24h":
            n_ctx = rolling_range_n(h, l, window=self.n_window)
        elif self.n_mode == "daily":
            # Velas diarias del marco 24/7 y `shift(1)`: a las 09:35 de la
            # sesión D, la N es la de los 20 días cerrados hasta D-1. La vela
            # diaria de D todavía está en curso a esa hora — usarla sería
            # look-ahead de medio día.
            day = ctx.resample("1D").agg({"high": "max", "low": "min",
                                          "close": "last"}).dropna()
            n_day = pd.Series(compute_n(day["high"], day["low"], day["close"],
                                        period=self.n_period),
                              index=day.index).shift(1)
            n_ctx = n_day.reindex(ctx.index, method="ffill").to_numpy()
        else:
            raise ValueError(f"n_mode desconocido: {self.n_mode!r}")
        # Reindexado EXACTO (sin relleno): mismo instante en ambos marcos.
        self.anchor = pd.Series(anchor, index=ctx.index).reindex(df.index).to_numpy()
        self.n_ctx = pd.Series(n_ctx, index=ctx.index).reindex(df.index).to_numpy()

        # La barra que CIERRA el rango de apertura: la (or_bars-1)-ésima de
        # cada sesión, contada desde `sess_first`.
        first = book.sess_first
        offset = np.empty(len(df), dtype=np.int64)
        k = 0
        for i in range(len(df)):
            k = 0 if (first is not None and first[i]) else k + 1
            offset[i] = k
        self.signal_bar = offset == (self.or_bars - 1)
        self.or_open = np.r_[np.full(self.or_bars - 1, np.nan),
                             book.opens[:len(df) - self.or_bars + 1]] \
            if self.or_bars > 1 else book.opens
        return None

    def evaluate(self, book, i):
        if not self.signal_bar[i]:
            return None
        # Una sesión sin sus `or_bars` primeras barras (hueco de datos) no
        # tiene rango de apertura: `offset` ya lo descarta, pero el open de
        # la primera barra tiene que ser de ESTA sesión.
        open_px = self.or_open[i]
        close_px = book.closes[i]
        anchor = self.anchor[i]
        n_val = self.n_ctx[i]
        side = opening_range_signal(open_px, close_px, anchor, n_val,
                                    allow_short=self.allow_short)
        if side is None:
            if not (n_val == n_val) or n_val <= 0:
                book.acct.reject("sin_n")
            elif not (anchor == anchor) or anchor <= 0:
                book.acct.reject("sin_vwap")
            else:
                book.acct.reject("desacuerdo")
            return None
        # `mid` es el ancla: con tp_mode="take_n" no se usa como objetivo,
        # pero queda en el registro y explica por qué se abrió la posición.
        return {"side": side, "n": float(n_val), "mid": float(anchor)}


def _fills_frame(fills):
    cols = ["time", "symbol", "kind", "side", "qty", "price", "notional", "fee",
            "reason"]
    return pd.DataFrame(fills, columns=cols)


def run_backtest(df: pd.DataFrame, params: BacktestParams,
                  funding: pd.Series | None = None,
                  policy=None, path: pd.DataFrame | None = None,
                  rule=None, ctx: pd.DataFrame | None = None
                  ) -> tuple[pd.Series, pd.DataFrame]:
    """Replay `df` (UTC-naive index, open/high/low/close/volume columns, one
    symbol) bar by bar. Returns (equity_curve, trades) — trades holds only
    CLOSED trades; any position still open at the end of the sample is
    reported separately via the second return value's `.attrs["open_book"]`
    (the "libro abierto" the skill requires: without it you don't know what
    the system has on right now). Every execution (entry, add, exit) with its
    notional is in `.attrs["fills"]`.

    Pessimistic intrabar precedence: if a bar's range could plausibly touch
    both stop and target, the stop wins — there is no intrabar data here to
    know which came first, and assuming the favorable order is exactly the
    bias that inflates a backtest.

    `policy` (opcional, p. ej. `VwapFadePolicy`) cambia el ancla de la
    operación y puede vetar rupturas; sus vetos quedan contados en
    `.attrs["rejected"]`. Con `policy=None` el motor es el de siempre, barra
    por barra.
    """
    acct = _Account(params)
    book = _SymbolBook(None, df, params, funding, acct, policy=policy,
                       path=path, rule=rule, ctx=ctx)
    equity_curve = np.empty(len(df))
    for i in range(len(df)):
        book.fill_at_open(i)
        book.manage(i)
        book.signals(i)
        equity_curve[i] = acct.balance + book.unrealized()

    ec = pd.Series(equity_curve, index=df.index, name="equity")
    trades_df = pd.DataFrame(book.trades)
    trades_df.attrs["open_book"] = book.open_book()
    trades_df.attrs["fills"] = _fills_frame(book.fills)
    trades_df.attrs["rejected"] = dict(acct.rejected)
    # La regla que REALMENTE corrió. El libro copia la que se le pasa (si no,
    # los símbolos de una cartera compartirían su estado), así que el objeto
    # del llamante se queda sin preparar: sin esto no hay forma de mirar el
    # ancla, la N ni las barras de señal que produjeron estas operaciones.
    trades_df.attrs["rule"] = book.rule
    return ec, trades_df


def run_portfolio_backtest(data: dict, params: BacktestParams,
                           funding: dict | None = None,
                           symbol_params: dict | None = None,
                           corr_groups: dict | None = None,
                           max_dir_units: int = 12, max_corr_units: int = 6,
                           policy=None, paths: dict | None = None,
                           rule=None, ctxs: dict | None = None):
    """Varios símbolos sobre UNA cuenta: saldo, sizing y topes compartidos.

    `run_backtest` por símbolo con la equity repartida no responde a "qué
    hace una cuenta de X con apalancamiento máximo Y": cada símbolo tendría
    su propio tope y el bruto de la cuenta podría superar Y. Aquí el tope
    es el de la cuenta en vivo (`_PortfolioAccount`) y el riesgo por
    operación se mide sobre la equity de TODA la cuenta.

    `params` fija la cuenta (equity0, risk_per_trade, max_gross_lev,
    use_dd_rule) y las reglas por defecto; `symbol_params[s]` sustituye las
    reglas de un símbolo (p. ej. `max_units` más bajo para SOL).

    Dentro de cada marca de tiempo: primero los fills al open de todos los
    símbolos, luego barreras y adds, luego señales — en el orden de `data`.
    Un símbolo sin barra en esa marca simplemente no avanza y su posición
    se sigue marcando a su último cierre. Eso es lo que permite meter en la
    misma cartera un símbolo que empezó a cotizar años después: hasta su
    primera barra no existe para el motor y no puede emitir señal.

    `policy` (opcional) se aplica a TODOS los símbolos: es la regla de la
    estrategia, no un ajuste por símbolo.

    Devuelve (equity, trades con columna `symbol`, fills). `trades.attrs`
    lleva `open_book` por símbolo y los rechazos por tope en `rejected`;
    `equity.attrs["gross"]` el nocional bruto de la cuenta barra a barra.
    """
    funding = funding or {}
    symbol_params = symbol_params or {}
    acct = _PortfolioAccount(params, max_dir_units=max_dir_units,
                             max_corr_units=max_corr_units,
                             corr_groups=corr_groups)
    paths = paths or {}
    ctxs = ctxs or {}
    books = {s: _SymbolBook(s, d, symbol_params.get(s, params), funding.get(s),
                            acct, policy=policy, path=paths.get(s),
                            rule=rule, ctx=ctxs.get(s))
             for s, d in data.items()}
    acct.books = list(books.values())

    union = pd.DatetimeIndex(sorted(set().union(*(d.index for d in data.values()))))
    pos = {s: d.index.get_indexer(union) for s, d in data.items()}
    equity_curve = np.empty(len(union))
    gross_curve = np.empty(len(union))
    for k in range(len(union)):
        live = [(b, pos[s][k]) for s, b in books.items() if pos[s][k] >= 0]
        for b, i in live:
            b.fill_at_open(i)
        for b, i in live:
            b.manage(i)
        for b, i in live:
            b.signals(i)
        equity_curve[k] = acct.balance + sum(b.unrealized() for b in books.values()
                                             if b.open_trade is not None)
        gross_curve[k] = acct.gross()

    ec = pd.Series(equity_curve, index=union, name="equity")
    # Nocional bruto de la cuenta al cierre de cada barra: con ec da el
    # apalancamiento realmente usado frente a `max_gross_lev`.
    ec.attrs["gross"] = pd.Series(gross_curve, index=union, name="gross")
    trades = [dict(t, symbol=s) for s, b in books.items() for t in b.trades]
    trades_df = pd.DataFrame(trades)
    if len(trades_df):
        trades_df = trades_df.sort_values("closed", kind="stable").reset_index(drop=True)
    trades_df.attrs["open_book"] = {s: b.open_book() for s, b in books.items()}
    trades_df.attrs["rejected"] = dict(acct.rejected)
    trades_df.attrs["rules"] = {s: b.rule for s, b in books.items()}
    fills = _fills_frame([f for b in books.values() for f in b.fills])
    fills = fills.sort_values("time", kind="stable").reset_index(drop=True)
    return ec, trades_df, fills


def _position_size(balance, p: BacktestParams, n, price, use_dd_rule, ref=None):
    if n is None or not (n == n) or n <= 0:
        return 0.0
    sizing_eq = _sizing_equity(balance, p.equity0, use_dd_rule, ref=ref)
    denom = p.stop_n * n
    if denom <= 0:
        return 0.0
    qty = p.risk_per_trade * sizing_eq / denom
    # Leverage cap: notional cannot exceed max_gross_lev * balance.
    if price > 0 and qty * price > p.max_gross_lev * balance:
        qty = (p.max_gross_lev * balance) / price
    return max(qty, 0.0)
