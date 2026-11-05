"""
binance_vision.py
=================

Lector del archivo público de Binance (`data.binance.vision`), que es un
transporte DISTINTO del de `binance_data.py` y por eso vive aparte: aquél
pagina el REST `/fapi/v1/klines` y guarda solo OHLCV; éste descarga zips
mensuales/diarios de S3 y conserva las columnas que el REST descarta.

Se mantiene separado a propósito. `binance_data.py` sostiene dos backtests ya
cerrados (`MeanReversion_Binance_Perp`, `Intraday_Crypto_VWAP_MR`) y sus
fixtures doradas; tocarlo para añadir columnas obligaría a revalidarlos. Este
módulo reutiliza su `CACHE_ROOT` y su convención de índice (UTC naive) para
que los marcos interoperen sin conversiones.

Las dos columnas por las que existe este módulo:

- `quote_volume`  -> barras de dólar (López de Prado, AFML §2.3.1.3)
- `taker_buy_volume` -> flujo firmado sin regla del tick: Binance dice qué
  lado fue el AGRESOR, así que el signo es un dato, no una inferencia. Es lo
  que permite construir barras de desequilibrio (§2.3.2.1) sin tick data.

Referencia del archivo: https://data.binance.vision/
"""
from __future__ import annotations

import io
import os
import zipfile

import pandas as pd
import requests

from qsCrypto.backtest.binance_data import CACHE_ROOT

_VISION = "https://data.binance.vision/data"

# Esquema de los zips de klines del archivo. El REST devuelve lo mismo como
# lista posicional; aquí vienen con cabecera (los meses recientes) o sin ella
# (los antiguos), de ahí el sniffing en `_read_zip_csv`.
KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume",
    "ignore",
]
AGGTRADE_COLS = [
    "agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id",
    "transact_time", "is_buyer_maker",
]


def _read_zip_csv(blob, cols):
    """CSV de dentro de un zip del archivo, con o sin cabecera.

    Binance añadió la fila de cabecera en algún momento de 2025 y no
    reescribió los meses anteriores, así que el mismo prefijo sirve ficheros
    de los dos formatos. Leer sin comprobarlo convierte la cabecera en una
    fila de datos y envenena la primera barra del mes con NaN.
    """
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = z.namelist()[0]
        with z.open(name) as fh:
            first = fh.readline().decode("utf-8", "replace")
    has_header = not first.split(",")[0].strip().lstrip("-").isdigit()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        with z.open(z.namelist()[0]) as fh:
            return pd.read_csv(fh, header=0 if has_header else None,
                               names=cols)


def _get(url, timeout=120):
    r = requests.get(url, timeout=timeout)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content


def _months(start, end):
    s = pd.Timestamp(start).to_period("M")
    e = pd.Timestamp(end).to_period("M")
    return pd.period_range(s, e, freq="M")


def _klines_cache_path(symbol, interval, market="um"):
    d = os.path.join(CACHE_ROOT, f"{interval}_full")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{market}_{symbol}.pkl")


def fetch_klines_vision(symbol, interval="1m", start="2022-01-01",
                        end=None, market="um", use_cache=True, verbose=True):
    """Klines del archivo, con TODAS las columnas, en un caché local.

    Devuelve un DataFrame indexado por `open_time` (UTC naive, igual que
    `fetch_klines_history`) con open/high/low/close/volume más
    `quote_volume`, `count`, `taker_buy_volume`, `taker_buy_quote_volume`.

    Se descarga por MESES: 1 minuto de 4,7 años son ~2,5 M de filas y ~57
    ficheros de unos 2,5 MB, frente a ~1.650 peticiones REST paginadas que
    además no traen el flujo del agresor.

    El caché es pickle y no CSV ni parquet. CSV no round-trippea float64 al
    bit (medido: ~1e-15 relativo) y a 2,5 M de filas tarda en leerse; parquet
    necesitaría pyarrow, que no está en las dependencias declaradas de
    `pyproject.toml`. El pickle es exacto, nativo de pandas y rápido, y como
    es un caché DERIVADO siempre se puede reconstruir borrándolo.

    El caché es todo-o-nada por rango: si el fichero cubre [start, end], se
    devuelve recortado; si no, se rehace. No hay lógica incremental porque
    este marco es histórico y cerrado, no una serie que se extiende a diario.
    """
    end = pd.Timestamp(end) if end is not None else pd.Timestamp.utcnow().floor("D")
    start = pd.Timestamp(start)
    path = _klines_cache_path(symbol, interval, market)

    if use_cache and os.path.exists(path):
        cached = pd.read_pickle(path)
        if len(cached) and cached.index[0] <= start and cached.index[-1] >= end:
            return cached.loc[start:end]

    frames = []
    for per in _months(start, end):
        url = (f"{_VISION}/futures/{market}/monthly/klines/{symbol}/{interval}/"
               f"{symbol}-{interval}-{per}.zip")
        blob = _get(url)
        if blob is not None:
            frames.append(_read_zip_csv(blob, KLINE_COLS))
            if verbose:
                print(f"  {per}: {len(frames[-1]):,} filas", flush=True)
            continue
        # El mes en curso todavía no tiene fichero mensual: se completa con
        # los diarios. Sin esto la muestra se corta al último mes cerrado y
        # deja de ser comparable con la del estudio de 5 minutos.
        days = pd.date_range(max(per.start_time, start),
                             min(per.end_time, end), freq="D")
        got = 0
        for d in days:
            durl = (f"{_VISION}/futures/{market}/daily/klines/{symbol}/"
                    f"{interval}/{symbol}-{interval}-{d.date()}.zip")
            dblob = _get(durl)
            if dblob is None:
                continue
            frames.append(_read_zip_csv(dblob, KLINE_COLS))
            got += len(frames[-1])
        if verbose:
            print(f"  {per}: {got:,} filas (ficheros diarios)"
                  if got else f"  {per}: no está en el archivo — se omite",
                  flush=True)

    if not frames:
        raise RuntimeError(f"Sin datos de {symbol} {interval} en el archivo")

    df = pd.concat(frames, ignore_index=True)
    # Binance pasó de milisegundos a microsegundos en los ficheros de 2025 en
    # adelante. Se detecta por magnitud en vez de fijar una fecha de corte:
    # un timestamp en ms de 2026 tiene 13 dígitos, uno en us tiene 16.
    unit = "us" if df["open_time"].iloc[-1] > 10 ** 14 else "ms"
    df["open_time"] = pd.to_datetime(df["open_time"], unit=unit)
    df = df.set_index("open_time").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    keep = ["open", "high", "low", "close", "volume", "quote_volume", "count",
            "taker_buy_volume", "taker_buy_quote_volume"]
    df = df[keep].astype(float)
    df.index.name = "time"
    df.to_pickle(path)
    return df.loc[start:end]


def _aggtrades_cache_path(symbol, day, market="um"):
    d = os.path.join(CACHE_ROOT, "aggTrades", f"{market}_{symbol}")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{pd.Timestamp(day).date()}.pkl")


def fetch_aggtrades_day(symbol, day, keep_window=None, tz="America/New_York",
                        market="um", use_cache=True):
    """Un día de aggTrades, recortado a una ventana horaria local.

    `keep_window=("09:30", "11:30")` descarga el zip del día entero (Binance
    no sirve trozos) pero solo PERSISTE las filas de esa ventana. Para
    BTCUSDT eso baja ~20 MB comprimidos por día a unos pocos MB en caché, y
    es lo que hace viable validar decenas de sesiones: la barra de apertura
    se cierra en ~15 minutos, todo lo demás del día es peso muerto para esta
    pregunta.

    Añade `signed_qty`: `is_buyer_maker=True` significa que el COMPRADOR era
    el pasivo, o sea que el agresor fue un VENDEDOR, así que el signo es
    negativo. Invertirlo da la vuelta a todo el desequilibrio, y es un error
    que no se nota en ningún sitio salvo en el resultado.
    """
    path = _aggtrades_cache_path(symbol, day, market)
    if use_cache and os.path.exists(path):
        return pd.read_pickle(path)

    d = pd.Timestamp(day).date()
    url = (f"{_VISION}/futures/{market}/daily/aggTrades/{symbol}/"
           f"{symbol}-aggTrades-{d}.zip")
    blob = _get(url)
    if blob is None:
        raise RuntimeError(f"aggTrades de {symbol} {d} no está en el archivo")

    df = _read_zip_csv(blob, AGGTRADE_COLS)
    unit = "us" if df["transact_time"].iloc[-1] > 10 ** 14 else "ms"
    df["time"] = pd.to_datetime(df["transact_time"], unit=unit)
    df = df.set_index("time").sort_index()

    if keep_window is not None:
        lo, hi = keep_window
        loc = df.index.tz_localize("UTC").tz_convert(tz)
        hhmm = loc.strftime("%H:%M")
        df = df[(hhmm >= lo) & (hhmm < hi)]

    df["price"] = df["price"].astype(float)
    df["quantity"] = df["quantity"].astype(float)
    if df["is_buyer_maker"].dtype == object:
        buyer_maker = df["is_buyer_maker"].astype(str).str.lower() == "true"
    else:
        buyer_maker = df["is_buyer_maker"].astype(bool)
    df["signed_qty"] = df["quantity"] * (-1.0) ** buyer_maker.astype(int)
    df = df[["price", "quantity", "signed_qty"]]
    df.to_pickle(path)
    return df
