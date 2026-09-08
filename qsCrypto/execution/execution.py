from __future__ import print_function

from abc import ABCMeta, abstractmethod
from decimal import Decimal, ROUND_DOWN
import logging

from qsCrypto.event.event import FillEvent
import math
import os

from dotenv import load_dotenv


class ExecutionHandler(object):
    """
    Provides an abstract base class to handle all execution in the
    backtesting and live trading system.
    """

    __metaclass__ = ABCMeta

    @abstractmethod
    def execute_order(self):
        """
        Send the order to the brokerage.
        """
        raise NotImplementedError("Should implement execute_order()")


class SimulatedExecution(object):
    """
    Provides a simulated execution handling environment. This class
    actually does nothing - it simply receives an order to execute.

    Instead, the Portfolio object actually provides fill handling.
    This will be modified in later versions.
    """
    def execute_order(self, event):
        pass


# ── Binance helpers ───────────────────────────────────────────────

def load_api_credentials(test_mode=True):
    """
    Load Binance API credentials from environment variables.
    Returns (api_key, secret_key).
    """
    load_dotenv()
    if test_mode:
        api_key = os.environ.get("BINANCE_TESTNET_API_KEY")
        secret_key = os.environ.get("BINANCE_TESTNET_SECRET_KEY")
    else:
        api_key = os.environ.get("BINANCE_API_KEY")
        secret_key = os.environ.get("BINANCE_SECRET_KEY")
    if not api_key or not secret_key:
        raise ValueError(
            "Binance API credentials not found. "
            "Set BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_SECRET_KEY "
            "(or production equivalents) in your .env file."
        )
    return api_key, secret_key


def create_execution_handler(test_mode=True, market_type="usdm_futures",
                            broker="binance", **kwargs):
    """
    Factory that returns an execution handler for the requested broker.

    `broker` defaults to "binance" so the existing call in trading.py keeps
    returning exactly what it returned before. The IBKR branch needs objects
    that only the caller can build (a live IB connection, the resolved
    contracts and their specs), so they arrive through **kwargs rather than
    being constructed from settings.
    """
    from qsCrypto import settings

    if broker.lower() in ("ibkr", "ib", "interactive_brokers"):
        return IBKRExecutionHandler(
            events_queue=kwargs["events_queue"],
            ib=kwargs["ib"],
            contracts=kwargs["contracts"],
            specs=kwargs["specs"],
            account=kwargs.get("account"),
        )

    api_key, secret_key = load_api_credentials(test_mode)
    env_label = "testnet" if test_mode else "production"
    base_url = settings.BINANCE_ENVIRONMENTS[market_type]["api"][env_label]
    return BinancePerpetualExecutionHandler(
        api_key=api_key,
        secret_key=secret_key,
        base_url=base_url,
        market_type=market_type,
    )


class BinancePerpetualExecutionHandler(ExecutionHandler):
    """
    Handles order execution against Binance USDⓈ-M or COIN-M
    perpetual futures via the official binance-futures-connector.
    """

    def __init__(self, api_key, secret_key, base_url, market_type="usdm_futures"):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url
        self.market_type = market_type
        self.logger = logging.getLogger(__name__)
        self.client = self._create_client()
        self._exchange_info = None  # lazy-loaded cache

    # ── Client creation ───────────────────────────────────────────

    def _create_client(self):
        if self.market_type == "usdm_futures":
            from binance.um_futures import UMFutures
            return UMFutures(
                key=self.api_key,
                secret=self.secret_key,
                base_url=self.base_url,
            )
        elif self.market_type == "coinm_futures":
            from binance.cm_futures import CMFutures
            return CMFutures(
                key=self.api_key,
                secret=self.secret_key,
                base_url=self.base_url,
            )
        else:
            raise ValueError(f"Unsupported market_type: {self.market_type}")

    # ── Exchange info & symbol filters ────────────────────────────

    def _load_exchange_info(self):
        """Fetch and cache /fapi/v1/exchangeInfo (or /dapi equivalent)."""
        if self._exchange_info is None:
            self.logger.info("Fetching exchange info from %s …", self.base_url)
            self._exchange_info = self.client.exchange_info()
        return self._exchange_info

    def get_symbol_filters(self, symbol):
        """
        Return a dict of filter dicts keyed by filterType for *symbol*.
        E.g. filters["PRICE_FILTER"]["tickSize"] -> '0.01'
        """
        info = self._load_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol.upper():
                return {f["filterType"]: f for f in s["filters"]}
        raise ValueError(f"Symbol {symbol} not found in exchange info")

    def round_quantity(self, symbol, qty):
        """
        Round *qty* down to the nearest valid step size
        according to the LOT_SIZE filter for *symbol*.
        """
        filters = self.get_symbol_filters(symbol)
        step_size = Decimal(filters["LOT_SIZE"]["stepSize"])
        qty = Decimal(str(qty))
        # number of decimals in stepSize
        if step_size == 0:
            return qty
        precision = int(round(-math.log10(float(step_size))))
        rounded = (qty // step_size) * step_size
        return float(round(rounded, precision))

    def round_price(self, symbol, price):
        """
        Round *price* to the nearest valid tick size
        according to the PRICE_FILTER for *symbol*.
        """
        filters = self.get_symbol_filters(symbol)
        tick_size = Decimal(filters["PRICE_FILTER"]["tickSize"])
        price = Decimal(str(price))
        if tick_size == 0:
            return price
        precision = int(round(-math.log10(float(tick_size))))
        rounded = (price // tick_size) * tick_size
        return float(round(rounded, precision))

    def validate_order(self, symbol, qty, price=None):
        """
        Validate that qty and price respect the exchange filters.
        Returns (rounded_qty, rounded_price | None).
        Raises ValueError on min-notional / min-qty violations.
        """
        filters = self.get_symbol_filters(symbol)

        rounded_qty = self.round_quantity(symbol, qty)

        min_qty = float(filters["LOT_SIZE"]["minQty"])
        max_qty = float(filters["LOT_SIZE"]["maxQty"])
        if rounded_qty < min_qty:
            raise ValueError(
                f"Quantity {rounded_qty} below minimum {min_qty} for {symbol}"
            )
        if rounded_qty > max_qty:
            raise ValueError(
                f"Quantity {rounded_qty} above maximum {max_qty} for {symbol}"
            )

        rounded_price = None
        if price is not None:
            rounded_price = self.round_price(symbol, price)

        # MIN_NOTIONAL check (qty * price >= minNotional)
        if "MIN_NOTIONAL" in filters and rounded_price is not None:
            min_notional = float(filters["MIN_NOTIONAL"].get("notional", 0))
            if rounded_qty * rounded_price < min_notional:
                raise ValueError(
                    f"Notional {rounded_qty * rounded_price:.4f} below "
                    f"minimum {min_notional} for {symbol}"
                )

        return rounded_qty, rounded_price

    # ── Order execution ───────────────────────────────────────────

    def execute_order(self, event):
        """
        Execute an OrderEvent as a market order on Binance Futures.
        Applies proper precision rounding before submission.
        """
        symbol = event.instrument.upper()
        side_map = {"buy": "BUY", "sell": "SELL"}
        side = side_map.get(event.side, event.side.upper())

        rounded_qty = self.round_quantity(symbol, event.units)

        self.logger.info(
            "Executing %s %s | qty=%s (raw %s)",
            side, symbol, rounded_qty, event.units,
        )

        try:
            response = self.client.new_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=rounded_qty,
            )
            self.logger.info("Order response: %s", response)
            return response
        except Exception as e:
            self.logger.error("Order failed for %s: %s", symbol, e)
            raise

    def execute_market_order(self, symbol, side, quantity):
        """
        Convenience method for placing a standalone market order
        outside the event-driven loop.
        """
        symbol = symbol.upper()
        side = side.upper()
        rounded_qty = self.round_quantity(symbol, quantity)
        self.logger.info(
            "Market order %s %s qty=%s", side, symbol, rounded_qty
        )
        try:
            response = self.client.new_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=rounded_qty,
            )
            self.logger.info("Order response: %s", response)
            return response
        except Exception as e:
            self.logger.error("Order failed: %s", e)
            raise

    def execute_limit_order(self, symbol, side, quantity, price, time_in_force="GTC"):
        """
        Place a limit order with proper precision rounding.
        """
        symbol = symbol.upper()
        side = side.upper()
        rounded_qty = self.round_quantity(symbol, quantity)
        rounded_price = self.round_price(symbol, price)
        self.logger.info(
            "Limit order %s %s qty=%s @ %s", side, symbol, rounded_qty, rounded_price
        )
        try:
            response = self.client.new_order(
                symbol=symbol,
                side=side,
                type="LIMIT",
                quantity=rounded_qty,
                price=rounded_price,
                timeInForce=time_in_force,
            )
            self.logger.info("Order response: %s", response)
            return response
        except Exception as e:
            self.logger.error("Limit order failed: %s", e)
            raise

    # ── Account helpers ───────────────────────────────────────────

    def get_account_info(self):
        return self.client.account()

    def get_position_info(self, symbol=None):
        if symbol:
            return self.client.get_position_risk(symbol=symbol.upper())
        return self.client.get_position_risk()

    def cancel_all_orders(self, symbol):
        return self.client.cancel_open_orders(symbol=symbol.upper())

    def get_open_orders(self, symbol=None):
        if symbol:
            return self.client.get_open_orders(symbol=symbol.upper())
        return self.client.get_open_orders()


class IBKRExecutionHandler(ExecutionHandler):
    """Órdenes contra IBKR con stop adjunto, y fills de vuelta al bus.

    Dos cosas que no hace el handler de Binance y aquí son obligatorias:

    1. **Bracket nativo.** La orden padre a mercado sale con `transmit=False` y
       el stop hijo con `transmit=True`: IBKR no activa nada hasta recibir el
       hijo, así que no existe el instante en el que hay posición abierta y
       todavía no hay stop. Si el proceso muere, el stop sigue vivo en el
       bróker.
    2. **`order.account` explícito.** Este login es un FA demo: el master `DF*`
       rechaza las órdenes que no llevan reparto, y las posiciones se reportan
       bajo etiquetas que no siempre son la que se pidió.

    Además publica un `FillEvent` por cada ejecución confirmada. Eso es lo que
    cierra el bucle `execution -> portfolio`: sin él el portfolio nunca sabe a
    qué precio se llenó de verdad.
    """

    def __init__(self, events_queue, ib, contracts, specs, account=None):
        self.events_queue = events_queue
        self.ib = ib
        self.contracts = contracts
        self.specs = specs
        self.logger = logging.getLogger(__name__)
        from qsCrypto.ibkr import config as ib_config
        self.account = account or ib_config.IB_ACCOUNT
        self.stops = {}          # pair -> Trade del stop vivo
        self.parents = {}        # pair -> Trade del padre
        self._seen_execs = set()
        self._pending_add = {}   # orderId -> bool(is_add)
        self.ib.execDetailsEvent += self._on_exec_details

    # ── utilidades de contrato ──────────────────────────────────────────────
    def round_tick(self, pair, px):
        """Al múltiplo exacto de minTick.

        El segundo redondeo no es cosmético: con minTick 5e-05,
        `round(px/tick)*tick` devuelve 1.0862500000000001 y TWS rechaza la orden
        por precio inválido.
        """
        tick = float(self.specs.get(pair, {}).get("minTick", 0.0)) or 0.0
        if tick <= 0:
            return float(px)
        from decimal import Decimal as _D
        dec = max(0, -_D(str(tick)).as_tuple().exponent)
        return round(round(float(px) / tick) * tick, dec)

    # ── contrato del motor ──────────────────────────────────────────────────
    def execute_order(self, event):
        from ib_async import MarketOrder, StopOrder

        pair = event.instrument
        contract = self.contracts[pair]
        qty = int(abs(event.units))
        if qty <= 0:
            self.logger.warning("Orden de %s con qty=0: no se envía.", pair)
            return None
        action = "BUY" if event.side == "buy" else "SELL"
        is_add = bool(getattr(event, "is_add", False))
        stop_px = getattr(event, "stop_price", None)

        # `tif` nunca queda en blanco: ib_async lo inicializa a `""`, y TWS le
        # aplica su preset de cuenta (visto en vivo: "Order TIF was set to
        # DAY based on order preset") — igual al padre que al stop, porque
        # nada aquí los distingue. Con un futuro CME que opera casi 24h, un
        # STOP en DAY puede expirar en el corte de sesión y dejar la
        # posición sin barrera de bróker justo cuando más importa (el motor
        # puede estar caído, ver streaming.py/trading.py). GTC explícito en
        # ambos evita depender de qué preset tenga la cuenta en TWS.
        parent = MarketOrder(action, qty)
        parent.orderId = self.ib.client.getReqId()
        parent.account = self.account
        parent.transmit = stop_px is None
        parent.outsideRth = True
        parent.tif = "GTC"

        trades = [self.ib.placeOrder(contract, parent)]
        self._pending_add[parent.orderId] = is_add

        if stop_px is not None:
            stop_px = self.round_tick(pair, stop_px)
            stop = StopOrder("SELL" if action == "BUY" else "BUY", qty, stop_px)
            stop.orderId = self.ib.client.getReqId()
            stop.account = self.account
            stop.parentId = parent.orderId
            stop.transmit = True
            stop.outsideRth = True
            stop.tif = "GTC"
            trades.append(self.ib.placeOrder(contract, stop))
            self.stops[pair] = trades[-1]
            self.logger.info("%s %s x%d a mercado, stop adjunto en %s",
                             action, pair, qty, stop_px)
        else:
            self.logger.info("%s %s x%d a mercado, sin stop adjunto",
                             action, pair, qty)
        self.parents[pair] = trades[0]
        return trades[0]

    def modify_stop(self, pair, new_stop, total_qty):
        """Reancla el stop vivo tras un add: precio Y cantidad.

        Mover solo el precio dejaría las unidades nuevas sin cubrir; mover solo
        la cantidad dejaría el stop donde la escalera ya no está.
        """
        trade = self.stops.get(pair)
        if trade is None:
            self.logger.warning("Sin stop vivo para %s: nada que reanclar.", pair)
            return None
        px = self.round_tick(pair, new_stop)
        trade.order.auxPrice = px
        trade.order.totalQuantity = int(total_qty)
        self.ib.placeOrder(self.contracts[pair], trade.order)
        self.logger.info("Stop de %s reanclado a %s por %d", pair, px, total_qty)
        return trade

    def flatten(self, pair, reason="manual"):
        """Cancela lo vivo y aplana. Cancelar ANTES de aplanar.

        Al revés, el stop adjunto seguiría vivo tras cerrar y quedaría como una
        orden huérfana capaz de abrir posición contraria.
        """
        from ib_async import MarketOrder
        contract = self.contracts[pair]
        for o in self.ib.reqOpenOrders():
            if o.contract.conId == contract.conId:
                self.ib.cancelOrder(o.order)
        self.ib.sleep(1.0)
        self.stops.pop(pair, None)

        self.ib.reqPositions()
        self.ib.sleep(0.5)
        flat = 0
        for p in self.ib.positions():
            if p.contract.conId == contract.conId and abs(p.position) > 0:
                side = "SELL" if p.position > 0 else "BUY"
                o = MarketOrder(side, abs(int(p.position)))
                # A la cuenta donde la posición ESTÁ, no a la configurada: una
                # orden de cierre en la cuenta equivocada no cierra nada.
                o.account = p.account
                self.ib.placeOrder(contract, o)
                flat += int(p.position)
                self.logger.info("Aplanando %+d de %s en %s (%s)",
                                 p.position, pair, p.account, reason)
        return flat

    # ── fills de vuelta al bus ──────────────────────────────────────────────
    def _on_exec_details(self, trade, fill):
        try:
            execution = fill.execution
            if execution.execId in self._seen_execs:
                return
            self._seen_execs.add(execution.execId)
            pair = None
            for p, c in self.contracts.items():
                if c.conId == fill.contract.conId:
                    pair = p
                    break
            if pair is None:
                return
            comm = 0.0
            if getattr(fill, "commissionReport", None) is not None:
                comm = float(getattr(fill.commissionReport, "commission", 0.0) or 0.0)
            self.events_queue.put(FillEvent(
                instrument=pair,
                units=int(execution.shares),
                side="buy" if execution.side == "BOT" else "sell",
                fill_price=float(execution.price),
                commission=comm,
                time=str(execution.time),
                account=execution.acctNumber,
                order_id=execution.orderId,
                is_add=self._pending_add.get(execution.orderId, False),
                exec_id=execution.execId,
            ))
            self.logger.info("FILL %s %s %d @ %s en %s", pair, execution.side,
                             execution.shares, execution.price,
                             execution.acctNumber)
        except Exception as exc:
            self.logger.error("Error procesando execDetails: %s", exc)
