from __future__ import print_function

from datetime import datetime, timezone
from decimal import Decimal, getcontext, ROUND_HALF_DOWN
import json
import logging
import threading
import time as _time

import pandas as pd
import requests
import websocket

from qsCrypto.event.event import BarEvent
from qsCrypto.data.price import PriceHandler
from qsCrypto import settings


class BinanceKlinePriceHandler(PriceHandler):
    """Binance USDⓈ-M kline websocket -> BarEvents, mirroring IBKRPriceHandler
    (qsCrypto/data/streaming.py:IBKRPriceHandler) but for perpetuals.

    Two things IBKR needed that perpetuals don't have at all:
      * No contract multiplier/expiry/rollover — `resolve_specs()` only reads
        exchangeInfo precision/step filters, there is no `front_month()`
        analog and none should be added here.
      * No `bars[-2]` look-ahead trick — each kline websocket message carries
        `k["x"]` (bool: is this bar closed?) directly, so that field alone
        decides whether to emit a BarEvent(complete=True).

    One thing perpetual websockets need that IBKR's `keepUpToDate` session
    didn't: a disconnect can silently drop bars (no server-side buffering),
    so `_resubscribe()` re-runs `warmup()` for the affected symbols to
    backfill the gap before resuming, which has no IBKR equivalent.

    Ticks (bid/ask, used by Portfolio._ref_price and MAE/MFE marking) are
    intentionally NOT duplicated here — run the existing
    `StreamingCryptoPrices` bookTicker handler alongside this one, both
    feeding the same events_queue, rather than reimplementing tick capture.
    """

    _STALE_CHECK_EVERY_S = 60
    _MAX_BAR_STALE_FACTOR = 1.5
    _RESUBSCRIBE_COOLDOWN_S = 300

    _INTERVAL_SECONDS = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
        "12h": 43200, "1d": 86400,
    }

    def __init__(self, pairs, events_queue, interval="30m", test_mode=True,
                 market_type="usdm_futures", warmup_limit=500,
                 execution_handler=None):
        self.pairs = [p.upper() for p in pairs]
        self.events_queue = events_queue
        self.interval = interval
        self.test_mode = test_mode
        self.market_type = market_type
        self.warmup_limit = warmup_limit
        self.logger = logging.getLogger(__name__)

        # Reuse the execution handler's exchangeInfo cache instead of
        # duplicating it, if one was supplied (data and execution layers
        # then share one cached fetch of /fapi/v1/exchangeInfo).
        self._execution_handler = execution_handler

        env_label = "testnet" if test_mode else "production"
        self.ws_base_url = settings.BINANCE_ENVIRONMENTS[market_type]["streaming"][env_label]
        self.rest_base_url = settings.BINANCE_ENVIRONMENTS[market_type]["api"][env_label]

        self.specs = {}
        self.history = {}
        self.prices = {p: {"bid": None, "ask": None, "time": None} for p in self.pairs}
        self.last_bar_at = {p: None for p in self.pairs}
        self.last_pump_at = None
        self._last_resubscribe_at = None
        self.ws = None
        self._running = False

    # ── specs (precision/step filters — no multiplier/expiry here) ───────

    def resolve_specs(self):
        """Populate self.specs from /fapi/v1/exchangeInfo.

        Delegates to the execution handler's cached get_symbol_filters()
        when available so the data and execution layers agree on one
        fetch of exchangeInfo; falls back to a local REST call otherwise
        (e.g. when this handler is used standalone, without a wired
        execution handler, such as in the isolation smoke test).
        """
        for pair in self.pairs:
            if self._execution_handler is not None:
                filters = self._execution_handler.get_symbol_filters(pair)
            else:
                filters = self._fetch_symbol_filters(pair)

            lot = filters.get("LOT_SIZE", {})
            price_f = filters.get("PRICE_FILTER", {})
            min_notional = filters.get("MIN_NOTIONAL", {})
            self.specs[pair] = {
                "stepSize": float(lot.get("stepSize", "0.001")),
                "minQty": float(lot.get("minQty", "0")),
                "tickSize": float(price_f.get("tickSize", "0.01")),
                "minNotional": float(min_notional.get("notional", "0")),
            }
        self.logger.info("Specs resueltos para %s", list(self.specs.keys()))
        return self.specs

    def _fetch_symbol_filters(self, pair):
        resp = requests.get(f"{self.rest_base_url}/fapi/v1/exchangeInfo", timeout=15)
        resp.raise_for_status()
        info = resp.json()
        for s in info["symbols"]:
            if s["symbol"] == pair:
                return {f["filterType"]: f for f in s["filters"]}
        raise ValueError(f"Symbol {pair} not found in exchange info")

    # ── warmup (REST klines) ──────────────────────────────────────────────

    def warmup(self, pairs=None):
        """REST GET /fapi/v1/klines per symbol -> UTC-naive OHLCV DataFrame,
        same shape as IBKRPriceHandler.warmup() so
        MeanReversionFadeStrategy.seed_from_handler()/get_history() work
        unmodified. Drops any still-forming last row.
        """
        for pair in (pairs or self.pairs):
            resp = requests.get(
                f"{self.rest_base_url}/fapi/v1/klines",
                params={"symbol": pair, "interval": self.interval,
                        "limit": self.warmup_limit},
                timeout=30,
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                raise RuntimeError(f"Sin histórico para {pair} ({self.interval})")

            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            # Binance's close_time (index 6) is inclusive of the still-forming
            # bar's close_time on the most recent row until it's actually
            # closed — drop any row whose close_time is in the future.
            rows = [r for r in rows if r[6] <= now_ms]

            idx = [pd.Timestamp(r[0], unit="ms", tz="UTC").tz_localize(None) for r in rows]
            df = pd.DataFrame(
                {
                    "open": [float(r[1]) for r in rows],
                    "high": [float(r[2]) for r in rows],
                    "low": [float(r[3]) for r in rows],
                    "close": [float(r[4]) for r in rows],
                    "volume": [float(r[5]) for r in rows],
                },
                index=pd.DatetimeIndex(idx, name="time"),
            ).sort_index()

            self.history[pair] = df
            self.last_bar_at[pair] = df.index[-1] if len(df) else None
            self.logger.info("Calentamiento %s: %d barras (%s -> %s)", pair,
                              len(df), df.index[0] if len(df) else None,
                              df.index[-1] if len(df) else None)
        return self.history

    def get_history(self, pair):
        if pair not in self.history:
            self.warmup([pair])
        return self.history.get(pair)

    # ── websocket streaming ────────────────────────────────────────────────

    def _build_ws_url(self):
        streams = "/".join(
            f"{pair.lower()}@kline_{self.interval}" for pair in self.pairs
        )
        return f"{self.ws_base_url}/stream?streams={streams}"

    def _on_message(self, ws, message):
        try:
            msg = json.loads(message)
            data = msg.get("data", msg)
            if data.get("e") != "kline":
                return
            k = data["k"]
            pair = data["s"].upper()
            if pair not in self.pairs:
                return

            # Use the kline's own "is closed" flag instead of IBKR's
            # bars[-2] look-ahead trick — Binance already tells us directly.
            if not k.get("x"):
                return

            ts = pd.Timestamp(k["t"], unit="ms", tz="UTC").tz_localize(None)
            o, h, l, c, v = (float(k["o"]), float(k["h"]),
                             float(k["l"]), float(k["c"]), float(k["v"]))

            if pair in self.history:
                self.history[pair].loc[ts] = [o, h, l, c, v]
            self.last_bar_at[pair] = ts

            # Cheap ref-price update from the kline close, so _ref_price()
            # has something even before the parallel bookTicker tick arrives.
            getcontext().rounding = ROUND_HALF_DOWN
            price = Decimal(str(c)).quantize(Decimal("0.00001"))
            self.prices[pair]["bid"] = price
            self.prices[pair]["ask"] = price
            self.prices[pair]["time"] = str(k.get("T", ""))

            self.events_queue.put(BarEvent(pair, ts, o, h, l, c, v, complete=True))
        except Exception as exc:
            self.logger.error("Error procesando kline: %s", exc)

    def _on_error(self, ws, error):
        self.logger.error("WebSocket error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg):
        self.logger.info("WebSocket closed: %s %s", close_status_code, close_msg)

    def _on_open(self, ws):
        self.logger.info("WebSocket connection opened: klines %s @ %s",
                          self.pairs, self.interval)

    def _backfill(self):
        """Re-run warmup() for all pairs to fill any gap left by a
        disconnect — unlike IBKR's keepUpToDate session, a dropped Binance
        websocket connection loses messages, it doesn't buffer them
        server-side."""
        self.logger.warning("Rellenando histórico tras desconexión: %s",
                             self.pairs)
        try:
            self.warmup()
        except Exception as exc:
            self.logger.error("Backfill tras reconexión falló: %s", exc)

    def _is_stale(self, now):
        bar_secs = self._INTERVAL_SECONDS.get(self.interval, 1800)
        stale_pairs = [
            pair for pair in self.pairs
            if self.last_bar_at.get(pair) is not None and
            (now.replace(tzinfo=None) - self.last_bar_at[pair]).total_seconds()
            > bar_secs * self._MAX_BAR_STALE_FACTOR
        ]
        return stale_pairs

    def _staleness_watchdog(self):
        """Runs on its own thread: `ws.run_forever()` blocks the calling
        thread, so a connection that stays open but silently stops pushing
        klines (no error, no close) can't be detected from inside the main
        streaming loop — this watchdog forces a reconnect by closing the
        socket, which causes `run_forever()` to return and the outer loop
        in stream_to_queue() to reconnect + backfill.
        """
        while self._running:
            _time.sleep(self._STALE_CHECK_EVERY_S)
            if not self._running:
                return
            now = datetime.now(timezone.utc)
            stale_pairs = self._is_stale(now)
            if not stale_pairs:
                continue
            if (self._last_resubscribe_at is not None and
                    (now - self._last_resubscribe_at).total_seconds()
                    < self._RESUBSCRIBE_COOLDOWN_S):
                continue
            self.logger.critical(
                "Feed de klines obsoleto para %s: forzando reconexión.",
                stale_pairs)
            self._last_resubscribe_at = now
            if self.ws is not None:
                try:
                    self.ws.close()
                except Exception as exc:
                    self.logger.error("Error forzando cierre: %s", exc)

    def _connect_ws(self):
        url = self._build_ws_url()
        self.logger.info("Conectando websocket de klines: %s", url)
        self.ws = websocket.WebSocketApp(
            url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )

    def stream_to_queue(self):
        """Blocks the calling thread — run in a dedicated thread, same
        convention as StreamingCryptoPrices/IBKRPriceHandler."""
        if not self.specs:
            self.resolve_specs()
        if not self.history:
            self.warmup()

        self._running = True
        first_connect = True
        watchdog = threading.Thread(
            target=self._staleness_watchdog, daemon=True)
        watchdog.start()

        while self._running:
            if not first_connect:
                self._backfill()
                _time.sleep(1)
            first_connect = False

            self._connect_ws()
            try:
                # Blocks until the connection drops; ping_interval keeps the
                # socket alive on Binance's side.
                self.ws.run_forever(ping_interval=180, ping_timeout=10)
            except Exception as exc:
                self.logger.error("Error en run_forever: %s", exc)

            if not self._running:
                break
            self.logger.warning("Websocket de klines desconectado: "
                                 "reconectando.")

    def stop(self):
        self._running = False
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception as exc:
                self.logger.error("Error cerrando websocket: %s", exc)
