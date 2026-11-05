from __future__ import print_function

from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext, ROUND_HALF_DOWN
import json
import logging
import threading
import time as _time

import websocket

from qsCrypto.event.event import BarEvent, TickEvent
from qsCrypto.data.price import PriceHandler
from qsCrypto import settings


class StreamingCryptoPrices(PriceHandler):
    """
    Connects to Binance WebSocket book-ticker streams and
    places TickEvents onto the events queue.
    """

    def __init__(self, pairs, events_queue, test_mode=True, market_type="usdm_futures"):
        self.pairs = pairs
        self.events_queue = events_queue
        self.test_mode = test_mode
        self.market_type = market_type
        self.prices = self._set_up_prices_dict()
        self.logger = logging.getLogger(__name__)

        env_label = "testnet" if test_mode else "production"
        self.ws_base_url = settings.BINANCE_ENVIRONMENTS[market_type]["streaming"][env_label]

    def _set_up_prices_dict(self):
        """
        For crypto we don't need inverse pairs like forex.
        Just set up one entry per pair.
        """
        return {
            p: {"bid": None, "ask": None, "time": None}
            for p in self.pairs
        }

    def _build_ws_url(self):
        """
        Build a combined stream URL like:
        wss://stream.binancefuture.com/stream?streams=btcusdt@bookTicker/ethusdt@bookTicker
        """
        streams = "/".join(
            f"{pair.lower()}@bookTicker" for pair in self.pairs
        )
        return f"{self.ws_base_url}/stream?streams={streams}"

    def _on_message(self, ws, message):
        try:
            msg = json.loads(message)
            data = msg.get("data", msg)

            symbol = data.get("s", "").upper()
            if symbol not in self.prices:
                return

            getcontext().rounding = ROUND_HALF_DOWN
            bid = Decimal(str(data["b"])).quantize(Decimal("0.00001"))
            ask = Decimal(str(data["a"])).quantize(Decimal("0.00001"))
            time_val = str(data.get("T", data.get("E", "")))

            self.prices[symbol]["bid"] = bid
            self.prices[symbol]["ask"] = ask
            self.prices[symbol]["time"] = time_val

            tev = TickEvent(symbol, time_val, bid, ask)
            self.events_queue.put(tev)

        except Exception as e:
            self.logger.error("Error processing WS message: %s", e)

    def _on_error(self, ws, error):
        self.logger.error("WebSocket error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg):
        self.logger.info("WebSocket closed: %s %s", close_status_code, close_msg)

    def _on_open(self, ws):
        self.logger.info("WebSocket connection opened")

    def stream_to_queue(self):
        """
        Connect to the Binance WebSocket and start streaming.
        Blocks the calling thread.
        """
        url = self._build_ws_url()
        self.logger.info("Connecting to WebSocket: %s", url)
        self.ws = websocket.WebSocketApp(
            url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )
        self.ws.run_forever()

    def stop(self):
        if hasattr(self, "ws") and self.ws:
            self.ws.close()

class StreamingBitgetPrices(PriceHandler):
    """
    Connects to the Bitget public WebSocket (v2) and subscribes to
    the ``ticker`` channel for each requested pair.  Delivers
    TickEvents with the same interface as StreamingCryptoPrices so
    it can be used as a drop-in replacement inside the trading loop.

    Parameters
    ----------
    pairs : list[str]
        Instrument IDs as used on Bitget, e.g. ["BTCUSDT", "ETHUSDT"].
    events_queue : queue.Queue
        Shared events queue.
    inst_type : str
        Bitget product-line type.  One of:
        "USDT-FUTURES", "COIN-FUTURES", "USDC-FUTURES", "SPOT".
        Defaults to "USDT-FUTURES".
    ws_url : str | None
        Override the public WebSocket endpoint (useful for testing).
    ping_interval : int
        Seconds between client-side "ping" keep-alives (default 25).
    """

    WS_PUBLIC_URL = "wss://ws.bitget.com/v2/ws/public"

    def __init__(
        self,
        pairs,
        events_queue,
        inst_type="USDT-FUTURES",
        ws_url=None,
        ping_interval=25,
    ):
        self.pairs = pairs
        self.events_queue = events_queue
        self.inst_type = inst_type
        self.ws_url = ws_url or self.WS_PUBLIC_URL
        self.ping_interval = ping_interval
        self.prices = self._set_up_prices_dict()
        self.logger = logging.getLogger(__name__)
        self.ws = None
        self._ping_thread = None
        self._stop_ping = threading.Event()

    # ── prices dict (same contract as StreamingCryptoPrices) ──────

    def _set_up_prices_dict(self):
        """One entry per pair — no inverse pairs needed for crypto."""
        return {
            p: {"bid": None, "ask": None, "time": None}
            for p in self.pairs
        }

    # ── subscribe message ─────────────────────────────────────────

    def _build_subscribe_message(self):
        """
        Build the Bitget ``subscribe`` op message for the ticker
        channel of every configured pair.
        """
        args = [
            {
                "instType": self.inst_type,
                "channel": "ticker",
                "instId": pair,
            }
            for pair in self.pairs
        ]
        return json.dumps({"op": "subscribe", "args": args})

    # ── keep-alive ping ───────────────────────────────────────────

    def _ping_loop(self, ws):
        """
        Bitget requires a string ``"ping"`` every ≤30 s to keep the
        connection alive; it replies with ``"pong"``.
        """
        while not self._stop_ping.is_set():
            try:
                ws.send("ping")
            except Exception:
                break
            self._stop_ping.wait(self.ping_interval)

    # ── WebSocket callbacks ───────────────────────────────────────

    def _on_open(self, ws):
        self.logger.info("Bitget WebSocket opened — subscribing to ticker")
        ws.send(self._build_subscribe_message())
        # Start the keep-alive ping thread
        self._stop_ping.clear()
        self._ping_thread = threading.Thread(
            target=self._ping_loop, args=(ws,), daemon=True
        )
        self._ping_thread.start()

    def _on_message(self, ws, message):
        # Bitget pong is a plain string, not JSON
        if message == "pong":
            return

        try:
            msg = json.loads(message)
        except json.JSONDecodeError as e:
            self.logger.error("Bitget WS JSON decode error: %s", e)
            return

        # Subscription confirmation / error
        event = msg.get("event")
        if event == "subscribe":
            self.logger.info("Bitget subscribed: %s", msg.get("arg"))
            return
        if event == "error":
            self.logger.error(
                "Bitget WS error code=%s msg=%s",
                msg.get("code"), msg.get("msg"),
            )
            return

        # ── ticker push data ─────────────────────────────────────
        data_list = msg.get("data")
        if not data_list:
            return

        for tick in data_list:
            # instId is always present in the push payload
            symbol = tick.get("instId", tick.get("symbol", "")).upper()
            if symbol not in self.prices:
                continue

            bid_raw = tick.get("bidPr")
            ask_raw = tick.get("askPr")
            if bid_raw is None or ask_raw is None:
                continue

            getcontext().rounding = ROUND_HALF_DOWN
            bid = Decimal(str(bid_raw)).quantize(Decimal("0.00001"))
            ask = Decimal(str(ask_raw)).quantize(Decimal("0.00001"))
            time_val = str(tick.get("ts", ""))

            self.prices[symbol]["bid"] = bid
            self.prices[symbol]["ask"] = ask
            self.prices[symbol]["time"] = time_val

            tev = TickEvent(symbol, time_val, bid, ask)
            self.events_queue.put(tev)

    def _on_error(self, ws, error):
        self.logger.error("Bitget WebSocket error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg):
        self.logger.info(
            "Bitget WebSocket closed: %s %s", close_status_code, close_msg
        )
        self._stop_ping.set()

    # ── public interface (same as StreamingCryptoPrices) ──────────

    def stream_to_queue(self):
        """
        Connect to the Bitget public WebSocket and start streaming.
        Blocks the calling thread — run in a dedicated thread.
        """
        self.logger.info("Connecting to Bitget WebSocket: %s", self.ws_url)
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self.ws.run_forever()

    def stop(self):
        """Cleanly shut down the WebSocket and the ping thread."""
        self._stop_ping.set()
        if self.ws:
            self.ws.close()


class IBKRPriceHandler(PriceHandler):
    """Precios de IBKR sobre el bus de eventos: barras cerradas + ticks.

    Emite DOS tipos de evento y cada uno tiene un destinatario distinto:

    * `BarEvent` a cada cierre de barra — es lo que la estrategia necesita, que
      razona sobre OHLC (canal Donchian, True Range). Solo se emite la barra
      TERMINADA: una barra en curso tiene máximo y mínimo provisionales y
      actuar sobre ella adelanta la decisión a información incompleta.
    * `TickEvent` con bid/ask — es lo que marca la posición y actualiza el
      MAE/MFE. A granularidad de 30 minutos la excursión adversa sería
      prácticamente invisible.

    Mantiene la interfaz de PriceHandler para ser intercambiable dentro del
    bucle de trading, igual que StreamingBitgetPrices.

    Instrumentos (`sec_type`): `FUT` (front month del CME, p. ej. MBT), `CFD`
    (p. ej. IBIT por SMART: se opera el CFD y las barras salen de la acción
    subyacente, porque IBKR no da histórico de CFDs) o `STK`.

    Dos formas de obtener las barras (`bar_mode`):

    * `stream`: suscripción `keepUpToDate`. Depende de que IBKR empuje
      actualizaciones, cosa que sin suscripción de datos en tiempo real puede
      no pasar nunca (medido con IBIT: 0 actualizaciones en 20 s).
    * `poll`: un `reqHistoricalData` normal justo después de cada cierre de
      barra, solo con la sesión abierta. El histórico de IBKR no va diferido
      aunque la cuenta no tenga tiempo real (medido: a las 14:44 UTC ya
      estaba la barra de 14:30), y no hay suscripción de barras que se pueda
      quedar muda, así que tampoco hay nada que resuscribir.
    """

    _MAX_CONSECUTIVE_PUMP_ERRORS = 5
    _STALE_CHECK_EVERY_S = 60      # cadencia de la comprobación de obsolescencia
    _MAX_TICK_STALE_S = 120        # sin un tick en 2 min: la línea está muerta
    # Edad máxima de la última barra publicada, medida desde su INICIO y en
    # múltiplos del tamaño de barra. Con el feed sano llega a 2x por
    # construcción: la barra que empieza en T cierra en T+1x, pero solo se
    # publica cuando aparece la siguiente, y la siguiente a esa en T+2x. El
    # 1x extra cubre el retraso de los datos diferidos (marketDataType=3,
    # ~10-15 min). Con 1.5x el watchdog saltaba 15 min antes de CADA cierre
    # con el feed sano, y al reabrir la suscripción tiraba la barra en camino.
    _MAX_BAR_STALE_FACTOR = 3.0
    _RESUBSCRIBE_COOLDOWN_S = 300  # no reintentar más de 1 vez cada 5 min
    # Con un error de permisos de datos (10089, 10197...) resuscribir no
    # arregla nada: se reintenta con mucha menos frecuencia.
    _ENTITLEMENT_COOLDOWN_S = 1800
    _POLL_DELAY_S = 5              # tras el cierre, margen para que IBKR la tenga
    _POLL_RETRY_S = 15             # si la barra aún no está, reintento...
    _POLL_GIVE_UP_S = 180          # ...hasta 3 min; luego, al siguiente cierre
    _SESSIONS_REFRESH_S = 6 * 3600
    # Errores de IBKR que dicen "no hay datos para ti", no "se cayó la línea".
    _MD_ENTITLEMENT_CODES = {354, 10089, 10090, 10167, 10168, 10186, 10197}
    _BAR_SECS = {"1 secs": 1, "5 secs": 5, "10 secs": 10, "15 secs": 15,
                 "30 secs": 30, "1 min": 60, "5 mins": 300, "15 mins": 900,
                 "30 mins": 1800, "1 hour": 3600, "1 day": 86400}

    def __init__(self, pairs, events_queue, ib=None, exchange="CME",
                 currency="USD", bar_size="30 mins", use_rth=True,
                 what_to_show="TRADES", warmup_bars=120, roll_days=2,
                 market_data_type=None, account=None, sec_type="FUT",
                 bar_mode="stream", tick_source="trade"):
        if bar_mode not in ("stream", "poll"):
            raise ValueError("bar_mode debe ser 'stream' o 'poll'")
        if tick_source not in ("trade", "data"):
            raise ValueError("tick_source debe ser 'trade' o 'data'")
        self.pairs = pairs
        self.events_queue = events_queue
        self.exchange = exchange
        self.currency = currency
        self.sec_type = sec_type.upper()
        self.bar_mode = bar_mode
        self.tick_source = tick_source
        self.bar_size = bar_size
        self.use_rth = use_rth
        self.what_to_show = what_to_show
        self.warmup_bars = warmup_bars
        self.roll_days = roll_days
        self.logger = logging.getLogger(__name__)
        self.prices = {}
        self._set_up_prices_dict()

        self.contracts = {}        # contrato que se OPERA (FUT / CFD / STK)
        self.data_contracts = {}   # contrato de las BARRAS (el subyacente de un CFD)
        self.specs = {}
        self.sessions = {}         # pair -> TradingSessions (UTC)
        self._sessions_at = None
        self.history = {}
        self._bar_subs = {}
        self._tickers = {}
        self._last_bar_time = {}
        self._next_poll_at = {}
        self._poll_deadline = {}
        # Último error de permisos de datos por símbolo: (código, mensaje, cuándo).
        self.md_errors = {}
        self._synthetic_warned = set()
        # Marca de tiempo REAL (datetime, no el string de `self.prices`) del
        # último tick y del último bombeo del loop de ib_async — lo que el
        # monitor necesita para distinguir "el feed está vivo pero tranquilo"
        # de "el hilo se congeló": `self.prices[pair]["time"]` puede quedarse
        # como estaba desde hace horas sin que nada más lo delate.
        self.last_tick_at = {p: None for p in self.pairs}
        self.last_pump_at = None
        self._last_resubscribe_at = None
        self.continue_backtest = True
        self._running = False

        from qsCrypto.ibkr import config as ib_config
        self.config = ib_config
        self.account = account or ib_config.IB_ACCOUNT
        self.market_data_type = (
            ib_config.IB_MARKET_DATA_TYPE if market_data_type is None
            else market_data_type
        )
        self.ib = ib
        if self.ib is not None:
            self.setup()

    # ── conexión y resolución de contratos ──────────────────────────────────
    def connect(self, client_id=None):
        from ib_async import IB
        ib = IB()
        # RequestTimeout viene a 0 y ib_async hace `if timeout:`, o sea CERO
        # SIGNIFICA SIN LÍMITE: una petición que no recibe su mensaje de fin
        # cuelga el proceso para siempre y deja la posición sin vigilancia.
        ib.RequestTimeout = 20
        ib.connect(self.config.IB_HOST, self.config.IB_PORT,
                   clientId=client_id or self.config.IB_CLIENT_ID,
                   timeout=15, readonly=False)
        self.ib = ib
        self.setup()
        return ib

    def setup(self):
        self.ib.reqMarketDataType(self.market_data_type)
        if self.market_data_type != 1:
            self.logger.warning(
                "marketDataType=%s (no es tiempo real): la MAE se mide sobre "
                "precios viejos y queda infravalorada.", self.market_data_type)
        self.ib.errorEvent += self._on_ib_error
        self.resolve_contracts()

    def resolve_contracts(self):
        """Contratos por símbolo, resueltos contra IB, nunca hardcodeados.

        FUT reutiliza `front_month` con el mismo `min_days` que el backtest
        para que vivo y backtest operen exactamente el mismo vencimiento. CFD
        opera el CFD y toma las barras del subyacente (`resolve_instrument`).
        """
        from qsCrypto.ibkr.contracts import resolve_instrument
        from qsCrypto.ibkr.sessions import TradingSessions, utcnow_naive
        for pair in self.pairs:
            contract, detail, data_contract, data_detail, expiry = \
                resolve_instrument(self.ib, pair, self.sec_type, self.exchange,
                                   self.currency, min_days=self.roll_days)
            self.contracts[pair] = contract
            self.data_contracts[pair] = data_contract
            self.specs[pair] = {
                "multiplier": float(contract.multiplier or 1.0),
                "minTick": float(detail.minTick),
                "expiry": expiry,
                "localSymbol": contract.localSymbol,
                "secType": contract.secType,
                "dataSymbol": data_contract.localSymbol,
                # Órdenes fuera de la sesión regular: sí en el futuro y en
                # divisas (CME e IDEALPRO cotizan casi 24 h y el stop debe
                # proteger siempre), NO en CFD/acción: el backtest solo ve
                # barras RTH, y un stop que saltara con un print del
                # pre-market sería una salida que el backtest nunca modeló.
                "outside_rth": contract.secType in ("FUT", "CASH"),
            }
            self.sessions[pair] = TradingSessions.from_details(
                data_detail, use_rth=self.use_rth)
            self.logger.info(
                "%s -> %s (%s) multiplier=%s minTick=%s expiry=%s barras de %s "
                "%s | %r", pair, contract.localSymbol, contract.secType,
                self.specs[pair]["multiplier"], self.specs[pair]["minTick"],
                expiry, data_contract.localSymbol, data_contract.secType,
                self.sessions[pair])
        self._sessions_at = utcnow_naive()

    def _refresh_sessions(self, now):
        """El calendario de IBKR cubre ~una semana: se renueva cada pocas
        horas. Un fallo no es grave — fuera del horizonte conocido el mercado
        se trata como abierto, que es el comportamiento anterior."""
        if self._sessions_at is not None and \
                (now - self._sessions_at).total_seconds() < self._SESSIONS_REFRESH_S:
            return
        from qsCrypto.ibkr.sessions import TradingSessions
        self._sessions_at = now
        for pair in self.pairs:
            try:
                det = self.ib.reqContractDetails(self.data_contracts[pair])
                if det:
                    self.sessions[pair] = TradingSessions.from_details(
                        det[0], use_rth=self.use_rth)
            except Exception as exc:
                self.logger.warning("No se pudo renovar el calendario de %s: %s",
                                    pair, exc)

    def market_open(self, pair, now):
        """True/False según el calendario; True si el calendario no cubre
        `now` (sin información, se vigila como si estuviera abierto)."""
        sess = self.sessions.get(pair)
        if sess is None or not sess.covers(now):
            return True
        return sess.is_open(now)

    def _on_ib_error(self, reqId, errorCode, errorString, contract=None, *args):
        """Guarda los errores de permisos de datos por símbolo: son la causa
        real de un feed mudo y el watchdog los usa para no martillear."""
        if errorCode not in self._MD_ENTITLEMENT_CODES or contract is None:
            return
        pair = self._pair_of_contract(contract)
        if pair is None:
            return
        from qsCrypto.ibkr.sessions import utcnow_naive
        first = pair not in self.md_errors or self.md_errors[pair][0] != errorCode
        self.md_errors[pair] = (errorCode, errorString, utcnow_naive())
        if first:
            self.logger.critical(
                "Sin datos de mercado en vivo para %s (%s): error %s — %s. "
                "Revisa la suscripción de datos para API o cierra la sesión "
                "live competidora (TWS/app/web con el mismo usuario).",
                pair, getattr(contract, "localSymbol", ""), errorCode, errorString)

    def _set_up_prices_dict(self):
        # Sin los pares inversos de forex de la clase base: un futuro no tiene
        # cotización recíproca.
        self.prices = {p: {"bid": None, "ask": None, "time": None}
                       for p in self.pairs}

    # ── calentamiento ───────────────────────────────────────────────────────
    def warmup(self):
        """Descarga el histórico para que N y el canal existan al arrancar.

        Sin esto harían falta `atr_period + entry_bars` barras nuevas antes de
        la primera señal — a 30 minutos, más de una jornada a ciegas.

        La ÚLTIMA barra que devuelve IBKR se descarta: en sesión es la barra
        en curso, con máximo y mínimo provisionales. Arrancando a las 13:55
        UTC llegaba la de 13:30 a medio formar; la estrategia se sembraba con
        ella y, al cerrar, la barra real de 13:30 se descartaba por no ser
        más nueva que `_last_bar_time`: la estrategia no la veía nunca.
        Se usa la misma regla estructural que en el streaming —la última
        barra está en curso hasta que aparece otra detrás— y no el reloj,
        que con datos diferidos daría por cerrada una barra aún incompleta.
        Fuera de sesión la barra descartada sí estaba cerrada; se publica en
        cuanto aparezca la primera de la sesión siguiente, igual que si
        hubiera llegado en vivo.
        """
        import pandas as pd
        from ib_async import util
        for pair in self.pairs:
            bars = self.ib.reqHistoricalData(
                self.data_contracts[pair], endDateTime="",
                durationStr=self._duration_for(self.warmup_bars),
                barSizeSetting=self.bar_size, whatToShow=self.what_to_show,
                useRTH=self.use_rth, formatDate=2, timeout=60)
            if len(bars) < 2:                  # la última se descarta abajo
                raise RuntimeError(
                    "Sin histórico para %s (%s, %s). En divisas whatToShow="
                    "'TRADES' vuelve vacío: usa MIDPOINT." % (
                        pair, self.bar_size, self.what_to_show))
            df = util.df(bars).rename(columns={"date": "time"})
            df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_localize(None)
            df = df.set_index("time").sort_index()
            if self.bar_mode == "poll":
                # Misma regla que el sondeo: fuera de sesión la última barra
                # ya terminó y es historia válida.
                now = pd.Timestamp.now(tz="UTC").tz_localize(None)
                df = df[df.index + pd.Timedelta(seconds=self._bar_secs()) <= now]
            else:
                df = df.iloc[:-1]
            cols = [c for c in ("open", "high", "low", "close", "volume")
                    if c in df.columns]
            self.history[pair] = df[cols].astype(float)
            self._last_bar_time[pair] = self.history[pair].index[-1]
            self.logger.info("Calentamiento %s: %d barras (%s -> %s)", pair,
                             len(self.history[pair]),
                             self.history[pair].index[0],
                             self.history[pair].index[-1])
        return self.history

    def _bar_secs(self):
        return self._BAR_SECS.get(self.bar_size, 1800)

    def _duration_for(self, n_bars):
        secs = self._bar_secs()
        if secs < 60:
            # IBKR limita la duración de peticiones de barras SUB-MINUTO a
            # más o menos 1 día (28.800s para 30 segs, menos para tamaños
            # menores). El cálculo de días de abajo está pensado para
            # barras de minutos+; aplicado aquí pediría "2 D" o más y la
            # petición revienta con un error de duración inválida de IBKR.
            return "1 D"
        days = max(2, int((n_bars * secs) / (6.5 * 3600)) + 2)
        return "%d D" % min(days, 60)

    def get_history(self, pair):
        if pair not in self.history:
            self.warmup()
        return self.history.get(pair)

    # ── streaming ───────────────────────────────────────────────────────────
    def _on_bar_update(self, bars, has_new_bar):
        """Callback de `keepUpToDate`: publica toda barra cerrada pendiente.

        Deliberadamente NO depende de `has_new_bar`: ib_async solo lo marca
        cuando la barra nueva llega como actualización de una suscripción
        ya abierta. Si la suscripción se reabre (watchdog) y la barra nueva
        viene en la carga inicial —lo normal con datos diferidos, que la
        entregan minutos después del cierre—, `has_new_bar` nunca es True
        para ella y se perdía. Comparar contra `_last_bar_time` sobre la
        lista entera es idempotente y no pierde nada.
        """
        pair = getattr(bars, "_qs_pair", None) or self._pair_of(bars)
        if pair is not None:
            self._publish_closed_bars(pair, bars)

    def _publish_closed_bars(self, pair, bars, now=None):
        """Publica, en orden, las barras cerradas posteriores a la última
        publicada. Devuelve cuántas.

        Qué barra está cerrada:
        * modo `stream` (`now=None`): regla estructural, `bars[-1]` es la
          barra en curso hasta que aparece otra detrás;
        * modo `poll` (`now` dado): la que ya terminó (`inicio + tamaño <=
          now`). El histórico que se sondea no va diferido, y así la última
          barra de la sesión se publica al cierre (16:00 ET en IBIT), no a
          la mañana siguiente cuando aparece la primera de la nueva sesión.

        Si hay más de una pendiente (hueco del feed, reapertura de la
        suscripción), solo la más reciente es operable; las anteriores van
        con `backfill=True`: completan N y el canal, pero una señal sobre
        ellas se ejecutaría a precio de ahora con información vieja.

        Sin ticks en vivo para el símbolo, cada barra va precedida de ticks
        sintéticos de su máximo, mínimo y cierre (`_bar_ticks`).
        """
        try:
            import pandas as pd
            bar_td = pd.Timedelta(seconds=self._bar_secs())
            last = self._last_bar_time.get(pair)
            candidates = bars if now is not None else bars[:-1]
            fresh = []
            for b in reversed(candidates):
                ts = pd.Timestamp(b.date)
                if ts.tzinfo is not None:
                    ts = ts.tz_convert("UTC").tz_localize(None)
                if last is not None and ts <= last:
                    break
                if now is not None and ts + bar_td > pd.Timestamp(now):
                    continue                   # aún en curso
                fresh.append((ts, b))
            if not fresh:
                return 0
            fresh.reverse()
            if len(fresh) > 1:
                self.logger.warning(
                    "%s: %d barras cerradas llegan juntas (%s -> %s); solo la "
                    "última es operable, el resto se publica como backfill.",
                    pair, len(fresh), fresh[0][0], fresh[-1][0])
            live_ticks = self._ticks_live(pair)
            for i, (ts, b) in enumerate(fresh):
                vol = float(getattr(b, "volume", 0.0) or 0.0)
                self._last_bar_time[pair] = ts
                self.history[pair].loc[ts] = [
                    float(b.open), float(b.high), float(b.low), float(b.close),
                    vol,
                ][:len(self.history[pair].columns)]
                if not live_ticks:
                    self._bar_ticks(pair, ts + bar_td, b)
                self.events_queue.put(BarEvent(
                    pair, ts, float(b.open), float(b.high), float(b.low),
                    float(b.close), vol, complete=True,
                    backfill=i < len(fresh) - 1))
            return len(fresh)
        except Exception as exc:
            self.logger.error("Error procesando barra: %s", exc)
            return 0

    def _ticks_live(self, pair):
        tick_at = self.last_tick_at.get(pair)
        return tick_at is not None and (
            datetime.now(timezone.utc) - tick_at).total_seconds() <= self._MAX_TICK_STALE_S

    def _bar_ticks(self, pair, bar_end, b):
        """Sin ticks en vivo (sin suscripción de datos, o sesión live
        competidora: IBKR 10089/10197), el portfolio se quedaría sin precio:
        `_ref_price` devuelve None y NINGUNA señal se opera, y el MAE nunca se
        actualiza. Se marca al máximo, al mínimo y al cierre de la barra, en
        ese orden — el precio vigente queda en el cierre y la excursión de
        las posiciones abiertas recoge los dos extremos. Van ANTES del
        BarEvent: una entrada que ese BarEvent dispare no debe ver los
        extremos de una barra anterior a su fill. Es MAE a resolución de
        barra, no de tick: queda dicho en el log una vez por símbolo.
        """
        if pair not in self._synthetic_warned:
            self._synthetic_warned.add(pair)
            self.logger.warning(
                "%s: sin ticks en vivo. Precio de referencia y MAE/MFE a "
                "resolución de BARRA (máximo, mínimo, cierre) hasta que "
                "vuelvan los ticks.", pair)
        tval = bar_end.isoformat()
        for px in (b.high, b.low, b.close):
            d = Decimal(str(float(px))).quantize(Decimal("0.00001"))
            self.prices[pair]["bid"] = d
            self.prices[pair]["ask"] = d
            self.prices[pair]["time"] = tval
            self.events_queue.put(TickEvent(pair, tval, d, d))

    def _pair_of(self, bars):
        for pair, sub in self._bar_subs.items():
            if sub is bars:
                return pair
        return None

    def _on_pending_tickers(self, tickers):
        getcontext().rounding = ROUND_HALF_DOWN
        for tk in tickers:
            try:
                pair = self._pair_of_contract(tk.contract)
                if pair is None:
                    continue
                bid, ask = tk.bid, tk.ask
                # Guarda de NaN: con datos retrasados IBKR devuelve nan y
                # Decimal(str(nan)) produce Decimal('NaN'), que envenena
                # Position y que los `is None` del Portfolio no atrapan.
                if bid is None or ask is None or bid != bid or ask != ask \
                        or bid <= 0 or ask <= 0:
                    continue
                bid_d = Decimal(str(bid)).quantize(Decimal("0.00001"))
                ask_d = Decimal(str(ask)).quantize(Decimal("0.00001"))
                tval = str(tk.time) if tk.time else ""
                # Primero el diccionario, después la cola: cuando el consumidor
                # saque el TickEvent, `prices` ya está al día. Es el mismo
                # invariante que respeta StreamingCryptoPrices._on_message.
                self.prices[pair]["bid"] = bid_d
                self.prices[pair]["ask"] = ask_d
                self.prices[pair]["time"] = tval
                if self.last_tick_at.get(pair) is None:
                    self.logger.info("%s: primer tick en vivo (marketDataType=%s).",
                                     pair, getattr(tk, "marketDataType", "?"))
                self.last_tick_at[pair] = datetime.now(timezone.utc)
                self.md_errors.pop(pair, None)
                self.events_queue.put(TickEvent(pair, tval, bid_d, ask_d))
            except Exception as exc:
                self.logger.error("Error procesando ticker: %s", exc)

    def _pair_of_contract(self, contract):
        for pair in self.pairs:
            for c in (self.contracts.get(pair), self.data_contracts.get(pair)):
                if c is not None and c.conId == contract.conId:
                    return pair
        return None

    def _tick_contract(self, pair):
        return (self.contracts[pair] if self.tick_source == "trade"
                else self.data_contracts[pair])

    def subscribe(self):
        for pair in self.pairs:
            self._tickers[pair] = self.ib.reqMktData(
                self._tick_contract(pair), "", False, False)
            if self.bar_mode != "stream":
                continue                       # en `poll` no hay suscripción de barras
            bars = self.ib.reqHistoricalData(
                self.data_contracts[pair], endDateTime="",
                durationStr=self._duration_for(50),
                barSizeSetting=self.bar_size, whatToShow=self.what_to_show,
                useRTH=self.use_rth, formatDate=2, keepUpToDate=True, timeout=60)
            bars._qs_pair = pair
            self._bar_subs[pair] = bars
            # La carga inicial no dispara `updateEvent`: lo que haya cerrado
            # mientras la suscripción anterior estaba cancelada (o entre el
            # calentamiento y este punto) se publica aquí o no se publica.
            self._publish_closed_bars(pair, bars)
            bars.updateEvent += self._on_bar_update
        self.ib.pendingTickersEvent += self._on_pending_tickers

    def _cancel_tickers(self):
        for pair, tk in list(self._tickers.items()):
            try:
                self.ib.cancelMktData(tk.contract)
            except Exception:
                pass
        self._tickers.clear()

    def _unsubscribe_quietly(self):
        """Cancela lo que haya, tragándose los fallos: es limpieza antes de
        volver a suscribirse, no el cierre final (`stop()` hace eso y sí
        registra sus fallos)."""
        try:
            self.ib.pendingTickersEvent -= self._on_pending_tickers
        except Exception:
            pass
        for pair, bars in list(self._bar_subs.items()):
            try:
                self.ib.cancelHistoricalData(bars)
            except Exception:
                pass
        self._cancel_tickers()
        self._bar_subs.clear()

    def _resubscribe(self):
        self._unsubscribe_quietly()
        self.subscribe()

    def _resubscribe_ticks(self):
        """En modo `poll` la única suscripción viva es la de ticks: se
        reabre solo esa, las barras no se tocan."""
        self._cancel_tickers()
        for pair in self.pairs:
            self._tickers[pair] = self.ib.reqMktData(
                self._tick_contract(pair), "", False, False)

    def _check_staleness(self, now):
        """Detecta un feed muerto SIN excepción — el caso que el reintento
        de `stream_to_queue` no cubre. Un farm de datos de IBKR puede
        dejar de entregar ticks/barras sin que `ib.sleep()` lance nada: el
        bombeo sigue "sano" (`last_pump_at` se actualiza cada segundo) pero
        no llega un solo dato nuevo.

        Solo se vigila con el mercado ABIERTO según el calendario del
        contrato: antes, con la sesión cerrada (noche, fin de semana,
        mantenimiento del CME) cada comprobación declaraba el feed muerto y
        reabría suscripciones cada 5 minutos hasta la apertura. Las edades se
        miden desde la apertura de la sesión si esta es más reciente que el
        último dato.

        * ticks: más de `_MAX_TICK_STALE_S` sin uno;
        * barras (solo modo `stream`): la última publicada empezó hace más de
          `_MAX_BAR_STALE_FACTOR` barras. En modo `poll` no hay suscripción de
          barras que pueda quedarse muda: el sondeo reintenta por su cuenta.

        Si la causa conocida es un error de permisos de datos (10089, 10197…)
        se reintenta cada `_ENTITLEMENT_COOLDOWN_S`, no cada 5 minutos:
        resuscribir no concede una suscripción que la cuenta no tiene.
        """
        now_n = now.replace(tzinfo=None)
        self._refresh_sessions(now_n)
        bar_secs = self._bar_secs()
        stale_pairs = []
        for pair in self.pairs:
            if not self.market_open(pair, now_n):
                continue
            sess = self.sessions.get(pair)
            cur = sess.current(now_n) if sess is not None and sess.covers(now_n) else None
            opened = cur[0] if cur is not None else None

            tick_at = self.last_tick_at.get(pair)
            tick_ref = tick_at.replace(tzinfo=None) if tick_at is not None else None
            if opened is not None:
                tick_ref = opened if tick_ref is None else max(tick_ref, opened)
            tick_stale = (tick_ref is None or
                          (now_n - tick_ref).total_seconds() > self._MAX_TICK_STALE_S)

            bar_stale = False
            last_bar_ts = self._last_bar_time.get(pair)
            if self.bar_mode == "stream" and last_bar_ts is not None:
                bar_ref = last_bar_ts
                if opened is not None and bar_ref < opened:
                    bar_ref = opened - timedelta(seconds=bar_secs)
                bar_stale = ((now_n - bar_ref).total_seconds()
                             > bar_secs * self._MAX_BAR_STALE_FACTOR)
            if tick_stale or bar_stale:
                stale_pairs.append(pair)
        if not stale_pairs:
            return
        known = all(p in self.md_errors for p in stale_pairs)
        cooldown = self._ENTITLEMENT_COOLDOWN_S if known else self._RESUBSCRIBE_COOLDOWN_S
        if (self._last_resubscribe_at is not None and
                (now - self._last_resubscribe_at).total_seconds() < cooldown):
            return
        if known:
            self.logger.warning(
                "Sin ticks en %s por permisos de datos (%s): se reintenta cada "
                "%d min.", stale_pairs,
                {p: self.md_errors[p][0] for p in stale_pairs}, cooldown // 60)
        else:
            self.logger.critical(
                "Feed obsoleto sin excepción en %s con el mercado abierto "
                "(posible farm de IBKR caído en silencio): reabriendo "
                "suscripciones.", stale_pairs)
        self._last_resubscribe_at = now
        try:
            if self.bar_mode == "poll":
                self._resubscribe_ticks()
            else:
                self._resubscribe()
        except Exception as exc:
            self.logger.critical("Reabrir suscripciones también falló: %s", exc)

    # ── sondeo de barras (bar_mode="poll") ──────────────────────────────────
    def _poll_due(self, pair, now):
        """Próximo sondeo tras `now`: el siguiente cierre de barra CON LA
        SESIÓN ABIERTA, más `_POLL_DELAY_S`. Si la sesión termina antes del
        siguiente corte (cierre anticipado), el cierre de sesión manda."""
        from qsCrypto.ibkr.sessions import floor_to
        step = timedelta(seconds=self._bar_secs())
        boundary = floor_to(now, self._bar_secs()) + step
        sess = self.sessions.get(pair)
        if sess is not None and sess.covers(now):
            cur = sess.current(now)
            if cur is not None:
                boundary = min(boundary, cur[1])
            else:
                nxt = sess.next_open(now)
                if nxt is not None:
                    boundary = floor_to(nxt, self._bar_secs()) + step
        return boundary + timedelta(seconds=self._POLL_DELAY_S)

    def _poll_once(self, pair, now):
        last = self._last_bar_time.get(pair)
        days = 2 if last is None else max(2, (now - last).days + 2)
        bars = self.ib.reqHistoricalData(
            self.data_contracts[pair], endDateTime="",
            durationStr="%d D" % min(days, 30),
            barSizeSetting=self.bar_size, whatToShow=self.what_to_show,
            useRTH=self.use_rth, formatDate=2, timeout=30)
        return self._publish_closed_bars(pair, list(bars or []), now=now)

    def _poll_step(self, now):
        """Sondea los símbolos a los que les toca. La barra esperada es la
        que termina en el corte programado; si IBKR aún no la tiene se
        reintenta cada `_POLL_RETRY_S` hasta `_POLL_GIVE_UP_S`, y si no
        aparece se pasa al siguiente corte (que la recogerá como backfill)."""
        from qsCrypto.ibkr.sessions import floor_to
        for pair in self.pairs:
            due = self._next_poll_at.get(pair)
            if due is None:
                self._next_poll_at[pair] = self._poll_due(pair, now)
                continue
            if now < due:
                continue
            boundary = self._poll_deadline.get(pair, (None, None))[0] or \
                due - timedelta(seconds=self._POLL_DELAY_S)
            expected = floor_to(boundary - timedelta(seconds=1), self._bar_secs())
            try:
                self._poll_once(pair, now)
            except Exception as exc:
                self.logger.error("Sondeo de barras de %s falló: %s", pair, exc)
            last = self._last_bar_time.get(pair)
            if last is not None and last >= expected:
                self._poll_deadline.pop(pair, None)
                self._next_poll_at[pair] = self._poll_due(pair, now)
                continue
            b0, give_up = self._poll_deadline.setdefault(
                pair, (boundary, boundary + timedelta(seconds=self._POLL_GIVE_UP_S)))
            if now >= give_up:
                self.logger.warning(
                    "%s: la barra de %s no apareció en %d s de sondeo; se "
                    "recogerá en el próximo corte.", pair, expected,
                    self._POLL_GIVE_UP_S)
                self._poll_deadline.pop(pair, None)
                self._next_poll_at[pair] = self._poll_due(pair, now)
            else:
                self._next_poll_at[pair] = now + timedelta(seconds=self._POLL_RETRY_S)

    def stream_to_queue(self):
        """Bombea el loop de ib_async. Es el ÚNICO hilo que lo hace.

        `ib.sleep(1)` es lo que entrega los callbacks de ticks/barras: si
        se bloquea o lanza (hipo de red, farm de datos reiniciando) y la
        excepción escapa sin más, este `while` se para y el hilo muere en
        silencio — `thread.is_alive()` lo delata, pero nada reintenta nada.
        Un fallo aislado se registra y se sigue; una racha de fallos
        (`_MAX_CONSECUTIVE_PUMP_ERRORS` seguidos) intenta resuscribirse antes
        de rendirse, porque un `reqMktData`/`reqHistoricalData` cuya
        suscripción se cayó en el bróker no se repara solo con reintentar el
        `sleep`. Eso cubre el bombeo roto; `_check_staleness` cubre el otro
        caso, más traicionero: el bombeo sigue sano pero la suscripción dejó
        de entregar datos sin lanzar nada. En modo `poll`, este mismo hilo
        sondea las barras en cada cierre (`_poll_step`).
        """
        if not self.history:
            self.warmup()
        self.subscribe()
        self._running = True
        self.last_pump_at = datetime.now(timezone.utc)
        self._last_resubscribe_at = None
        last_stale_check = self.last_pump_at
        self.logger.info("Streaming IBKR arrancado sobre %s (barras: %s, ticks: %s)",
                         self.pairs, self.bar_mode, self.tick_source)
        consecutive_errors = 0
        while self._running:
            try:
                self.ib.sleep(1)
                self.last_pump_at = datetime.now(timezone.utc)
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                self.logger.error(
                    "Error bombeando streaming (%d seguidos): %s",
                    consecutive_errors, exc)
                if consecutive_errors >= self._MAX_CONSECUTIVE_PUMP_ERRORS:
                    self.logger.critical(
                        "%d fallos seguidos bombeando streaming: "
                        "reintentando suscripción.", consecutive_errors)
                    try:
                        self.subscribe()
                        consecutive_errors = 0
                    except Exception as sub_exc:
                        self.logger.critical(
                            "Resuscripción también falló: %s. El hilo sigue "
                            "vivo pero el feed puede seguir muerto.", sub_exc)
                _time.sleep(1)
                continue

            now = self.last_pump_at
            if self.bar_mode == "poll":
                try:
                    self._poll_step(now.replace(tzinfo=None))
                except Exception as exc:
                    self.logger.error("Error en el sondeo de barras: %s", exc)
            if (now - last_stale_check).total_seconds() >= self._STALE_CHECK_EVERY_S:
                last_stale_check = now
                self._check_staleness(now)

    def stop(self):
        self._running = False
        try:
            self.ib.pendingTickersEvent -= self._on_pending_tickers
            for pair, bars in self._bar_subs.items():
                self.ib.cancelHistoricalData(bars)
            for pair, tk in list(self._tickers.items()):
                self.ib.cancelMktData(tk.contract)
        except Exception as exc:
            self.logger.error("Error cerrando suscripciones: %s", exc)
