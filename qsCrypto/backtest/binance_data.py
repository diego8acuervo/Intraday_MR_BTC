"""
binance_data.py
================

Real historical data for the independent Binance-perpetuals backtest —
public REST endpoints only (klines, funding-rate history), no API key
needed, mirroring qsCrypto/data_cache/ibkr/{barSize}/ with a
qsCrypto/data_cache/binance/{interval}/ cache of its own.
"""
from __future__ import annotations

import os
import time

import pandas as pd
import requests

from qsCrypto.settings import BINANCE_ENVIRONMENTS

CACHE_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data_cache", "binance",
)
_BASE_URL = BINANCE_ENVIRONMENTS["usdm_futures"]["api"]["production"]


def _klines_cache_path(symbol, interval):
    d = os.path.join(CACHE_ROOT, interval)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{symbol}.csv")


def fetch_klines_history(symbol, interval="30m", start=None, end=None,
                         use_cache=True, request_sleep=0.25):
    """Paginated GET /fapi/v1/klines from `start` (default: 2022-01-01,
    comfortably before this pilot's coins were all listed as perpetuals) to
    `end` (default: now). Caches the merged result to
    data_cache/binance/{interval}/{symbol}.csv and only fetches the gap
    since the cached file's last row on subsequent calls.

    Returns a UTC-naive OHLCV DataFrame, same shape as
    IBKRPriceHandler.warmup() / BinanceKlinePriceHandler.warmup().
    """
    symbol = symbol.upper()
    requested_start = pd.Timestamp(start or "2022-01-01", tz="UTC").tz_localize(None)
    end = pd.Timestamp(end or pd.Timestamp.now(tz="UTC"))
    requested_end = end.tz_localize(None) if end.tzinfo else end
    start = pd.Timestamp(start or "2022-01-01", tz="UTC")
    cache_path = _klines_cache_path(symbol, interval)

    cached = None
    if use_cache and os.path.exists(cache_path):
        full_cache = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        if len(full_cache) and full_cache.index[-1] >= requested_end:
            # Cache already fully covers the requested window — no fetch
            # needed; just slice and return (this is what previously fell
            # through to a full refetch/no-op, silently ignoring `end`).
            return full_cache[(full_cache.index >= requested_start) &
                               (full_cache.index <= requested_end)]
        cached = full_cache
        if len(cached):
            start = cached.index[-1] + pd.Timedelta(minutes=1)

    rows = []
    cur = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    while cur < end_ms:
        resp = requests.get(
            f"{_BASE_URL}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval,
                    "startTime": cur, "endTime": end_ms, "limit": 1500},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1][0] + 1
        if len(batch) < 1500:
            break
        time.sleep(request_sleep)

    if rows:
        now_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
        rows = [r for r in rows if r[6] <= now_ms]
        new_df = pd.DataFrame(
            {
                "open": [float(r[1]) for r in rows],
                "high": [float(r[2]) for r in rows],
                "low": [float(r[3]) for r in rows],
                "close": [float(r[4]) for r in rows],
                "volume": [float(r[5]) for r in rows],
            },
            index=pd.DatetimeIndex(
                [pd.Timestamp(r[0], unit="ms", tz="UTC").tz_localize(None) for r in rows],
                name="time"),
        )
    else:
        new_df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.concat([cached, new_df]) if cached is not None else new_df
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if use_cache:
        df.to_csv(cache_path)
    return df[(df.index >= requested_start) & (df.index <= requested_end)]


def _funding_cache_path(symbol):
    d = os.path.join(CACHE_ROOT, "funding")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{symbol}.csv")


def fetch_funding_history(symbol, start=None, end=None, use_cache=True,
                          request_sleep=0.25):
    """Paginated GET /fapi/v1/fundingRate. Returns a pd.Series of funding
    rates (fraction, e.g. 0.0001) indexed by UTC-naive settlement time —
    the shape qsCrypto.backtest.engine.run_backtest's `funding` argument
    expects.
    """
    symbol = symbol.upper()
    requested_start = pd.Timestamp(start or "2022-01-01", tz="UTC").tz_localize(None)
    end = pd.Timestamp(end or pd.Timestamp.now(tz="UTC"))
    requested_end = end.tz_localize(None) if end.tzinfo else end
    start = pd.Timestamp(start or "2022-01-01", tz="UTC")
    cache_path = _funding_cache_path(symbol)

    cached = None
    if use_cache and os.path.exists(cache_path):
        full_cache = pd.read_csv(cache_path, index_col=0, parse_dates=True)["rate"]
        if len(full_cache) and full_cache.index[-1] >= requested_end:
            # El caché ya cubre la ventana pedida: recortar y devolver sin
            # pedir nada más. Sin este atajo, el flujo de abajo seguía
            # fetcheando desde el FINAL del caché completo y el `return`
            # devolvía la serie entera desde 2022 sin recortar a la ventana
            # solicitada — el mismo bug que `fetch_klines_history` ya evita.
            return full_cache[(full_cache.index >= requested_start) &
                              (full_cache.index <= requested_end)]
        cached = full_cache
        if len(cached):
            start = cached.index[-1] + pd.Timedelta(minutes=1)

    rows = []
    cur = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    while cur < end_ms:
        resp = requests.get(
            f"{_BASE_URL}/fapi/v1/fundingRate",
            params={"symbol": symbol, "startTime": cur, "endTime": end_ms, "limit": 1000},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1]["fundingTime"] + 1
        if len(batch) < 1000:
            break
        time.sleep(request_sleep)

    if rows:
        new = pd.Series(
            [float(r["fundingRate"]) for r in rows],
            index=pd.DatetimeIndex(
                [pd.Timestamp(r["fundingTime"], unit="ms", tz="UTC").tz_localize(None)
                 for r in rows]),
            name="rate",
        )
    else:
        new = pd.Series(dtype=float, name="rate")

    out = pd.concat([cached, new]) if cached is not None else new
    out = out[~out.index.duplicated(keep="last")].sort_index()
    if use_cache:
        out.to_frame().to_csv(cache_path)
    return out[(out.index >= requested_start) & (out.index <= requested_end)]


RTH_TZ = "America/New_York"
RTH_START = "09:30"
RTH_END = "16:15"


def restrict_to_session(df, tz=RTH_TZ, start=RTH_START, end=RTH_END,
                        weekdays_only=True):
    """Deja solo las barras de la sesión regular, en hora LOCAL del mercado.

    El filtro se aplica **a los datos**, antes de cualquier indicador. Ése es
    el punto: N, el canal de Donchian y el VWAP tienen que calcularse sobre la
    serie YA filtrada, de modo que la barra "anterior" de la primera barra del
    día sea el cierre del día anterior y no la madrugada. Filtrar después
    —calcular los indicadores en 24/7 y quedarse con las señales de la
    sesión— deja dentro de N y del canal precisamente las barras de madrugada
    y fin de semana que se quería excluir, que es el problema medido: el
    semiancho del canal cae de 2.60N a 2.02N y el payoff de 2.34 a 1.19.

    La ventana se expresa en hora local y se convierte, así que el **horario
    de verano queda resuelto**: 09:30 ET son las 13:30 UTC en verano y las
    14:30 UTC en invierno. Fijar una ventana UTC constante —como hacía la
    comparación del cuaderno anterior— desplaza la sesión una hora medio año.

    `[start, end)`: el extremo derecho queda fuera, así la barra de 16:15 no
    entra por redondeo.

    No se filtran festivos: un perpetuo cotiza el 4 de julio aunque el Nasdaq
    no. Es un residuo conocido, pequeño (~9 sesiones/año) y sin forma obvia de
    arreglarlo sin un calendario de festivos.
    """
    if len(df) == 0:
        return df
    idx = df.index
    if idx.tz is None:
        local = idx.tz_localize("UTC").tz_convert(tz)
    else:
        local = idx.tz_convert(tz)
    t = local.time
    t0, t1 = pd.Timestamp(start).time(), pd.Timestamp(end).time()
    mask = (t >= t0) & (t < t1)
    if weekdays_only:
        mask &= local.dayofweek < 5
    return df[mask]


def fetch_pilot_universe(symbols, interval="30m", start=None, end=None):
    """Fetch klines + funding for every symbol in `symbols`. Returns
    (data: {symbol: DataFrame}, funding: {symbol: Series})."""
    data, funding = {}, {}
    for sym in symbols:
        data[sym] = fetch_klines_history(sym, interval=interval, start=start, end=end)
        funding[sym] = fetch_funding_history(sym, start=start, end=end)
    return data, funding
