"""
bars.py
=======

Muestreo alternativo al reloj: barras de volumen, de dólar y de desequilibrio
(López de Prado, *Advances in Financial Machine Learning*, cap. 2).

El argumento que se pone a prueba: la información no llega de forma uniforme
en el tiempo de reloj, así que las barras de tiempo sobremuestrean los tramos
tranquilos e inframuestrean los activos. Muestrear por ACTIVIDAD —cada X BTC,
cada X dólares, cada X de desequilibrio de flujo— debería producir series con
mejores propiedades estadísticas.

Todo se construye sobre velas de 1 minuto, no sobre ticks. La frontera de una
barra cae en el borde del minuto que cruza el umbral, no en la operación
exacta: con una barra de apertura de ~15 minutos el error es de ~7 % de su
longitud. `Information_Bars_Opening_Range.ipynb` §5 lo mide contra aggTrades
reales en vez de darlo por bueno.

Convenciones compartidas con el resto del paquete: índice UTC naive, columnas
open/high/low/close/volume, y ninguna función mira hacia adelante.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Columnas que devuelve cualquiera de los muestreadores.
BAR_COLS = ["start", "end", "open", "high", "low", "close", "volume",
            "dollar", "ticks", "signed_volume", "n_minutes", "flat_minutes"]

_ACCUM = {
    "volume": "volume",        # BTC negociados
    "dollar": "quote_volume",  # USDT negociados
    "tick": "count",           # número de operaciones
    "time": None,              # minutos: reproduce las barras de reloj
}


def session_id(index, tz="America/New_York", start="09:30", end="15:45",
               weekdays_only=True):
    """Etiqueta de sesión por marca de tiempo, o NaT fuera de sesión.

    Devuelve la FECHA LOCAL de la sesión a la que pertenece cada minuto. Es
    lo que permite reiniciar el acumulador en cada apertura sin depender del
    hueco entre barras: con barras irregulares, el heurístico de "hueco mayor
    que el paso mediano" que usa `engine.py` deja de ser fiable, porque el
    paso mediano ya no significa nada.
    """
    idx = pd.DatetimeIndex(index)
    loc = idx.tz_localize("UTC").tz_convert(tz)
    hhmm = loc.strftime("%H:%M")
    inside = (hhmm >= start) & (hhmm < end)
    if weekdays_only:
        inside &= loc.weekday < 5
    out = pd.Series(pd.NaT, index=idx, dtype="datetime64[ns]")
    out[inside] = pd.to_datetime(loc.normalize().tz_localize(None))[inside]
    return out


def _emit(rows, o, h, l, c, t0, t1, vol, dol, tk, sgn, nmin, flat):
    rows.append((t0, t1, o, h, l, c, vol, dol, tk, sgn, nmin, flat))


def sample_bars(df1m, kind="dollar", threshold=None, session_reset=True,
                tz="America/New_York", session_start="09:30",
                session_end="15:45", weekdays_only=True):
    """Barras muestreadas por actividad a partir de velas de 1 minuto.

    `kind`:
      - ``"volume"``  cierra cada `threshold` unidades del activo base (BTC)
      - ``"dollar"``  cierra cada `threshold` de volumen cotizado (USDT)
      - ``"tick"``    cierra cada `threshold` operaciones
      - ``"time"``    cierra cada `threshold` minutos — reproduce las barras
        de reloj POR EL MISMO CAMINO de código, que es lo que hace creíble
        compararlas con las demás: si el control no reproduce el resultado ya
        conocido, el muestreador está mal y ninguna otra cifra vale.

    `session_reset=True` reinicia el acumulador en cada apertura de sesión y
    descarta los minutos de fuera. No es opcional para la hipótesis que se
    prueba aquí: sin ello, "la primera barra de la sesión" puede ser una
    barra que empezó de madrugada y arrastra volumen overnight, que es
    justo lo que la hipótesis NO dice.

    `flat_minutes` cuenta los minutos con volumen 0 que entraron en la barra.
    El archivo de Binance rellena algunos tramos con velas planas (precio
    congelado, volumen 0) donde el REST sí devuelve datos; una barra con
    `flat_minutes > 0` está construida en parte sobre relleno y no sobre
    mercado. El cuaderno las cuenta y las excluye en vez de promediarlas con
    las buenas.

    Una barra se cierra en el minuto que CRUZA el umbral, así que su
    acumulado es >= `threshold` (nunca se parte un minuto). La última barra
    de cada sesión suele quedar incompleta; se emite igual y se marca por su
    acumulado, para que quien la use pueda descartarla.
    """
    if kind not in _ACCUM:
        raise ValueError(f"kind desconocido: {kind!r} (usa {list(_ACCUM)})")
    if threshold is None or threshold <= 0:
        raise ValueError("threshold debe ser > 0")

    need = ["open", "high", "low", "close", "volume"]
    missing = [c for c in need if c not in df1m.columns]
    if missing:
        raise ValueError(f"faltan columnas en df1m: {missing}")

    df = df1m
    if session_reset:
        sid = session_id(df.index, tz=tz, start=session_start,
                         end=session_end, weekdays_only=weekdays_only)
        df = df[sid.notna()]
        sid = sid[sid.notna()]
        sess = sid.to_numpy()
    else:
        sess = np.zeros(len(df), dtype=np.int64)

    o_ = df["open"].to_numpy(float); h_ = df["high"].to_numpy(float)
    l_ = df["low"].to_numpy(float);  c_ = df["close"].to_numpy(float)
    v_ = df["volume"].to_numpy(float)
    q_ = (df["quote_volume"].to_numpy(float) if "quote_volume" in df
          else v_ * c_)
    n_ = (df["count"].to_numpy(float) if "count" in df
          else np.zeros(len(df)))
    tb_ = (df["taker_buy_volume"].to_numpy(float) if "taker_buy_volume" in df
           else np.full(len(df), np.nan))
    # Flujo neto del agresor: compras a mercado menos ventas a mercado.
    s_ = 2.0 * tb_ - v_
    idx = df.index.to_numpy()

    # De qué se acumula el umbral. "time" cuenta minutos, que es lo que
    # reproduce las barras de reloj por este mismo camino.
    acc_src = {"volume": v_, "dollar": q_, "tick": n_,
               "time": np.ones(len(df))}[kind]

    rows = []
    cur = None
    for i in range(len(df)):
        if cur is None or sess[i] != cur["sess"]:
            if cur is not None:
                _emit(rows, cur["o"], cur["h"], cur["l"], cur["c"],
                      cur["t0"], cur["t1"], cur["v"], cur["d"], cur["n"],
                      cur["s"], cur["m"], cur["f"])
            cur = {"sess": sess[i], "o": o_[i], "h": h_[i], "l": l_[i],
                   "c": c_[i], "t0": idx[i], "t1": idx[i], "v": 0.0,
                   "d": 0.0, "n": 0.0, "s": 0.0, "m": 0, "f": 0, "acc": 0.0}
        cur["h"] = max(cur["h"], h_[i]); cur["l"] = min(cur["l"], l_[i])
        cur["c"] = c_[i]; cur["t1"] = idx[i]
        cur["v"] += v_[i]; cur["d"] += q_[i]; cur["n"] += n_[i]
        cur["s"] += s_[i]; cur["m"] += 1
        cur["f"] += int(v_[i] <= 0.0)
        cur["acc"] += acc_src[i]
        if cur["acc"] >= threshold:
            _emit(rows, cur["o"], cur["h"], cur["l"], cur["c"], cur["t0"],
                  cur["t1"], cur["v"], cur["d"], cur["n"], cur["s"], cur["m"],
                  cur["f"])
            # `cur = None` basta: la vuelta siguiente entra por la rama de
            # `cur is None` y abre la barra nueva con el minuto i+1, esté o
            # no en la misma sesión.
            cur = None
    if cur is not None and cur["m"] > 0:
        _emit(rows, cur["o"], cur["h"], cur["l"], cur["c"], cur["t0"],
              cur["t1"], cur["v"], cur["d"], cur["n"], cur["s"], cur["m"],
              cur["f"])

    out = pd.DataFrame(rows, columns=BAR_COLS)
    out["session"] = pd.to_datetime(
        session_id(pd.DatetimeIndex(out["start"]), tz=tz, start=session_start,
                   end=session_end, weekdays_only=weekdays_only).to_numpy()
    ) if session_reset else pd.NaT
    return out


def imbalance_bars(df1m, threshold=None, ewma_span=100, warmup_bars=20,
                   init_expected_minutes=5, session_reset=True,
                   tz="America/New_York", session_start="09:30",
                   session_end="15:45", weekdays_only=True, max_minutes=None):
    """Barras de desequilibrio de volumen (VIB/DIB, AFML §2.3.2.1).

    Acumula el flujo firmado del agresor ``theta_T = sum(2*taker_buy - volume)``
    y cierra la barra cuando ``|theta_T| >= E[T] * |2v+ - E[v]|``, con ambas
    esperanzas como EWMA sobre las barras ya cerradas. La idea: la barra se
    cierra cuando el desequilibrio de órdenes supera lo que cabía esperar, es
    decir cuando aparece flujo informado — no cada X unidades fijas.

    **Aproximación, y es importante:** las TIB/VIB del libro aplican la regla
    del tick operación a operación. Aquí el signo NO se infiere —Binance dice
    qué lado fue el agresor— pero sí se NETEA dentro de cada minuto, así que
    el flujo alternante intraminuto se cancela y el desequilibrio medido es
    una cota inferior del real. El efecto es que se cierran menos barras y más
    tarde que con tick data. §5 del cuaderno lo cuantifica contra aggTrades.

    Dos modos:

    - `threshold=X` (**el que usa el experimento**): umbral FIJO sobre
      ``|theta|``. Estable y, sobre todo, calibrable a una duración objetivo,
      que es lo que permite comparar este esquema con los de volumen y dólar
      sin que la diferencia sea simplemente que las barras duran otra cosa.
      Medido sobre BTCUSDT en sesión: 300 BTC -> ~5 min, 600 -> ~12 min.
    - `threshold=None`: la EWMA del libro. Se conserva porque es la
      formulación canónica, pero **es inestable en la práctica** y el
      cuaderno lo documenta: `E[T]` se realimenta con la duración realizada,
      así que una racha de barras largas sube el disparador, que alarga las
      barras, que vuelve a subirlo. Con velas de 1 minuto acaba topando en
      `max_minutes` y las "barras de desequilibrio" se convierten en barras
      de tiempo del tamaño del tope sin que nada lo avise.

    `max_minutes` corta barras que no alcanzan nunca el umbral (una sesión en
    la que el flujo se compensa). Sin él, una sesión entera puede devolver una
    sola barra y la "barra de apertura" deja de significar lo que dice.
    """
    need = ["open", "high", "low", "close", "volume", "taker_buy_volume"]
    missing = [c for c in need if c not in df1m.columns]
    if missing:
        raise ValueError(f"imbalance_bars necesita {missing}")

    df = df1m
    if session_reset:
        sid = session_id(df.index, tz=tz, start=session_start,
                         end=session_end, weekdays_only=weekdays_only)
        df = df[sid.notna()]
        sess = sid[sid.notna()].to_numpy()
    else:
        sess = np.zeros(len(df), dtype=np.int64)

    o_ = df["open"].to_numpy(float); h_ = df["high"].to_numpy(float)
    l_ = df["low"].to_numpy(float);  c_ = df["close"].to_numpy(float)
    v_ = df["volume"].to_numpy(float)
    q_ = (df["quote_volume"].to_numpy(float) if "quote_volume" in df
          else v_ * c_)
    n_ = (df["count"].to_numpy(float) if "count" in df else np.zeros(len(df)))
    tb_ = df["taker_buy_volume"].to_numpy(float)
    s_ = 2.0 * tb_ - v_
    idx = df.index.to_numpy()

    alpha = 2.0 / (ewma_span + 1.0)
    # Semilla del umbral, y hay que razonarla: `theta` es una suma de flujo
    # FIRMADO, así que se comporta como un paseo aleatorio y crece con
    # sqrt(T), no con T. Sembrar `e_imb` con el desequilibrio ABSOLUTO medio
    # por minuto (mean|s|) pone el disparador en e_T*mean|s|, que a 5 minutos
    # es ~297 BTC cuando lo alcanzable es sqrt(5)*std ~ 340: el umbral queda
    # al borde de lo imposible, la barra topa en `max_minutes`, `e_T` se
    # actualiza hacia ese tope y el disparador crece todavía más. Es una
    # trampa de realimentación: las barras dejan de ser de desequilibrio y
    # pasan a ser barras de tiempo del tamaño del tope, sin avisar.
    #
    # La semilla correcta sale de igualar el disparador a lo que un paseo
    # aleatorio alcanza en la duración objetivo:
    #     e_T * e_imb = sqrt(e_T) * std(s)   =>   e_imb = std(s)/sqrt(e_T)
    # y es consistente con la actualización de abajo, porque para un paseo
    # aleatorio |theta|/T ~ std/sqrt(T).
    warm = min(len(s_), max(warmup_bars * init_expected_minutes, 200))
    e_T = float(init_expected_minutes)
    sd = float(np.nanstd(s_[:warm])) if warm else 0.0
    e_imb = sd / np.sqrt(max(e_T, 1.0))
    if not np.isfinite(e_imb) or e_imb <= 0:
        e_imb = 1.0
    # Tope de `e_T`: sin él, una racha de barras largas empuja el disparador
    # hacia arriba sin vuelta atrás (la misma realimentación de antes).
    e_T_cap = float(max_minutes) if max_minutes else 10.0 * init_expected_minutes

    rows, cur, theta = [], None, 0.0
    for i in range(len(df)):
        new_sess = cur is None or sess[i] != cur["sess"]
        if new_sess:
            if cur is not None and cur["m"] > 0:
                _emit(rows, cur["o"], cur["h"], cur["l"], cur["c"], cur["t0"],
                      cur["t1"], cur["v"], cur["d"], cur["n"], cur["s"],
                      cur["m"], cur["f"])
            theta = 0.0
            cur = {"sess": sess[i], "o": o_[i], "h": h_[i], "l": l_[i],
                   "c": c_[i], "t0": idx[i], "t1": idx[i], "v": 0.0, "d": 0.0,
                   "n": 0.0, "s": 0.0, "m": 0, "f": 0}
        cur["h"] = max(cur["h"], h_[i]); cur["l"] = min(cur["l"], l_[i])
        cur["c"] = c_[i]; cur["t1"] = idx[i]
        cur["v"] += v_[i]; cur["d"] += q_[i]; cur["n"] += n_[i]
        cur["m"] += 1; cur["f"] += int(v_[i] <= 0.0)
        theta += s_[i]; cur["s"] = theta

        trigger = (float(threshold) if threshold is not None
                   # Mismo escalado que la semilla: crece con sqrt(E[T]).
                   else np.sqrt(e_T) * e_imb)
        hit = abs(theta) >= trigger
        if max_minutes is not None and cur["m"] >= max_minutes:
            hit = True
        if hit:
            _emit(rows, cur["o"], cur["h"], cur["l"], cur["c"], cur["t0"],
                  cur["t1"], cur["v"], cur["d"], cur["n"], cur["s"], cur["m"],
                  cur["f"])
            if threshold is None:
                e_T = min((1 - alpha) * e_T + alpha * cur["m"], e_T_cap)
                e_imb = (1 - alpha) * e_imb + alpha * (
                    abs(theta) / np.sqrt(max(cur["m"], 1)))
            theta = 0.0
            cur = None
            continue
    if cur is not None and cur["m"] > 0:
        _emit(rows, cur["o"], cur["h"], cur["l"], cur["c"], cur["t0"],
              cur["t1"], cur["v"], cur["d"], cur["n"], cur["s"], cur["m"],
              cur["f"])

    out = pd.DataFrame(rows, columns=BAR_COLS)
    out["session"] = pd.to_datetime(
        session_id(pd.DatetimeIndex(out["start"]), tz=tz, start=session_start,
                   end=session_end, weekdays_only=weekdays_only).to_numpy()
    ) if session_reset else pd.NaT
    return out


def opening_bars(bars):
    """La PRIMERA barra de cada sesión: una fila por sesión.

    Es el objeto de la hipótesis. Se toma por orden de `start` dentro de cada
    sesión y no por posición en el DataFrame, para que no dependa de que el
    muestreador haya emitido las filas ordenadas.
    """
    if not len(bars):
        return bars.copy()
    b = bars.sort_values("start", kind="stable")
    return b.groupby("session", as_index=False).first()
