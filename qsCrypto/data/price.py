from __future__ import print_function

import datetime
from decimal import Decimal, getcontext, ROUND_HALF_DOWN
import json
import logging
import os
import os.path
import re
import time

import numpy as np
import pandas as pd
import requests
import websocket

from qsCrypto import settings
from qsCrypto.event.event import TickEvent


class PriceHandler(object):
    """
    PriceHandler is an abstract base class providing an interface for
    all subsequent (inherited) data handlers (both live and historic).

    The goal of a (derived) PriceHandler object is to output a set of
    bid/ask/timestamp "ticks" for each currency pair and place them into
    an event queue.

    This will replicate how a live strategy would function as current
    tick data would be streamed via a brokerage. Thus a historic and live
    system will be treated identically by the rest of the qsCrypto 
    backtesting suite.
    """

    def _set_up_prices_dict(self):
        """
        Due to the way that the Position object handles P&L
        calculation, it is necessary to include values for not
        only base/quote currencies but also their reciprocals.
        This means that this class will contain keys for, e.g.
        "GBPUSD" and "USDGBP".

        At this stage they are calculated in an ad-hoc manner,
        but a future TODO is to modify the following code to
        be more robust and straightforward to follow.
        """
        prices_dict = dict(
            (k, v) for k,v in [
                (p, {"bid": None, "ask": None, "time": None}) for p in self.pairs
            ]
        )
        inv_prices_dict = dict(
            (k, v) for k,v in [
                (
                    "%s%s" % (p[3:], p[:3]), 
                    {"bid": None, "ask": None, "time": None}
                ) for p in self.pairs
            ]
        )
        prices_dict.update(inv_prices_dict)
        return prices_dict

    def invert_prices(self, pair, bid, ask):
        """
        Simply inverts the prices for a particular currency pair.
        This will turn the bid/ask of "GBPUSD" into bid/ask for
        "USDGBP" and place them in the prices dictionary.
        """
        getcontext().rounding = ROUND_HALF_DOWN
        inv_pair = "%s%s" % (pair[3:], pair[:3])
        inv_bid = (Decimal("1.0")/bid).quantize(
            Decimal("0.00001")
        )
        inv_ask = (Decimal("1.0")/ask).quantize(
            Decimal("0.00001")
        )
        return inv_pair, inv_bid, inv_ask


class HistoricCSVPriceHandler(PriceHandler):
    """
    HistoricCSVPriceHandler is designed to read CSV files of
    tick data for each requested currency pair and stream those
    to the provided events queue.
    """

    def __init__(self, pairs, events_queue, csv_dir):
        """
        Initialises the historic data handler by requesting
        the location of the CSV files and a list of symbols.

        It will be assumed that all files are of the form
        'pair.csv', where "pair" is the currency pair. For
        GBP/USD the filename is GBPUSD.csv.

        Parameters:
        pairs - The list of currency pairs to obtain.
        events_queue - The events queue to send the ticks to.
        csv_dir - Absolute directory path to the CSV files.
        """
        self.pairs = pairs
        self.events_queue = events_queue
        self.csv_dir = csv_dir
        self.prices = self._set_up_prices_dict()
        self.pair_frames = {}
        self.file_dates = self._list_all_file_dates()
        self.continue_backtest = True
        self.cur_date_idx = 0
        self.cur_date_pairs = self._open_convert_csv_files_for_day(
            self.file_dates[self.cur_date_idx]
        )

    def _list_all_csv_files(self):
        files = os.listdir(settings.CSV_DATA_DIR)
        pattern = re.compile(r"[A-Z]{6}_\d{8}\.csv")
        matching_files = [f for f in files if pattern.search(f)]
        matching_files.sort()
        return matching_files

    def _list_all_file_dates(self):
        """
        Removes the pair, underscore and '.csv' from the
        dates and eliminates duplicates. Returns a list
        of date strings of the form "YYYYMMDD". 
        """
        csv_files = self._list_all_csv_files()
        de_dup_csv = list(set([d[7:-4] for d in csv_files]))
        de_dup_csv.sort()
        return de_dup_csv

    def _open_convert_csv_files_for_day(self, date_str):
        """
        Opens the CSV files from the data directory, converting
        them into pandas DataFrames within a pairs dictionary.
        
        The function then concatenates all of the separate pairs
        for a single day into a single data frame that is time 
        ordered, allowing tick data events to be added to the queue 
        in a chronological fashion.
        """
        for p in self.pairs:
            pair_path = os.path.join(self.csv_dir, '%s_%s.csv' % (p, date_str))
            self.pair_frames[p] = pd.io.parsers.read_csv(
                pair_path, header=True, index_col=0, 
                parse_dates=True, dayfirst=True,
                names=("Time", "Ask", "Bid", "AskVolume", "BidVolume")
            )
            self.pair_frames[p]["Pair"] = p
        return pd.concat(self.pair_frames.values()).sort().iterrows()

    def _update_csv_for_day(self):
        try:
            dt = self.file_dates[self.cur_date_idx+1]
        except IndexError:  # End of file dates
            return False
        else:
            self.cur_date_pairs = self._open_convert_csv_files_for_day(dt)
            self.cur_date_idx += 1
            return True

    def stream_next_tick(self):
        """
        The Backtester has now moved over to a single-threaded
        model in order to fully reproduce results on each run.
        This means that the stream_to_queue method is unable to
        be used and a replacement, called stream_next_tick, is
        used instead.

        This method is called by the backtesting function outside
        of this class and places a single tick onto the queue, as
        well as updating the current bid/ask and inverse bid/ask.
        """
        try:
            index, row = next(self.cur_date_pairs)
        except StopIteration:
            # End of the current days data
            if self._update_csv_for_day():
                index, row = next(self.cur_date_pairs)
            else: # End of the data
                self.continue_backtest = False
                return
        
        getcontext().rounding = ROUND_HALF_DOWN
        pair = row["Pair"]
        bid = Decimal(str(row["Bid"])).quantize(
            Decimal("0.00001")
        )
        ask = Decimal(str(row["Ask"])).quantize(
            Decimal("0.00001")
        )

        # Create decimalised prices for traded pair
        self.prices[pair]["bid"] = bid
        self.prices[pair]["ask"] = ask
        self.prices[pair]["time"] = index

        # Create decimalised prices for inverted pair
        inv_pair, inv_bid, inv_ask = self.invert_prices(pair, bid, ask)
        self.prices[inv_pair]["bid"] = inv_bid
        self.prices[inv_pair]["ask"] = inv_ask
        self.prices[inv_pair]["time"] = index

        # Create the tick event for the queue
        tev = TickEvent(pair, index, bid, ask)
        self.events_queue.put(tev)


# ── CoinAPI symbol‐id helpers ────────────────────────────────────

def _pair_to_coinapi_symbol(pair, exchange="BINANCE", market="SPOT"):
    """
    Convert an internal pair like "BTCUSDT" into a CoinAPI symbol_id
    such as "BINANCEFTS_PERP_BTC_USDT" or "BINANCE_SPOT_BTC_USDT".

    For perpetual futures use market="PERP" and exchange="BINANCEFTS".
    """
    # Heuristic: quote currencies that are 4 chars (USDT, USDC, BUSD)
    if pair.endswith(("USDT", "USDC", "BUSD")):
        base, quote = pair[:-4], pair[-4:]
    else:
        base, quote = pair[:-3], pair[-3:]
    return f"{exchange}_{market}_{base}_{quote}"


def _coinapi_symbol_to_pair(symbol_id):
    """
    Convert a CoinAPI symbol_id like "BINANCE_SPOT_BTC_USDT" back
    to the internal pair format "BTCUSDT".
    """
    parts = symbol_id.split("_")
    # Format: EXCHANGE_MARKET_BASE_QUOTE  (at minimum 4 parts)
    if len(parts) >= 4:
        return parts[-2] + parts[-1]
    return symbol_id


class CoinAPIPriceHandler(PriceHandler):
    """
    CoinAPI price handler that can:
      1. Download historical OHLCV / quote data via the REST API
      2. Stream real-time quote (bid/ask) data via the WebSocket API

    Both modes produce TickEvents compatible with the rest of qsCrypto.

    Parameters54
    ----------
    pairs : list[str]
        Internal pair names, e.g. ["BTCUSDT", "ETHUSDT"].
    events_queue : queue.Queue
        The events queue shared with the trading loop.
    api_key : str
        CoinAPI API key.
    exchange : str
        Exchange identifier used in CoinAPI symbol ids (default "BINANCE").
    market : str
        Market type for symbol ids: "SPOT", "PERP", etc. (default "SPOT").
    rest_base_url : str
        Base URL for the CoinAPI REST API.
    ws_url : str
        WebSocket endpoint for real-time streaming.
    heartbeat : bool
        Whether to request heartbeat messages from the WebSocket.
    """

    REST_BASE_URL = "https://rest.coinapi.io/v1"
    WS_URL = "wss://ws.coinapi.io/v1/"

    def __init__(
        self,
        pairs,
        events_queue,
        api_key=None,
        exchange="BINANCE",
        market="SPOT",
        rest_base_url=None,
        ws_url=None,
        heartbeat=True,
    ):
        self.pairs = pairs
        self.events_queue = events_queue
        self.api_key = api_key or os.environ.get("COINAPI_KEY", "")
        self.exchange = exchange
        self.market = market
        self.rest_base_url = rest_base_url or self.REST_BASE_URL
        self.ws_url = ws_url or self.WS_URL
        self.heartbeat = heartbeat
        self.prices = self._set_up_prices_dict()
        self.logger = logging.getLogger(__name__)
        self.ws = None
        self.continue_backtest = True

        # Map CoinAPI symbol_id → internal pair for fast WS lookups
        self._symbol_map = {
            _pair_to_coinapi_symbol(p, exchange, market): p
            for p in self.pairs
        }

    def _set_up_prices_dict(self):
        """
        For crypto we only need one entry per pair (no inverse).
        """
        return {
            p: {"bid": None, "ask": None, "time": None}
            for p in self.pairs
        }

    # ── REST helpers ──────────────────────────────────────────────

    def _rest_headers(self):
        return {
            "X-CoinAPI-Key": self.api_key,
            "Accept": "application/json",
        }

    def _rest_get(self, path, params=None):
        """
        Generic GET against the CoinAPI REST API.
        Returns the parsed JSON response.
        """
        url = f"{self.rest_base_url}{path}"
        self.logger.debug("CoinAPI REST GET %s params=%s", url, params)
        resp = requests.get(url, headers=self._rest_headers(), params=params)
        resp.raise_for_status()
        return resp.json()

    # ── Historical OHLCV ──────────────────────────────────────────

    def get_historical_ohlcv(
        self,
        pair,
        period_id="1MIN",
        time_start=None,
        time_end=None,
        limit=1000,
    ):
        """
        Download OHLCV timeseries for *pair* from the CoinAPI REST API.

        Parameters
        ----------
        pair : str       – e.g. "BTCUSDT"
        period_id : str  – e.g. "1SEC", "1MIN", "5MIN", "1HRS", "1DAY"
        time_start : str – ISO‑8601 start time (optional)
        time_end : str   – ISO‑8601 end time (optional)
        limit : int      – max rows to return (max 100000)

        Returns
        -------
        pd.DataFrame with columns:
            time_period_start, time_period_end, time_open, time_close,
            price_open, price_high, price_low, price_close,
            volume_traded, trades_count
        """
        symbol_id = _pair_to_coinapi_symbol(pair, self.exchange, self.market)
        params = {"period_id": period_id, "limit": limit}
        if time_start:
            params["time_start"] = time_start
        if time_end:
            params["time_end"] = time_end

        data = self._rest_get(f"/ohlcv/{symbol_id}/history", params)
        df = pd.DataFrame(data)
        if not df.empty:
            for col in ("time_period_start", "time_period_end", "time_open", "time_close"):
                if col in df.columns:
                    df[col] = pd.to_datetime(df[col])
            df.set_index("time_period_start", inplace=True)
        return df

    # ── Historical Quotes ─────────────────────────────────────────

    def get_historical_quotes(
        self,
        pair,
        date=None,
        time_start=None,
        time_end=None,
        limit=1000,
    ):
        """
        Download historical quote (bid/ask) updates for *pair*.

        Parameters
        ----------
        pair : str       – e.g. "BTCUSDT"
        date : str       – ISO‑8601 date for a full day (preferred)
        time_start : str – ISO‑8601 start (must be same day as time_end)
        time_end : str   – ISO‑8601 end
        limit : int      – max rows (max 100000)

        Returns
        -------
        pd.DataFrame with columns:
            symbol_id, time_exchange, time_coinapi,
            ask_price, ask_size, bid_price, bid_size
        """
        symbol_id = _pair_to_coinapi_symbol(pair, self.exchange, self.market)
        params = {"limit": limit}
        if date:
            params["date"] = date
        if time_start:
            params["time_start"] = time_start
        if time_end:
            params["time_end"] = time_end

        data = self._rest_get(f"/quotes/{symbol_id}/history", params)
        df = pd.DataFrame(data)
        if not df.empty:
            for col in ("time_exchange", "time_coinapi"):
                if col in df.columns:
                    df[col] = pd.to_datetime(df[col])
            df.set_index("time_exchange", inplace=True)
        return df

    # ── Historical quote replay (backtest) ────────────────────────

    def load_historical_quotes_for_backtest(
        self,
        date=None,
        time_start=None,
        time_end=None,
        limit=100000,
    ):
        """
        Load historical quotes for all pairs, merge them
        chronologically, and store an iterator for replay
        via ``stream_next_tick()``.
        """
        frames = []
        for pair in self.pairs:
            df = self.get_historical_quotes(
                pair,
                date=date,
                time_start=time_start,
                time_end=time_end,
                limit=limit,
            )
            if not df.empty:
                df["Pair"] = pair
                frames.append(df)

        if not frames:
            self.logger.warning("No historical quote data loaded")
            self.continue_backtest = False
            self._quote_iter = iter([])
            return

        merged = pd.concat(frames).sort_index()
        self._quote_iter = merged.iterrows()
        self.continue_backtest = True

    def stream_next_tick(self):
        """
        Replay the next historical quote as a TickEvent.
        Compatible with the Backtest runner.
        """
        try:
            index, row = next(self._quote_iter)
        except StopIteration:
            self.continue_backtest = False
            return

        getcontext().rounding = ROUND_HALF_DOWN
        pair = row["Pair"]
        bid = Decimal(str(row["bid_price"])).quantize(Decimal("0.00001"))
        ask = Decimal(str(row["ask_price"])).quantize(Decimal("0.00001"))

        self.prices[pair]["bid"] = bid
        self.prices[pair]["ask"] = ask
        self.prices[pair]["time"] = index

        tev = TickEvent(pair, index, bid, ask)
        self.events_queue.put(tev)

    # ── WebSocket real-time streaming ─────────────────────────────

    def _build_hello_message(self):
        """
        Build the CoinAPI WebSocket 'hello' message to subscribe
        to real-time quote updates for the configured pairs.
        """
        symbol_ids = [
            _pair_to_coinapi_symbol(p, self.exchange, self.market) + "$"
            for p in self.pairs
        ]
        return json.dumps({
            "type": "hello",
            "apikey": self.api_key,
            "heartbeat": self.heartbeat,
            "subscribe_data_type": ["quote"],
            "subscribe_filter_symbol_id": symbol_ids,
        })

    def _on_ws_open(self, ws):
        self.logger.info("CoinAPI WebSocket opened — sending hello")
        hello = self._build_hello_message()
        ws.send(hello)

    def _on_ws_message(self, ws, message):
        try:
            msg = json.loads(message)
        except json.JSONDecodeError as e:
            self.logger.error("CoinAPI WS JSON decode error: %s", e)
            return

        msg_type = msg.get("type", "")

        if msg_type == "hearbeat":
            return  # CoinAPI spells it "hearbeat" (no 't')

        if msg_type == "error":
            self.logger.error("CoinAPI WS error: %s", msg.get("message"))
            return

        if msg_type == "reconnect":
            self.logger.warning(
                "CoinAPI WS reconnect requested within %s seconds",
                msg.get("within_seconds"),
            )
            return

        if msg_type != "quote":
            return  # Ignore unexpected message types

        symbol_id = msg.get("symbol_id", "")
        pair = self._symbol_map.get(symbol_id)
        if pair is None:
            # Try partial match (prefix)
            for sid, p in self._symbol_map.items():
                if symbol_id.startswith(sid.rstrip("$")):
                    pair = p
                    break
        if pair is None:
            self.logger.debug("Ignoring quote for unknown symbol: %s", symbol_id)
            return

        getcontext().rounding = ROUND_HALF_DOWN
        bid = Decimal(str(msg["bid_price"])).quantize(Decimal("0.00001"))
        ask = Decimal(str(msg["ask_price"])).quantize(Decimal("0.00001"))
        time_val = msg.get("time_exchange", msg.get("time_coinapi", ""))

        self.prices[pair]["bid"] = bid
        self.prices[pair]["ask"] = ask
        self.prices[pair]["time"] = time_val

        tev = TickEvent(pair, time_val, bid, ask)
        self.events_queue.put(tev)

    def _on_ws_error(self, ws, error):
        self.logger.error("CoinAPI WebSocket error: %s", error)

    def _on_ws_close(self, ws, close_status_code, close_msg):
        self.logger.info(
            "CoinAPI WebSocket closed: %s %s", close_status_code, close_msg
        )

    def stream_to_queue(self):
        """
        Connect to CoinAPI WebSocket and stream real-time quotes.
        Blocks the calling thread (run in a separate thread).
        """
        self.logger.info("Connecting to CoinAPI WebSocket: %s", self.ws_url)
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_ws_open,
            on_message=self._on_ws_message,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close,
        )
        self.ws.run_forever(ping_interval=30)

    def stop(self):
        """Close the WebSocket connection."""
        if self.ws:
            self.ws.close()


# NOTA: aquí vivía `HistIBDatos(AdminDatos)`, una celda de notebook pegada tal cual
# (líneas 578-751). Se ha eliminado porque:
#   * contenía `!pip install ib_insync`, magia de Jupyter que en un .py es un
#     SyntaxError — este módulo NO IMPORTABA, y con él caían `data/streaming.py`
#     y `trading/trading.py` enteros;
#   * heredaba de `AdminDatos`, que no existe en ningún punto del repo;
#   * no era un PriceHandler: no tocaba `events_queue` ni emitía TickEvent, así
#     que no encajaba en el bus de eventos.
# El acceso a IBKR vive ahora en `IBKRPriceHandler` (data/streaming.py), que sí
# implementa la interfaz de PriceHandler y usa `ib_async`, no `ib_insync`.
