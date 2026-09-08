from __future__ import print_function

from copy import deepcopy
from datetime import timezone
import math
from decimal import Decimal, getcontext, ROUND_HALF_DOWN
import logging
import os

import pandas as pd

from qsCrypto.event.event import OrderEvent, PortfolioEvent
from qsCrypto.performance.performance import create_drawdowns
from qsCrypto.portfolio.position import Position
from qsCrypto.settings import OUTPUT_RESULTS_DIR


class Portfolio(object):
    def __init__(
        self, ticker, events, home_currency="USDT", 
        leverage=20, equity=Decimal("100000.00"), 
        risk_per_trade=Decimal("0.02"), backtest=True
    ):
        self.ticker = ticker
        self.events = events
        self.home_currency = home_currency
        self.leverage = leverage
        self.equity = equity
        self.balance = deepcopy(self.equity)
        self.risk_per_trade = risk_per_trade
        self.backtest = backtest
        self.positions = {}
        # Excursions of closed trades, harvested in close_position() before the
        # Position object is discarded. Feeds the trade journal and the
        # winners-vs-losers MAE analysis.
        self.closed_excursions = []
        if self.backtest:
            self.backtest_file = self.create_equity_file()
        self.logger = logging.getLogger(__name__)

    def calc_risk_position_size(self, currency_pair):
        """
        Dynamically calculate the number of units to trade
        based on current price, equity, and risk per trade.
        For crypto pairs (e.g. BTCUSDT), the units are
        (equity * risk_per_trade) / current_price.
        """
        tp = self.ticker.prices
        if currency_pair not in tp or tp[currency_pair]["ask"] is None:
            self.logger.warning(
                "No price available for %s, returning 0 units", currency_pair
            )
            return Decimal("0")
        current_price = Decimal(str(tp[currency_pair]["ask"]))
        if current_price == 0:
            return Decimal("0")
        units = (self.equity * self.risk_per_trade) / current_price
        return units.quantize(Decimal("0.00001"), ROUND_HALF_DOWN)

    def add_new_position(
        self, position_type, currency_pair, units, ticker, initial_stop=None
    ):
        ps = Position(
            self.home_currency, position_type,
            currency_pair, units, ticker, initial_stop=initial_stop
        )
        self.positions[currency_pair] = ps

    def add_position_units(self, currency_pair, units):
        if currency_pair not in self.positions:
            return False
        else:
            ps = self.positions[currency_pair]
            ps.add_units(units)
            return True

    def remove_position_units(self, currency_pair, units):
        if currency_pair not in self.positions:
            return False
        else:
            ps = self.positions[currency_pair]
            pnl = ps.remove_units(units)
            self.balance += pnl
            return True

    def close_position(self, currency_pair):
        if currency_pair not in self.positions:
            return False
        else:
            ps = self.positions[currency_pair]
            pnl = ps.close_position()
            self.balance += pnl
            # The excursion has to be harvested BEFORE the position object is
            # dropped: MAE/MFE live only on the Position and there is no way to
            # recover them afterwards. `closed_excursions` is what the trade
            # journal and the winners-vs-losers analysis read.
            self.closed_excursions.append({
                "currency_pair": currency_pair,
                "position_type": ps.position_type,
                "units": ps.units,
                "entry_anchor": ps.entry_anchor,
                "exit_price": ps.cur_price,
                "pnl": pnl,
                "mae_price": ps.mae_price,
                "mfe_price": ps.mfe_price,
                "mae_base": ps.mae_base,
                "mfe_base": ps.mfe_base,
                "mae_r": ps.calculate_mae_r(),
                "mfe_r": ps.calculate_mfe_r(),
            })
            del[self.positions[currency_pair]]
            return True

    def create_equity_file(self):
        filename = "backtest.csv"
        out_file = open(os.path.join(OUTPUT_RESULTS_DIR, filename), "w")
        header = "Timestamp,Balance"
        for pair in self.ticker.pairs:
            header += ",%s" % pair
        # MAE per pair, in price units so the column is always numeric (mae_r
        # is undefined when no initial stop was supplied, and a blank would be
        # dropped by the dropna() in output_results, taking the whole row with
        # it). These columns are excluded from the Total below.
        for pair in self.ticker.pairs:
            header += ",%s_mae" % pair
        header += "\n"
        out_file.write(header)
        if self.backtest:
            print(header[:-2])
        return out_file

    def output_results(self):
        # Closes off the Backtest.csv file so it can be 
        # read via Pandas without problems
        self.backtest_file.close()
        
        in_filename = "backtest.csv"
        out_filename = "equity.csv" 
        in_file = os.path.join(OUTPUT_RESULTS_DIR, in_filename)
        out_file = os.path.join(OUTPUT_RESULTS_DIR, out_filename)

        # Create equity curve dataframe
        df = pd.read_csv(in_file, index_col=0)
        df.dropna(inplace=True)
        # Total = Balance + unrealised P&L per pair, as before. The new *_mae
        # columns are diagnostics and must be excluded: a bare df.sum(axis=1)
        # would fold the excursion into the equity curve.
        equity_cols = [c for c in df.columns if not c.endswith("_mae")]
        df["Total"] = df[equity_cols].sum(axis=1)
        df["Returns"] = df["Total"].pct_change()
        df["Equity"] = (1.0+df["Returns"]).cumprod()
        
        # Create drawdown statistics
        drawdown, max_dd, dd_duration = create_drawdowns(df["Equity"])
        df["Drawdown"] = drawdown
        df.to_csv(out_file, index=True)
        
        print("Simulation complete and results exported to %s" % out_filename)

    def update_portfolio(self, tick_event):
        """
        This updates all positions ensuring an up to date
        unrealised profit and loss (PnL).
        """
        currency_pair = tick_event.instrument
        if currency_pair in self.positions:
            ps = self.positions[currency_pair]
            ps.update_position_price()
        if self.backtest:
            out_line = "%s,%s" % (tick_event.time, self.balance)
            for pair in self.ticker.pairs:
                if pair in self.positions:
                    out_line += ",%s" % self.positions[pair].profit_base
                else:
                    out_line += ",0.00"
            for pair in self.ticker.pairs:
                if pair in self.positions:
                    out_line += ",%s" % self.positions[pair].mae_base
                else:
                    out_line += ",0.00"
            out_line += "\n"
            print(out_line[:-2])
            self.backtest_file.write(out_line)

    def execute_signal(self, signal_event):
        # Check that the prices ticker contains all necessary
        # currency pairs prior to executing an order
        execute = True
        tp = self.ticker.prices
        for pair in tp:
            if tp[pair]["ask"] is None or tp[pair]["bid"] is None:
                execute = False

        # All necessary pricing data is available,
        # we can execute
        if execute:
            side = signal_event.side
            currency_pair = signal_event.instrument
            units = self.calc_risk_position_size(currency_pair)
            time = signal_event.time

            # Guard against zero units
            if units <= 0:
                self.logger.warning(
                    "Calculated 0 units for %s — skipping order", currency_pair
                )
                return
            
            # If there is no position, create one
            if currency_pair not in self.positions:
                if side == "buy":
                    position_type = "long"
                else:
                    position_type = "short"
                self.add_new_position(
                    position_type, currency_pair, 
                    units, self.ticker
                )

            # If a position exists add or remove units
            else:
                ps = self.positions[currency_pair]

                if side == "buy" and ps.position_type == "long":
                    self.add_position_units(currency_pair, units)

                elif side == "sell" and ps.position_type == "long":
                    if units == ps.units:
                        self.close_position(currency_pair)
                    # TODO: Allow units to be added/removed
                    elif units < ps.units:
                        return
                    elif units > ps.units:
                        return

                elif side == "buy" and ps.position_type == "short":
                    if units == ps.units:
                        self.close_position(currency_pair)
                    # TODO: Allow units to be added/removed
                    elif units < ps.units:
                        return
                    elif units > ps.units:
                        return
                        
                elif side == "sell" and ps.position_type == "short":
                    self.add_position_units(currency_pair, units)

            order = OrderEvent(currency_pair, units, "market", side)
            self.events.put(order)

            self.logger.info("Portfolio Balance: %s" % self.balance)
        else:
            self.logger.info("Unable to execute order as price data was insufficient.")


class IBKRFuturesPortfolio(Portfolio):
    """Portfolio para futuros de CME con la aritmética del backtest.

    `Portfolio` es agnóstico de venue: solo toca `ticker.prices` y
    `ticker.pairs`. Lo que cambia aquí es lo que el contrato de futuros y la
    estrategia de reversión exigen y la clase base no sabe hacer:

    1. **Sizing.** `Portfolio.calc_risk_position_size` hace `equity*risk/ask` y
       cuantiza a 0.00001: ni multiplicador de contrato ni N, y devuelve
       fracciones. Un futuro se compra en unidades enteras y su riesgo por
       contrato es `stop_n * N * multiplicador`, no el precio.
    2. **R.** La clase base nunca pasa `initial_stop` a `add_new_position`, así
       que `r_unit` queda a None y `calculate_mae_r()` devuelve None SIEMPRE.
       El MAE en R —lo que el enunciado pide monitorizar— no existe hoy en el
       camino dirigido por eventos. Aquí se calcula el stop antes de abrir y se
       pasa, que es lo que hace que R esté definido.
    3. **Barreras.** Objetivo en el centro del canal congelado en la ruptura y
       barrera vertical por número de barras, evaluadas sobre barras cerradas
       con prelación pesimista.
    4. **Reconciliación.** `update_fill` reancla la posición al precio REAL del
       bróker; hasta ahora el estado local se actualizaba con el precio de
       referencia y nadie comprobaba el fill.
    """

    def __init__(self, ticker, events, home_currency="USD", leverage=1,
                 equity=Decimal("100000.00"), risk_per_trade=Decimal("0.01"),
                 backtest=False, specs=None, stop_n=1.0, add_n=0.5,
                 max_units=4, max_hold_bars=10, tp_mode="mid_channel",
                 take_n=5.0, max_gross_lev=4.0, max_dir_units=12,
                 max_corr_units=6, corr_groups=None, use_dd_rule=True,
                 execution=None, strategy=None):
        super(IBKRFuturesPortfolio, self).__init__(
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
        # Estado por símbolo que la clase base no modela: N y nivel congelados,
        # unidades de la escalera, último fill y barras vividas.
        self.meta = {}
        self.rejected = {"qty_cero": 0, "apalancamiento": 0, "topes": 0}

    # ── reconciliación con el bróker ─────────────────────────────────────────
    def _vwap_from_executions(self, ib, contract, account=None):
        """VWAP de entrada + hora del primer fill, de las ejecuciones REALES
        del día — no de `avgCost`, que IBKR no siempre reporta con la misma
        convención (y que para un futuro exige dividir por el multiplicador
        adivinando la convención correcta).

        Camina las ejecuciones de hoy en orden y lleva una base de costo
        corriente: los fills que amplían la posición (misma dirección o
        posición en cero) suman al costo medio; un fill en dirección
        contraria se trata como reducción a ese mismo costo medio, tal como
        lo hace el propio bróker. Si la posición pasa por cero y se vuelve a
        abrir en el mismo día, la base se reinicia — solo interesa la
        entrada de la posición VIVA ahora mismo.

        Devuelve `(vwap, hora_primer_fill, num_fills_de_entrada)`, o
        `(None, None, 0)` si no hay ejecuciones utilizables (p.ej. la
        posición es de un día anterior y `reqExecutions` sin filtro de
        tiempo no la trae completa).
        """
        try:
            from ib_async import ExecutionFilter
            filt = ExecutionFilter()
            if account:
                filt.acctCode = account
            fills = ib.reqExecutions(filt)
        except Exception as exc:
            self.logger.warning("reqExecutions falló: %s", exc)
            return None, None, 0

        fills = [f for f in fills if f.contract.conId == contract.conId
                 and (not account or f.execution.acctNumber == account)]
        fills.sort(key=lambda f: f.time)

        running_qty, running_cost = 0.0, 0.0
        entry_time, n_fills = None, 0
        for f in fills:
            signed = f.execution.shares if f.execution.side == "BOT" \
                else -f.execution.shares
            same_dir = running_qty == 0 or (running_qty > 0) == (signed > 0)
            if same_dir:
                running_cost += f.execution.price * abs(signed)
                running_qty += signed
                n_fills += 1
                if entry_time is None:
                    entry_time = f.time
            else:
                avg = running_cost / abs(running_qty) if running_qty else 0.0
                running_qty += signed
                if running_qty == 0:
                    entry_time, n_fills = None, 0
                    running_cost = 0.0
                else:
                    running_cost = avg * abs(running_qty)
        if running_qty == 0:
            return None, None, 0
        return running_cost / abs(running_qty), entry_time, n_fills

    def _find_resting_stop(self, ib, contract, account=None):
        """Orden STP abierta sobre el contrato, mirando TODOS los clientes.

        `ib.reqOpenOrders()` a secas solo devuelve las órdenes del `clientId`
        de esta conexión — un stop colocado por otra sesión, por TWS a mano,
        o sobrevivido a un reinicio con un `clientId` distinto queda
        invisible y `reconcile_from_broker` reportaría "sin stop" con el
        stop vivo delante. `reqAllOpenOrders()` sí ve todos los clientes de
        la cuenta; se combinan ambas por si acaso una sola de las dos falla.
        """
        orders, seen = [], set()
        for fetch in (ib.reqAllOpenOrders, ib.reqOpenOrders):
            try:
                for oo in fetch():
                    key = (getattr(oo.order, "orderId", None),
                           getattr(oo.order, "account", None))
                    if key not in seen:
                        seen.add(key)
                        orders.append(oo)
            except Exception as exc:
                self.logger.warning("%s falló: %s", fetch.__name__, exc)
        for oo in orders:
            if (oo.contract.conId == contract.conId
                    and str(oo.order.orderType).startswith("STP")
                    and (account is None or oo.order.account == account)):
                return float(oo.order.auxPrice), int(oo.order.totalQuantity)
        return None, None

    def reconcile_from_broker(self, ib, contracts, account=None):
        """Reconstruye posiciones desde el estado REAL de la cuenta.

        `self.positions` nace vacío en `__init__` pase lo que pase en la
        cuenta: si el kernel se reinició con una posición todavía abierta (o
        si la abrió una sesión anterior), este portfolio no tiene forma de
        saberlo por sí solo — `snapshot()` devolvería `[]` para siempre y el
        motor podría incluso intentar abrir una segunda posición encima de
        la real. Llamar a esto una vez, justo después de construir el
        portfolio y antes de arrancar los hilos, cierra ese hueco.

        La cantidad y el lado vienen de `ib.positions()` (la única fuente
        fiable de "hay posición ahora"); el precio de entrada y la hora del
        primer fill vienen de `_vwap_from_executions` (las ejecuciones REALES
        del día, no `avgCost`); el stop vigente de `_find_resting_stop`
        (mirando todos los `clientId`, no solo el de esta sesión). El canal
        (`level`/`mid`) se aproxima con el Donchian ACTUAL de la estrategia
        (`strategy._channel`) — es una aproximación, no el canal congelado en
        la ruptura original que ya no existe en ningún sitio, pero es mucho
        mejor que renunciar al objetivo por completo; se marca `reconciled`
        para que quede claro en el journal. `bars` se reconstruye contando
        las barras cerradas desde el primer fill, en vez de arrancar
        siempre en 0.

        Si no encuentra una orden de stop abierta, dispara un log CRITICAL:
        significa que la posición está, en ese instante, sin barrera
        automática — requiere intervención manual en TWS.

        También reconcilia en la dirección CONTRARIA: si `self.positions`
        tiene un símbolo que el bróker ya no tiene, lo purga localmente. Es
        el mismo hueco que "el bróker tiene algo que el local no ve", pero
        al revés — puede pasar si `_exit()` cerró de verdad en el bróker
        pero la contabilidad local (`close_position`) falló a medio camino
        y dejó una posición fantasma: el motor seguiría creyendo que hay
        algo que gestionar (bloqueando nuevas señales del símbolo) sobre
        algo que ya no existe.
        """
        # ── purga de posiciones LOCALES sin contrapartida en el bróker ──
        for pair in list(self.positions.keys()):
            contract = contracts.get(pair)
            if contract is None:
                continue
            still_open = any(
                p.contract.conId == contract.conId and float(p.position) != 0
                and (account is None or p.account == account)
                for p in ib.positions()
            )
            if not still_open:
                self.logger.critical(
                    "%s: posición local sin contrapartida en el bróker "
                    "(huérfana de un cierre previo). Se purga localmente "
                    "sin PnL exacto — revisa el journal/TWS para el "
                    "resultado real.", pair)
                self.positions.pop(pair, None)
                self.meta.pop(pair, None)
                self._sync_strategy()

        for pair, contract in contracts.items():
            if pair in self.positions:
                continue
            matches = [
                p for p in ib.positions()
                if p.contract.conId == contract.conId
                and float(p.position) != 0
                and (account is None or p.account == account)
            ]
            if not matches:
                continue
            ib_pos = matches[0]
            qty = int(round(float(ib_pos.position)))
            side = 1 if qty > 0 else -1

            entry_px, entry_time, n_fills = self._vwap_from_executions(
                ib, contract, account)
            if entry_px is None:
                mult = self.multiplier(pair) or 1.0
                entry_px = float(ib_pos.avgCost) / mult
                entry_time, n_fills = None, 1
                self.logger.warning(
                    "Sin ejecuciones de hoy para %s: uso avgCost=%.5f como "
                    "entrada aproximada (posible posición de un día "
                    "anterior).", pair, entry_px)

            stop_px, stop_qty = self._find_resting_stop(ib, contract, account)

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

            bars_elapsed = 0
            h = self.ticker.history.get(pair) if entry_time is not None else None
            if h is not None and len(h):
                et = entry_time
                if getattr(et, "tzinfo", None) is not None:
                    et = et.astimezone(timezone.utc).replace(tzinfo=None)
                bars_elapsed = int((h.index > et).sum())

            # `Position()` exige un bid/ask utilizable en el instante de
            # construirse (`set_up_currencies`). Esto se llama justo después
            # de construir el portfolio, ANTES de arrancar el streaming, así
            # que `self.ticker.prices[pair]` todavía tiene bid/ask en None —
            # `Decimal(str(None))` revienta con InvalidOperation. Se pide un
            # snapshot puntual solo para poder construir el objeto; el ancla
            # de entrada real (`entry_px`, de las ejecuciones) se reaplica
            # justo después con `reanchor_entry` — si se dejara el snapshot
            # como ancla, R y el MAE/MFE se medirían contra el precio actual
            # del mercado en vez de contra la entrada real.
            snap_bid = snap_ask = None
            try:
                tk = ib.reqTickers(contract)[0]
                if tk.bid is not None and tk.bid == tk.bid and tk.bid > 0:
                    snap_bid = float(tk.bid)
                if tk.ask is not None and tk.ask == tk.ask and tk.ask > 0:
                    snap_ask = float(tk.ask)
            except Exception as exc:
                self.logger.warning(
                    "Snapshot de precio para %s falló: %s", pair, exc)
            self.ticker.prices[pair]["bid"] = snap_bid or entry_px
            self.ticker.prices[pair]["ask"] = snap_ask or entry_px

            self.add_new_position(
                "long" if side > 0 else "short", pair, abs(qty), self.ticker,
                initial_stop=stop_px,
            )
            self.positions[pair].reanchor_entry(entry_px)
            self.meta[pair] = {
                "side": side, "n": n_est, "level": level, "mid": mid,
                "units": max(1, min(n_fills, self.max_units)), "last_fill": entry_px,
                "stop": stop_px, "bars": bars_elapsed, "qty": abs(qty),
                "reconciled": True,
            }
            self._sync_strategy()
            tag = "%s %s x%d @ %.5f (%d fill(s), %d barra(s) desde entrada)" % (
                pair, "long" if side > 0 else "short", abs(qty), entry_px,
                n_fills, bars_elapsed)
            if stop_px is None:
                self.logger.critical(
                    "RECONCILIADA %s SIN STOP detectado en el bróker (ni por "
                    "clientId propio ni por reqAllOpenOrders): sin barrera "
                    "automática hasta colocar uno manualmente en TWS.", tag)
            else:
                self.logger.warning(
                    "RECONCILIADA %s stop=%.5f (qty orden=%s) mid≈%s "
                    "(aproximado, no el canal original).",
                    tag, stop_px, stop_qty, mid)
        self._emit_state("reconcile")

    # ── sizing ──────────────────────────────────────────────────────────────
    def multiplier(self, pair):
        return float(self.specs.get(pair, {}).get("multiplier", 1.0))

    def min_tick(self, pair):
        return float(self.specs.get(pair, {}).get("minTick", 0.0))

    def sizing_equity(self):
        """Equity sobre el que se dimensiona, con la regla Turtle de drawdown.

        Cada −10% de equity reduce el nocional un 20%, con suelo en el 5% del
        capital inicial. Reduce el tamaño precisamente cuando la racha va mal,
        que es cuando la ruina se construye.
        """
        eq = float(self.balance)
        if not self.use_dd_rule:
            return max(eq, float(self.equity0) * 0.05)
        eq0 = float(self.equity0)
        adj = eq
        if eq < eq0:
            steps = int((eq0 - eq) / (0.10 * eq0))
            adj = eq * (0.80 ** steps)
        return max(adj, eq0 * 0.05)

    def calc_risk_position_size(self, currency_pair, n=None):
        """Contratos ENTEROS cuyo movimiento de 1R cuesta `risk_per_trade`.

        `floor`, nunca `round`: redondear hacia arriba excedería en silencio el
        riesgo declarado. Si no cabe ni un contrato la señal se rechaza — media
        posición no existe en un futuro.
        """
        if n is None or not (n == n) or n <= 0:
            return 0
        mult = self.multiplier(currency_pair)
        denom = self.stop_n * float(n) * mult
        if denom <= 0:
            return 0
        return int(math.floor(float(self.risk_per_trade) * self.sizing_equity() / denom))

    def _fits_leverage(self, pair, qty, price):
        gross = sum(
            abs(float(p.units)) * float(p.cur_price) * self.multiplier(sym)
            for sym, p in self.positions.items()
        )
        gross += abs(qty) * float(price) * self.multiplier(pair)
        return gross <= self.max_gross_lev * float(self.balance)

    def _unit_counts(self):
        longs = sum(m["units"] for s, m in self.meta.items()
                    if s in self.positions and m["side"] > 0)
        shorts = sum(m["units"] for s, m in self.meta.items()
                     if s in self.positions and m["side"] < 0)
        return longs, shorts

    # ── entrada ─────────────────────────────────────────────────────────────
    def execute_signal(self, signal_event):
        pair = signal_event.instrument
        side = 1 if signal_event.side == "buy" else -1
        n = signal_event.n

        if pair in self.positions:
            return                        # una posición por símbolo

        px = self._ref_price(pair, signal_event.side)
        if px is None:
            self.logger.info("Sin precio utilizable para %s: no se opera.", pair)
            return

        units = self.calc_risk_position_size(pair, n=n)
        if units <= 0:
            self.rejected["qty_cero"] += 1
            self.logger.warning("qty=0 para %s con N=%s: señal descartada.", pair, n)
            return

        longs, shorts = self._unit_counts()
        if (side > 0 and longs + 1 > self.max_dir_units) or \
           (side < 0 and shorts + 1 > self.max_dir_units):
            self.rejected["topes"] += 1
            return
        if not self._fits_leverage(pair, units, px):
            self.rejected["apalancamiento"] += 1
            self.logger.warning("Apalancamiento: %s x%d rechazado.", pair, units)
            return

        stop_px = float(px) - side * self.stop_n * float(n)
        self.add_new_position(
            "long" if side > 0 else "short", pair, units, self.ticker,
            initial_stop=stop_px,
        )
        self.meta[pair] = {
            "side": side, "n": float(n), "level": signal_event.level,
            "mid": signal_event.mid, "units": 1, "last_fill": float(px),
            "stop": stop_px, "bars": 0, "qty": units,
        }
        order = OrderEvent(pair, units, "market", signal_event.side)
        order.stop_price = stop_px          # el bracket lo necesita
        order.n = float(n)
        self.events.put(order)
        self._sync_strategy()
        self._emit_state("entry")
        self.logger.info("ENTRADA %s %s x%d stop=%.5f R=%.5f",
                         signal_event.side, pair, units, stop_px,
                         self.stop_n * float(n))

    def _ref_price(self, pair, side):
        p = self.ticker.prices.get(pair, {})
        px = p.get("ask") if side == "buy" else p.get("bid")
        if px is None:
            return None
        px = Decimal(str(px))
        return None if not px.is_finite() else px

    # ── barras: barreras de objetivo y vertical ─────────────────────────────
    def on_bar(self, bar_event):
        """Objetivo y barrera vertical. El stop lo ejecuta el bróker.

        Prelación pesimista: si en la misma barra se pudieran tocar stop y
        objetivo, el stop manda. Sin datos intrabarra no se sabe cuál llegó
        antes, y suponer el favorable es exactamente el sesgo que infla un
        backtest.
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
        hit_stop = (not flat) and stop_px is not None and (
            (lo <= stop_px) if side > 0 else (hi >= stop_px))

        take_px = m.get("mid") if self.tp_mode == "mid_channel" else (
            (m["last_fill"] + sd * self.take_n * m["n"])
            if m.get("n") is not None else None)
        hit_take = (not flat) and take_px is not None and (
            (hi >= take_px) if side > 0 else (lo <= take_px))

        if hit_stop:
            self._exit(pair, stop_px, "stop")           # pesimista: primero
        elif hit_take:
            self._exit(pair, take_px, "target")
        elif m["bars"] >= self.max_hold_bars:
            self._exit(pair, float(bar_event.close), "vertical")
        elif not flat:
            self._try_add(pair, bar_event)

    def _try_add(self, pair, bar_event):
        """Escalera Turtle: una unidad más cada ½N, hasta `max_units`.

        Una posición reconciliada desde el bróker sin N conocido (ver
        `reconcile_from_broker`) no puede calcular el disparador de la
        escalera: se abstiene de añadir en vez de adivinar.
        """
        m = self.meta[pair]
        if m["units"] >= self.max_units or m.get("n") is None:
            return
        side, sd = m["side"], float(m["side"])
        # `add_until_mid`: no se engorda una posición que ya llegó al objetivo.
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
        self.events.put(order)
        self.logger.info("ADD %s unidad %d en %.5f", pair, m["units"] + 1, fill_ref)

    def _exit(self, pair, price, reason):
        m = self.meta.pop(pair, None)
        if pair in self.positions:
            try:
                self.close_position(pair)
            except Exception as exc:
                # `close_position` calcula PnL contra el precio actual del
                # ticker: si en ese instante `self.ticker.prices[pair]` no
                # tiene bid/ask utilizable, revienta. Sin este try/except la
                # posición quedaba "fantasma": nunca se borraba de
                # `self.positions`, así que el motor seguía creyendo que
                # había algo que gestionar (bloqueando nuevas señales del
                # mismo símbolo) sobre una posición que el bróker ya cerró.
                # Se fuerza el cierre local igual — sin el PnL exacto, que
                # hay que reconstruir del journal/TWS — porque una
                # contabilidad incompleta es preferible a un motor
                # atascado sobre un fantasma.
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
        self.logger.info("SALIDA %s por %s en %.5f", pair, reason, price)

    # ── fills reales del bróker ─────────────────────────────────────────────
    def update_fill(self, fill_event):
        """Reancla al precio REALMENTE ejecutado.

        El estado local se creó con el precio de referencia de la señal. El
        precio que define R y el ancla de la excursión es este, y con fills
        parciales además cambia la cantidad. Sin este paso el MAE se mide contra
        un precio al que nunca se entró.
        """
        pair = fill_event.instrument
        if pair not in self.positions or pair not in self.meta:
            self.logger.warning("FILL de %s sin posición local: revisar cuenta.", pair)
            return
        m, ps = self.meta[pair], self.positions[pair]
        vwap = float(fill_event.fill_price)
        sd = float(m["side"])

        if fill_event.is_add:
            m["units"] += 1
            m["qty"] += int(fill_event.units)
            m["last_fill"] = vwap
            ps.add_units(int(fill_event.units))
            ps.reanchor_entry(Decimal(str(vwap)))
            m["stop"] = vwap - sd * self.stop_n * m["n"]
            if self.execution is not None:
                self.execution.modify_stop(pair, m["stop"], m["qty"])
        else:
            m["last_fill"] = vwap
            m["qty"] = int(fill_event.units)
            ps.units = int(fill_event.units)
            ps.reanchor_entry(Decimal(str(vwap)))
            m["stop"] = vwap - sd * self.stop_n * m["n"]
        self._emit_state("fill")

    # ── estado hacia el monitor y el journal ────────────────────────────────
    def _sync_strategy(self):
        if self.strategy is not None and hasattr(self.strategy, "set_open_symbols"):
            self.strategy.set_open_symbols(self.positions.keys())

    def snapshot(self):
        """Una fila por posición abierta, en R. Es lo que lee el monitor."""
        rows = []
        for pair, ps in self.positions.items():
            m = self.meta.get(pair, {})
            mae_r, mfe_r = ps.calculate_mae_r(), ps.calculate_mfe_r()
            r_unit = ps.r_unit
            dist_r = None
            if r_unit and m.get("stop") is not None:
                dist_r = float(abs(ps.cur_price - Decimal(str(m["stop"])))) / float(r_unit)
            rows.append({
                "pair": pair, "side": ps.position_type, "qty": int(ps.units),
                "units": m.get("units"), "entry": float(ps.entry_anchor),
                "price": float(ps.cur_price), "stop": m.get("stop"),
                "n": m.get("n"), "r_unit": None if r_unit is None else float(r_unit),
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
