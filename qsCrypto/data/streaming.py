from __future__ import print_function

from datetime import datetime, timezone
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
    """

    _MAX_CONSECUTIVE_PUMP_ERRORS = 5
    _STALE_CHECK_EVERY_S = 60      # cadencia de la comprobación de obsolescencia
    _MAX_TICK_STALE_S = 120        # sin un tick en 2 min: la línea está muerta
    _MAX_BAR_STALE_FACTOR = 1.5    # 1.5x el tamaño de barra sin barra nueva
    _RESUBSCRIBE_COOLDOWN_S = 300  # no reintentar más de 1 vez cada 5 min

    def __init__(self, pairs, events_queue, ib=None, exchange="CME",
                 currency="USD", bar_size="30 mins", use_rth=True,
                 what_to_show="TRADES", warmup_bars=120, roll_days=2,
                 market_data_type=None, account=None):
        self.pairs = pairs
        self.events_queue = events_queue
        self.exchange = exchange
        self.currency = currency
        self.bar_size = bar_size
        self.use_rth = use_rth
        self.what_to_show = what_to_show
        self.warmup_bars = warmup_bars
        self.roll_days = roll_days
        self.logger = logging.getLogger(__name__)
        self.prices = {}
        self._set_up_prices_dict()

        self.contracts = {}
        self.specs = {}
        self.history = {}
        self._bar_subs = {}
        self._tickers = {}
        self._last_bar_time = {}
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
        self.resolve_contracts()

    def resolve_contracts(self):
        """Front month por símbolo, resuelto contra IB, nunca hardcodeado.

        Reutiliza `front_month` con el mismo `min_days` que el backtest para que
        vivo y backtest operen exactamente el mismo vencimiento.
        """
        from qsCrypto.ibkr.contracts import front_month
        for pair in self.pairs:
            contract, detail, expiry = front_month(
                self.ib, pair, self.exchange, self.currency,
                min_days=self.roll_days)
            self.contracts[pair] = contract
            self.specs[pair] = {
                "multiplier": float(contract.multiplier or 1.0),
                "minTick": float(detail.minTick),
                "expiry": expiry,
                "localSymbol": contract.localSymbol,
            }
            self.logger.info("%s -> %s multiplier=%s minTick=%s expiry=%s",
                             pair, contract.localSymbol,
                             self.specs[pair]["multiplier"],
                             self.specs[pair]["minTick"], expiry)

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
        """
        import pandas as pd
        from ib_async import util
        for pair in self.pairs:
            bars = self.ib.reqHistoricalData(
                self.contracts[pair], endDateTime="",
                durationStr=self._duration_for(self.warmup_bars),
                barSizeSetting=self.bar_size, whatToShow=self.what_to_show,
                useRTH=self.use_rth, formatDate=2, timeout=60)
            if not bars:
                raise RuntimeError(
                    "Sin histórico para %s (%s, %s). En divisas whatToShow="
                    "'TRADES' vuelve vacío: usa MIDPOINT." % (
                        pair, self.bar_size, self.what_to_show))
            df = util.df(bars).rename(columns={"date": "time"})
            df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_localize(None)
            df = df.set_index("time").sort_index()
            cols = [c for c in ("open", "high", "low", "close", "volume")
                    if c in df.columns]
            self.history[pair] = df[cols].astype(float)
            self._last_bar_time[pair] = self.history[pair].index[-1]
            self.logger.info("Calentamiento %s: %d barras (%s -> %s)", pair,
                             len(self.history[pair]),
                             self.history[pair].index[0],
                             self.history[pair].index[-1])
        return self.history

    def _duration_for(self, n_bars):
        secs = {"1 secs": 1, "5 secs": 5, "10 secs": 10, "15 secs": 15,
                "30 secs": 30, "1 min": 60, "5 mins": 300, "15 mins": 900,
                "30 mins": 1800, "1 hour": 3600, "1 day": 86400
                }.get(self.bar_size, 1800)
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
        """Solo publica la barra ANTERIOR a la que está en curso."""
        if not has_new_bar or len(bars) < 2:
            return
        try:
            import pandas as pd
            pair = getattr(bars, "_qs_pair", None) or self._pair_of(bars)
            if pair is None:
                return
            b = bars[-2]                       # -1 es la barra viva
            ts = pd.Timestamp(b.date)
            if ts.tzinfo is not None:
                ts = ts.tz_convert("UTC").tz_localize(None)
            if self._last_bar_time.get(pair) is not None and \
                    ts <= self._last_bar_time[pair]:
                return
            self._last_bar_time[pair] = ts
            self.history[pair].loc[ts] = [
                float(b.open), float(b.high), float(b.low), float(b.close),
                float(getattr(b, "volume", 0.0) or 0.0),
            ][:len(self.history[pair].columns)]
            self.events_queue.put(BarEvent(
                pair, ts, float(b.open), float(b.high), float(b.low),
                float(b.close), float(getattr(b, "volume", 0.0) or 0.0),
                complete=True))
        except Exception as exc:
            self.logger.error("Error procesando barra: %s", exc)

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
                self.last_tick_at[pair] = datetime.now(timezone.utc)
                self.events_queue.put(TickEvent(pair, tval, bid_d, ask_d))
            except Exception as exc:
                self.logger.error("Error procesando ticker: %s", exc)

    def _pair_of_contract(self, contract):
        for pair, c in self.contracts.items():
            if c.conId == contract.conId:
                return pair
        return None

    def subscribe(self):
        for pair in self.pairs:
            tk = self.ib.reqMktData(self.contracts[pair], "", False, False)
            self._tickers[pair] = tk
            bars = self.ib.reqHistoricalData(
                self.contracts[pair], endDateTime="",
                durationStr=self._duration_for(50),
                barSizeSetting=self.bar_size, whatToShow=self.what_to_show,
                useRTH=self.use_rth, formatDate=2, keepUpToDate=True, timeout=60)
            bars._qs_pair = pair
            self._bar_subs[pair] = bars
            bars.updateEvent += self._on_bar_update
        self.ib.pendingTickersEvent += self._on_pending_tickers

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
        for pair in list(self._tickers):
            try:
                self.ib.cancelMktData(self.contracts[pair])
            except Exception:
                pass
        self._bar_subs.clear()
        self._tickers.clear()

    def _resubscribe(self):
        self._unsubscribe_quietly()
        self.subscribe()

    def _check_staleness(self, now):
        """Detecta un feed muerto SIN excepción — el caso que el reintento
        de `stream_to_queue` no cubre. Un farm de datos de IBKR puede
        dejar de entregar ticks/barras sin que `ib.sleep()` lance nada: el
        bombeo sigue "sano" (`last_pump_at` se actualiza cada segundo) pero
        no llega un solo dato nuevo. Se comprueba cada
        `_STALE_CHECK_EVERY_S` segundos y, si hace más de
        `_MAX_TICK_STALE_S` que no llega un tick o más de
        `_MAX_BAR_STALE_FACTOR` veces el tamaño de barra que no cierra una,
        se fuerza una resuscripción — con un enfriamiento mínimo entre
        intentos para no entrar en una tormenta de reqMktData si el mercado
        de verdad está cerrado (mantenimiento diario del CME, fin de
        semana).
        """
        bar_secs = {"1 min": 60, "5 mins": 300, "15 mins": 900,
                    "30 mins": 1800, "1 hour": 3600, "1 day": 86400
                    }.get(self.bar_size, 1800)
        stale_pairs = []
        for pair in self.pairs:
            tick_at = self.last_tick_at.get(pair)
            tick_stale = (tick_at is None or
                         (now - tick_at).total_seconds() > self._MAX_TICK_STALE_S)
            last_bar_ts = self._last_bar_time.get(pair)
            bar_stale = (last_bar_ts is not None and
                        (now.replace(tzinfo=None) - last_bar_ts).total_seconds()
                        > bar_secs * self._MAX_BAR_STALE_FACTOR)
            if tick_stale or bar_stale:
                stale_pairs.append(pair)
        if not stale_pairs:
            return
        if (self._last_resubscribe_at is not None and
                (now - self._last_resubscribe_at).total_seconds()
                < self._RESUBSCRIBE_COOLDOWN_S):
            return
        self.logger.critical(
            "Feed obsoleto sin excepción en %s (posible farm de IBKR caído "
            "en silencio): reabriendo suscripciones.", stale_pairs)
        self._last_resubscribe_at = now
        try:
            self._resubscribe()
        except Exception as exc:
            self.logger.critical("Reabrir suscripciones también falló: %s", exc)

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
        de entregar datos sin lanzar nada.
        """
        if not self.history:
            self.warmup()
        self.subscribe()
        self._running = True
        self.last_pump_at = datetime.now(timezone.utc)
        self._last_resubscribe_at = None
        last_stale_check = self.last_pump_at
        self.logger.info("Streaming IBKR arrancado sobre %s", self.pairs)
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
            if (now - last_stale_check).total_seconds() >= self._STALE_CHECK_EVERY_S:
                last_stale_check = now
                self._check_staleness(now)

    def stop(self):
        self._running = False
        try:
            self.ib.pendingTickersEvent -= self._on_pending_tickers
            for pair, bars in self._bar_subs.items():
                self.ib.cancelHistoricalData(bars)
            for pair in list(self._tickers):
                self.ib.cancelMktData(self.contracts[pair])
        except Exception as exc:
            self.logger.error("Error cerrando suscripciones: %s", exc)
