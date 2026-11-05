import copy
import logging

from qsCrypto.event.event import SignalEvent


class TestStrategy(object):
    """
    A testing strategy that alternates between buying and selling
    a currency pair on every 5th tick. This has the effect of
    continuously "crossing the spread" and so will be loss-making
    strategy. 

    It is used to test that the backtester/live trading system is
    behaving as expected.
    """
    def __init__(self, pairs, events):
        self.pairs = pairs
        self.events = events
        self.ticks = 0
        self.invested = False

    def calculate_signals(self, event):
        if event.type == 'TICK' and event.instrument == self.pairs[0]:
            if self.ticks % 5 == 0:
                if self.invested == False:
                    signal = SignalEvent(self.pairs[0], "market", "buy", event.time)
                    self.events.put(signal)
                    self.invested = True
                else:
                    signal = SignalEvent(self.pairs[0], "market", "sell", event.time)
                    self.events.put(signal)
                    self.invested = False
            self.ticks += 1


class MovingAverageCrossStrategy(object):
    """
    A basic Moving Average Crossover strategy that generates
    two simple moving averages (SMA), with default windows
    of 500 ticks for the short SMA and 2,000 ticks for the
    long SMA.

    The strategy is "long only" in the sense it will only
    open a long position once the short SMA exceeds the long
    SMA. It will close the position (by taking a corresponding
    sell order) when the long SMA recrosses the short SMA.

    The strategy uses a rolling SMA calculation in order to
    increase efficiency by eliminating the need to call two
    full moving average calculations on each tick.
    """
    def __init__(
        self, pairs, events, 
        short_window=500, long_window=2000
    ):
        self.pairs = pairs
        self.pairs_dict = self.create_pairs_dict()
        self.events = events      
        self.short_window = short_window
        self.long_window = long_window

    def create_pairs_dict(self):
        attr_dict = {
            "ticks": 0,
            "invested": False,
            "short_sma": None,
            "long_sma": None
        }
        pairs_dict = {}
        for p in self.pairs:
            pairs_dict[p] = copy.deepcopy(attr_dict)
        return pairs_dict

    def calc_rolling_sma(self, sma_m_1, window, price):
        return ((sma_m_1 * (window - 1)) + price) / window

    def calculate_signals(self, event):
        if event.type == 'TICK':
            pair = event.instrument
            price = event.bid
            pd = self.pairs_dict[pair]
            if pd["ticks"] == 0:
                pd["short_sma"] = price
                pd["long_sma"] = price
            else:
                pd["short_sma"] = self.calc_rolling_sma(
                    pd["short_sma"], self.short_window, price
                )
                pd["long_sma"] = self.calc_rolling_sma(
                    pd["long_sma"], self.long_window, price
                )
            # Only start the strategy when we have created an accurate short window
            if pd["ticks"] > self.short_window:
                if pd["short_sma"] > pd["long_sma"] and not pd["invested"]:
                    signal = SignalEvent(pair, "market", "buy", event.time)
                    self.events.put(signal)
                    pd["invested"] = True
                if pd["short_sma"] < pd["long_sma"] and pd["invested"]:
                    signal = SignalEvent(pair, "market", "sell", event.time)
                    self.events.put(signal)
                    pd["invested"] = False
            pd["ticks"] += 1

# ─────────────────────────────────────────────────────────────────────────────
# Mean reversion: fade de ruptura Donchian con barra de confirmación
# ─────────────────────────────────────────────────────────────────────────────

def compute_n(highs, lows, closes, period=20):
    """N = EMA-`period` del True Range (fórmula Turtle exacta), en puntos.

    Copiada literalmente del backtest (MeanReversion_IBKR_CME_Bitcoin.ipynb,
    celda 21) para que vivo y backtest midan la volatilidad con la MISMA
    aritmética. Se siembra con la media simple de los `period` primeros TR y a
    partir de ahí es la EMA de Wilder `((p-1)*N_prev + TR)/p`; una EMA estándar
    de pandas daría otro número y las dos series divergirían en silencio.

    Devuelve un array de la misma longitud, con NaN hasta que hay muestra.
    """
    import numpy as np

    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    if len(c) == 0:
        return np.array([])
    pdc = np.roll(c, 1)
    pdc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pdc), np.abs(pdc - l)))
    n = np.full(len(tr), np.nan)
    if len(tr) < period:
        return n
    n[period - 1] = tr[:period].mean()
    for i in range(period, len(tr)):
        n[i] = ((period - 1) * n[i - 1] + tr[i]) / period
    return n


def is_flat_bar(high, low, volume):
    """Barra sin recorrido observable.

    Una barra plana tiene excursión cero: no confirma nada y no puede tocar una
    barrera. Tratarla como una barra normal infravalora la MAE, que es el
    sentido peligroso del error — hace que los stops parezcan más seguros de lo
    que son.
    """
    return bool(high <= low and (volume is None or volume <= 0))


def compute_vwap(highs, lows, closes, volumes, mode="rolling", bars=288,
                 index=None):
    """VWAP de precio típico, en puntos, alineado con la entrada.

    `vwap = Σ(((h+l+c)/3)·v) / Σ(v)` sobre la ventana. Dos modos:

    * `"rolling"` — ventana móvil de `bars` barras (48 = 24 h a 30 min). Es el
      modo por defecto: una cripto no tiene sesión a la que anclar un VWAP
      diario, así que la referencia es "lo que se ha pagado en el último día".
    * `"session"` — se reinicia en cada medianoche UTC. Requiere `index` (un
      DatetimeIndex) para saber dónde cortar.

    La barra en curso SÍ entra en su propio VWAP: a diferencia del canal
    Donchian —que se rompería contra sí mismo— el VWAP es un precio medio que
    ya se conoce al cierre de la barra, que es el instante en que se decide.

    Ventana sin volumen ⇒ NaN: un VWAP sobre volumen cero no es un precio
    medio, es una división por cero disfrazada. El que lo consume descarta la
    ruptura, igual que hace con un N inválido.
    """
    import numpy as np

    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    v = np.asarray(volumes, dtype=float)
    if len(c) == 0:
        return np.array([])
    tp = (h + l + c) / 3.0
    pv = tp * v
    out = np.full(len(c), np.nan)

    if mode == "rolling":
        if bars <= 0:
            raise ValueError("bars debe ser positivo en modo 'rolling'")
        # Sumas acumuladas: O(n) en vez de O(n·bars). cum[i] = Σ de [0, i).
        cum_pv = np.concatenate(([0.0], np.cumsum(pv)))
        cum_v = np.concatenate(([0.0], np.cumsum(v)))
        for i in range(bars - 1, len(c)):
            lo = i + 1 - bars
            vol = cum_v[i + 1] - cum_v[lo]
            if vol > 0:
                out[i] = (cum_pv[i + 1] - cum_pv[lo]) / vol
        return out

    if mode == "session":
        if index is None:
            raise ValueError("el modo 'session' necesita `index` para cortar "
                             "los días UTC")
        days = np.asarray([t.date() for t in index])
        acc_pv = acc_v = 0.0
        prev_day = None
        for i in range(len(c)):
            if days[i] != prev_day:
                acc_pv = acc_v = 0.0
                prev_day = days[i]
            acc_pv += pv[i]
            acc_v += v[i]
            if acc_v > 0:
                out[i] = acc_pv / acc_v
        return out

    raise ValueError(f"vwap_mode desconocido: {mode!r}")


def breakout_stretch(extreme, anchor, n):
    """Cuánto se ha estirado la ruptura respecto a su ancla, medido en N.

    Es la magnitud que filtra la estrategia VWAP y la que el notebook
    histograma para justificar el umbral: con el stop en 1N, exigir >= 2N de
    estiramiento es exigir una relación beneficio/riesgo de 2:1 en el momento
    de la señal, porque el objetivo ES el ancla.
    """
    if n is None or not (n == n) or n <= 0:
        return float("nan")
    if anchor is None or not (anchor == anchor):
        return float("nan")
    return abs(float(extreme) - float(anchor)) / float(n)


def rolling_range_n(highs, lows, window=288):
    """N medida sobre el HORIZONTE de la posición: el rango de las últimas
    `window` velas (con 288 velas de 5 minutos, las últimas 24 horas).

    `compute_n` NO sirve para esto. Es la media de Wilder del rango verdadero
    de CADA vela: con `period=288` sobre velas de 5 minutos devuelve el rango
    medio de una vela de 5 minutos suavizado a lo largo de un día -- unos 104 $
    con BTC a 46 000 $ -- no el recorrido de 24 horas, que son ~2 055 $. Usarla
    para dimensionar una posición que dura una sesión entera infradimensiona
    el riesgo por un factor de 13: el recorrido típico de una sesión son 13,6
    de esas N, así que arriesgar "1 % por N" arriesgaría en realidad un 13 %.
    Es el mismo error que sizing por volatilidad de 1 minuto una posición
    semanal.

    Aquí N es el rango verdadero de una vela sintética de `window` velas:
    max(high) - min(low). Causal por construcción (solo mira hacia atrás) y
    sale del mismo búfer de 288 velas que necesita el ancla VWAP, así que el
    motor en vivo no necesita una serie diaria aparte para dimensionar.

    Devuelve un array con NaN durante el calentamiento.
    """
    import numpy as np
    import pandas as pd

    window = int(window)
    h = pd.Series(np.asarray(highs, dtype=float))
    l = pd.Series(np.asarray(lows, dtype=float))
    if len(h) == 0:
        return np.array([])
    n = (h.rolling(window).max() - l.rolling(window).min()).to_numpy(copy=True)
    n[n <= 0] = np.nan
    return n


def opening_range_signal(open_px, close_px, anchor, n, allow_short=True):
    """Signo del rango de apertura, o None si no hay señal.

    La regla de IntradayMomentumStrategy en una sola función, para que el
    backtest (`OpeningRangeVwapRule`) y el motor en vivo la compartan
    literalmente y no puedan divergir -- la misma disciplina por la que
    engine.py importa `compute_n` de aquí en vez de re-derivarla.

    Dos condiciones que deben coincidir: el color de la barra (cierre contra
    apertura) y el lado del ancla (cierre contra el VWAP de 24 h). Cuando
    discrepan NO hay operación: el color solo dice que el rango de apertura
    se resolvió al alza, el ancla dice si eso ocurre por encima o por debajo
    del precio medio ponderado del día anterior. La estrategia apuesta a que
    el día continúa cuando ambas cosas apuntan al mismo sitio.

    `n` no entra en la decisión, pero se exige finito y positivo: sin N no
    hay unidad de riesgo y la señal no es dimensionable, así que no es
    operable. Devolverla igual y que el sizing la descarte más tarde
    escondería el motivo del rechazo.
    """
    vals = (open_px, close_px, anchor, n)
    if any(v is None or not (v == v) for v in vals):
        return None
    if float(n) <= 0 or float(anchor) <= 0:
        return None
    if close_px > open_px and close_px > anchor:
        return 1
    if close_px < open_px and close_px < anchor:
        return -1 if allow_short else None
    return None


class MeanReversionFadeStrategy(object):
    """Fade de ruptura Donchian con confirmación, sobre barras cerradas.

    Estrategia que puede ser rentable en horario NYSE/CME pero no con velas overnight.
    La relación stop/loss - Take profit es muy baja y al incorporar costos destruye valor 
    Requiere mantener posiciones por varios ciclos de funding rate lo que reduce la 
    rentabilidad de todos los trades que van en contra del funding rate. 
    
    Es la estrategia validada en `MeanReversion_IBKR_CME_Bitcoin.ipynb`. Tiene
    tres INSTANTES, pero solo dos LLAMADAS a `calculate_signals`: la ruptura
    pendiente se resuelve en la MISMA llamada en que llega la barra que la
    resuelve, no en una llamada posterior — diferirla un ciclo más perdería un
    bar entero de latencia que ya no se puede recuperar en vivo.

      barra t    ruptura del canal de `entry_bars`. No se abre nada. Se congelan
                 N, el nivel roto y el centro del canal DE ESTA BARRA.
      barra t+1  al recibir su CIERRE se resuelve la ruptura pendiente, en esa
                 MISMA llamada:
                   - confirma (el cierre vuelve DENTRO del canal, la ruptura
                     FALLA): se emite la señal AHORA, con el lado INVERTIDO
                     respecto de la ruptura (`signal_mode="fade"`). El fill de
                     mercado resultante cae en el open de la barra t+2 — el
                     mismo instante en que t+1 cerró, así que es ejecutable sin
                     adivinar ningún precio futuro.
                   - NO confirma (la ruptura sigue extendiéndose): se descarta
                     y, sin gastar un ciclo completo en notarlo, esta MISMA
                     barra se reevalúa como una ruptura nueva contra el canal
                     ya actualizado (que ahora incluye la barra t). Si vuelve a
                     romper, queda pendiente de confirmar en t+2; si no, no
                     queda nada pendiente.
      barra t+2  recibe el fill al open de mercado.

    Antes de este comportamiento la señal se emitía en la llamada de t+2 (con
    esa barra YA CERRADA), lo que solo es ejecutable en torno al open de t+3:
    un bar entero de retraso frente al backtest validado, con el stop y el
    objetivo fijados sobre un precio que la señal no vio — entradas tarde,
    contra un rango que ya se movió, con el mismo stop de 1N: la firma de una
    estrategia de ruina, no de la que se validó.

    Por qué N, nivel y centro viajan congelados desde t: son los valores con los
    que se tomó la decisión. Recalcularlos en el momento de la señal
    dimensionaría la posición con una volatilidad que la ruptura no vio.

    Solo reacciona a eventos BAR. Los TICK los ignora: sirven para marcar precio
    y MAE en el Portfolio, no para decidir.
    """

    def __init__(self, pairs, events, price_handler=None, entry_bars=20,
                 atr_period=20, signal_mode="fade", require_confirmation=True,
                 confirm_mode="inside", allow_short=True, max_history=500):
        self.pairs = pairs
        self.events = events
        self.price_handler = price_handler
        self.entry_bars = entry_bars
        self.atr_period = atr_period
        self.signal_mode = signal_mode
        self.require_confirmation = require_confirmation
        self.confirm_mode = confirm_mode
        self.allow_short = allow_short
        self.max_history = max_history
        self.logger = logging.getLogger(__name__)

        # Histórico por símbolo y señal pendiente por símbolo.
        self.history = {p: [] for p in pairs}
        self.pending = {p: None for p in pairs}
        # Diagnóstico: por qué se descartan señales.
        self.rejected = {"sin_confirmar": 0, "plana": 0, "sin_n": 0,
                         "posicion_abierta": 0}
        self.open_symbols = set()
        if price_handler is not None:
            self.seed_from_handler()

    # ── arranque en caliente ────────────────────────────────────────────────
    def seed_from_handler(self):
        """Precarga el histórico para no operar a ciegas las primeras horas.

        Sin esto harían falta `atr_period + entry_bars` barras nuevas — a 30 min,
        veinte horas de mercado — antes de que N y el canal existan.
        """
        for pair in self.pairs:
            try:
                df = self.price_handler.get_history(pair)
            except Exception as exc:
                self.logger.warning("Sin histórico para %s: %s", pair, exc)
                continue
            if df is None or len(df) == 0:
                continue
            self.history[pair] = [
                {"time": t, "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]),
                 "volume": float(r.get("volume", 0.0))}
                for t, r in df.iterrows()
            ][-self.max_history:]
            self.logger.info("Estrategia sembrada con %d barras de %s",
                             len(self.history[pair]), pair)

    def min_bars(self):
        """Barras necesarias antes de que la primera señal sea calculable."""
        return max(self.atr_period, self.entry_bars) + 1

    # ── indicadores sobre el histórico ya cerrado ───────────────────────────
    def _channel(self, bars):
        """(ent_hi, ent_lo, mid) del canal, EXCLUYENDO la barra en curso.

        Equivale al `.shift(1)` del backtest. Incluir la barra actual en su
        propio canal la haría romperse contra sí misma: look-ahead puro.
        """
        window = bars[-(self.entry_bars + 1):-1]
        if len(window) < self.entry_bars:
            return None, None, None
        ent_hi = max(b["high"] for b in window)
        ent_lo = min(b["low"] for b in window)
        return ent_hi, ent_lo, (ent_hi + ent_lo) / 2.0

    def _n(self, bars):
        import numpy as np
        n = compute_n([b["high"] for b in bars], [b["low"] for b in bars],
                      [b["close"] for b in bars], period=self.atr_period)
        if len(n) == 0:
            return float("nan")
        val = n[-1]
        return float(val) if np.isfinite(val) else float("nan")

    # ── contrato del motor ──────────────────────────────────────────────────
    def calculate_signals(self, event):
        if getattr(event, "type", None) != "BAR" or not event.complete:
            return
        pair = event.instrument
        if pair not in self.history:
            return

        bar = {"time": event.time, "open": float(event.open),
               "high": float(event.high), "low": float(event.low),
               "close": float(event.close), "volume": float(event.volume or 0.0)}
        self.history[pair].append(bar)
        if len(self.history[pair]) > self.max_history:
            self.history[pair] = self.history[pair][-self.max_history:]

        bars = self.history[pair]
        # Traza de auditoría: una línea INFO por barra cerrada, exista o no
        # señal. Antes de esto, el log solo dejaba rastro en el caso raro de
        # una señal emitida — todas las demás ramas (esperando historial,
        # posición abierta bloqueando, confirmación rechazada, sin ruptura)
        # volvían en silencio y no había forma de distinguir "el motor no
        # está viendo nada" de "no hubo nada que ver".
        self.logger.info(
            "BAR %s %s O=%.5f H=%.5f L=%.5f C=%.5f (%d/%d barras)",
            pair, event.time, bar["open"], bar["high"], bar["low"],
            bar["close"], len(bars), self.min_bars())
        if getattr(event, "backfill", False):
            # Barra recuperada tarde (ráfaga tras reabrir el feed): alimenta N
            # y el canal, pero no se opera. Una ruptura pendiente de antes del
            # hueco tampoco sobrevive: su barra de confirmación es esta, y
            # confirmarla ahora ejecutaría a mercado con información vieja.
            if self.pending[pair] is not None:
                self.pending[pair] = None
                self.logger.info(
                    "%s: barra de backfill, se descarta la señal pendiente.", pair)
            else:
                self.logger.info("%s: barra de backfill, no se evalúa señal.", pair)
            return
        if len(bars) < self.min_bars():
            self.logger.info("%s: historial insuficiente, se descarta la barra.", pair)
            return

        flat = is_flat_bar(bar["high"], bar["low"], bar["volume"])
        pend = self.pending[pair]

        # Una posición viva mata la señal pendiente: una posición por símbolo.
        if pair in self.open_symbols:
            if pend is not None:
                self.rejected["posicion_abierta"] += 1
                self.pending[pair] = None
                self.logger.info(
                    "%s: posición ya abierta, se descarta la señal pendiente.", pair)
            else:
                self.logger.info("%s: posición ya abierta, no se evalúa señal.", pair)
            return

        # ── resolver una ruptura pendiente, en la MISMA llamada que la recibe.
        #    Diferir esto a una llamada posterior (una etapa "abrir" separada,
        #    comprobada solo cuando llega la barra DESPUÉS de esta) perdería un
        #    bar entero de latencia frente a lo validado en el backtest: para
        #    cuando esa llamada posterior llega, la barra ya cerró y su open ya
        #    no es ejecutable. Resolviendo aquí, el fill cae en el open de la
        #    barra SIGUIENTE — el mismo instante en que ESTA cerró. ──
        if pend is not None:
            if not self.require_confirmation:
                # Sin confirmación: se entra pase lo que pase, sin condición
                # sobre el cierre.
                side = -pend["side"] if self.signal_mode == "fade" else pend["side"]
                signal = SignalEvent(
                    pair, "market", "buy" if side > 0 else "sell", event.time,
                    n=pend["n"], level=pend["level"], mid=pend["mid"],
                )
                self.events.put(signal)
                self.pending[pair] = None
                self.logger.info(
                    "Señal %s %s (sin confirmación) n=%.5f nivel=%.5f mid=%.5f",
                    signal.side, pair, pend["n"], pend["level"], pend["mid"])
                return

            if flat:
                # Un cierre plano no confirma nada.
                self.rejected["plana"] += 1
                self.pending[pair] = None
                self.logger.info(
                    "%s: barra plana no confirma la ruptura pendiente, se descarta.",
                    pair)
                return

            confirmed = ((bar["close"] < pend["level"]) if pend["side"] > 0
                        else (bar["close"] > pend["level"]))
            if confirmed:
                side = -pend["side"] if self.signal_mode == "fade" else pend["side"]
                signal = SignalEvent(
                    pair, "market", "buy" if side > 0 else "sell", event.time,
                    n=pend["n"], level=pend["level"], mid=pend["mid"],
                )
                self.events.put(signal)
                self.pending[pair] = None
                self.logger.info(
                    "%s: ruptura CONFIRMADA (close=%.5f vs nivel=%.5f) -> "
                    "señal %s n=%.5f nivel=%.5f mid=%.5f", pair, bar["close"],
                    pend["level"], signal.side, pend["n"], pend["level"], pend["mid"])
                return

            # No confirmó: la ruptura anterior sigue extendiéndose en vez de
            # fallar. Esta MISMA barra se reevalúa abajo como una ruptura
            # nueva contra el canal ya actualizado (que ahora incluye la
            # barra que la originó) -- sin esto se pierde un ciclo completo
            # antes de volver a intentarlo, y para cuando se reintentara el
            # canal que esta barra rompió ya habría cambiado.
            self.rejected["sin_confirmar"] += 1
            self.pending[pair] = None
            self.logger.info(
                "%s: ruptura NO confirmada (close=%.5f vs nivel=%.5f); "
                "reevaluando esta misma barra como nueva ruptura.",
                pair, bar["close"], pend["level"])

        # ── detectar una ruptura: nueva, o reevaluación tras un rechazo ──
        pend_new = self._detect_breakout(pair, bars)
        if pend_new is None:
            return
        self.pending[pair] = pend_new
        self.logger.info(
            "%s: RUPTURA side=%s level=%.5f n=%.5f -> esperando confirmación "
            "en la próxima barra", pair,
            "alcista" if pend_new["side"] > 0 else "bajista",
            pend_new["level"], pend_new["n"])

    def _detect_breakout(self, pair, bars):
        """`{side, n, level, mid}` de la ruptura de la última barra, o None.

        Único punto de extensión de la máquina de tres barras: una subclase
        que cambie QUÉ ruptura se opera (otra ancla, un filtro extra) toca
        solo esto y hereda intacto el resto —confirmación, reevaluación en la
        misma barra, congelado de valores en `t`—, que es donde está la
        validación.
        """
        bar = bars[-1]
        n_val = self._n(bars)
        if not (n_val == n_val) or n_val <= 0:      # NaN o no positivo
            self.rejected["sin_n"] += 1
            self.logger.info("%s: N inválido (%.5f), se descarta la barra.", pair, n_val)
            return None
        ent_hi, ent_lo, mid = self._channel(bars)
        if ent_hi is None:
            self.logger.info("%s: canal aún no calculable.", pair)
            return None

        if bar["high"] > ent_hi:
            brk, level = 1, ent_hi
        elif self.allow_short and bar["low"] < ent_lo:
            brk, level = -1, ent_lo
        else:
            self.logger.info(
                "%s: sin ruptura (H=%.5f/L=%.5f dentro de canal [%.5f, %.5f]).",
                pair, bar["high"], bar["low"], ent_lo, ent_hi)
            return None

        return {"side": brk, "n": n_val, "level": float(level),
                "mid": float(mid)}

    # ── el portfolio informa de qué símbolos tienen posición ────────────────
    def set_open_symbols(self, symbols):
        self.open_symbols = set(symbols)


# ─────────────────────────────────────────────────────────────────────────────
# Variante anclada al VWAP
# ─────────────────────────────────────────────────────────────────────────────

class MeanReversionFadeVwapStrategy(MeanReversionFadeStrategy):
    """
    Estrategia probada y rechazada: El recorrido del extremo del canal al centro
    es demasiado corto. Un trade promedio tiene un Stop de 1N y recorre máximo 
    2N hasta el VWAP. La relación profit/loss es muy mala y si se añaden costos
    y tasas de fondeo el retorno neto es negativo en cualquier horizonte inferior a 
    1 hora. 
    
    Presenta rentabilidad positiva si no se tienen en cuenta las velas nocturnas 
    y fuera de mercado y por lo tanto se suspende la operación ,pero abre riesgo de gaps
    
    Fade de ruptura Donchian anclado al VWAP, solo sobre rupturas estiradas.

    Dos diferencias con `MeanReversionFadeStrategy`, y ninguna más:

    1. **El ancla es el VWAP, no el centro del canal.** El canal conserva su
       único trabajo —detectar la ruptura—, pero la media a la que se supone
       que revierte el precio, y por tanto el objetivo de la operación, es un
       precio medio ponderado por volumen. El centro aritmético de la caja de
       Donchian no es una media de nada: es el punto medio entre dos extremos,
       y basta una mecha vieja para desplazarlo.
    2. **Solo se opera la ruptura ESTIRADA.** El extremo de la barra que rompe
       debe estar a `min_range_n` N o más del VWAP. Con el stop en `stop_n`=1N
       y el objetivo en el ancla, el umbral por defecto de 2N es un filtro de
       beneficio/riesgo 2:1 evaluado en el momento de la señal.

    El ancla viaja congelada desde `t` dentro de la clave `mid` del pendiente,
    la misma que ya usan el Portfolio y la ejecución: así el objetivo pasa a
    ser el VWAP sin tocar una línea aguas abajo.
    
    
    """

    def __init__(self, *args, min_range_n=2.0, vwap_mode="rolling",
                 vwap_bars=48, **kwargs):
        super(MeanReversionFadeVwapStrategy, self).__init__(*args, **kwargs)
        self.min_range_n = min_range_n
        self.vwap_mode = vwap_mode
        self.vwap_bars = vwap_bars
        self.rejected.setdefault("sin_vwap", 0)
        self.rejected.setdefault("poco_estirado", 0)

    def min_bars(self):
        """El VWAP móvil necesita su propia ventana antes de existir."""
        base = super(MeanReversionFadeVwapStrategy, self).min_bars()
        if self.vwap_mode == "rolling":
            return max(base, self.vwap_bars)
        return base

    def _vwap(self, bars):
        import numpy as np

        arr = compute_vwap(
            [b["high"] for b in bars], [b["low"] for b in bars],
            [b["close"] for b in bars], [b["volume"] for b in bars],
            mode=self.vwap_mode, bars=self.vwap_bars,
            index=[b["time"] for b in bars] if self.vwap_mode == "session" else None,
        )
        if len(arr) == 0:
            return float("nan")
        val = arr[-1]
        return float(val) if np.isfinite(val) else float("nan")

    def _detect_breakout(self, pair, bars):
        brk = super(MeanReversionFadeVwapStrategy, self)._detect_breakout(pair, bars)
        if brk is None:
            return None

        anchor = self._vwap(bars)
        if not (anchor == anchor) or anchor <= 0:
            self.rejected["sin_vwap"] += 1
            self.logger.info(
                "%s: VWAP no calculable (modo=%s, ventana=%d), se descarta la "
                "ruptura.", pair, self.vwap_mode, self.vwap_bars)
            return None

        bar = bars[-1]
        extreme = bar["high"] if brk["side"] > 0 else bar["low"]
        stretch = breakout_stretch(extreme, anchor, brk["n"])
        if not (stretch == stretch) or stretch < self.min_range_n:
            self.rejected["poco_estirado"] += 1
            self.logger.info(
                "%s: ruptura poco estirada (%.2fN < %.2fN al VWAP %.5f), se "
                "descarta.", pair, stretch, self.min_range_n, anchor)
            return None

        self.logger.info(
            "%s: ruptura estirada %.2fN sobre el VWAP %.5f (extremo %.5f).",
            pair, stretch, anchor, extreme)
        brk["mid"] = float(anchor)
        return brk


class VwapFadePolicy(object):
    """La misma regla de `MeanReversionFadeVwapStrategy`, para el backtest.

    El motor recorre arrays, no eventos, así que no puede instanciar la
    estrategia viva sin reescribirse entero. Esta clase es el punto único
    donde vive la regla para los dos caminos: `prepare` precalcula el ancla de
    toda la serie de una vez (el motor ya hace lo mismo con N) y `accept`
    aplica exactamente el mismo test de estiramiento.

    Que la regla esté aquí y en la estrategia obliga a comprobar que ambas
    dicen lo mismo: eso es lo que hace el test de paridad.
    """

    def __init__(self, min_range_n=2.0, vwap_mode="rolling", vwap_bars=48):
        self.min_range_n = min_range_n
        self.vwap_mode = vwap_mode
        self.vwap_bars = vwap_bars

    def warmup_bars(self):
        return self.vwap_bars if self.vwap_mode == "rolling" else 0

    def prepare(self, df):
        volumes = (df["volume"].to_numpy(dtype=float) if "volume" in df.columns
                   else None)
        if volumes is None:
            raise ValueError(
                "VwapFadePolicy necesita la columna 'volume': un VWAP sin "
                "volumen no es un VWAP.")
        return compute_vwap(
            df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float),
            df["close"].to_numpy(dtype=float), volumes,
            mode=self.vwap_mode, bars=self.vwap_bars, index=df.index)

    def accept(self, brk, extreme, anchor):
        """True si la ruptura se opera; el motor ya habrá puesto `anchor` en
        `brk["mid"]` si es así."""
        if anchor is None or not (anchor == anchor) or anchor <= 0:
            return False, "sin_vwap"
        stretch = breakout_stretch(extreme, anchor, brk["n"])
        if not (stretch == stretch) or stretch < self.min_range_n:
            return False, "poco_estirado"
        return True, None


def vwap_fade_policy(min_range_n=2.0, vwap_mode="rolling", vwap_bars=48):
    """Azúcar para el notebook: `run_backtest(df, p, policy=vwap_fade_policy())`."""
    return VwapFadePolicy(min_range_n=min_range_n, vwap_mode=vwap_mode,
                          vwap_bars=vwap_bars)


class IntradayMomentumStrategy(object):
    """Momentum del rango de apertura de la sesión de contado de EE. UU.

    Sept 22 2026

    Estrategia que recoge los datos de los primeros 5 minutos del mercado
    NYSE/CME como predictor del color de la vela diaria. Si la vela de los
    primeros 5 minutos del día es alcista y cierra por encima del VWAP de las
    últimas 24 horas (288 velas de 5 minutos, incluidas las overnight) se
    considera una señal de compra. Si es bajista y cierra por debajo de ese
    VWAP, señal de venta. La regulación del sizing se hace por unidades de
    riesgo normalizadas por volatilidad (N sizing). Las posiciones se
    mantienen abiertas hasta las 3:45 PM del mismo día: no hay posición
    overnight.

    Es posible hacer extensiones o modificaciones con volume bars o dollar
    bars para cambiar el muestreo por tiempo de mercado en lugar de tiempo de
    reloj. Para implementar las volume bars se requiere tick data de los
    primeros 5 minutos de todas las sesiones sobre las que se vaya a hacer
    backtesting.

    Contrato de implementación
    --------------------------
    1. Dos instantes, no tres. A diferencia de MeanReversionFadeStrategy no
       hay barra de confirmación: la señal nace al CIERRE de la barra de
       apertura (09:35 ET con `or_bars=1`) y se ejecuta al open de la
       siguiente. En vivo eso es una orden a mercado enviada en ese mismo
       instante; en el backtest es el `fire_pending` del motor. Resolverla
       sobre la propia barra de apertura sería look-ahead de una barra.

    2. El ancla mira 24 horas, no la sesión. El VWAP se calcula sobre las 288
       velas de 5 minutos anteriores INCLUIDAS las overnight: es el precio
       medio al que cambió de manos el contrato desde la apertura anterior,
       y es justo la parte que una serie filtrada a la sesión no puede ver.
       Por eso el handler en vivo debe alimentar la estrategia con la serie
       24/7 completa, no solo con las barras de sesión, y por eso el backtest
       calcula el ancla sobre el marco 24/7 (`ctx`) y no sobre `df`.

    3. N mide el mismo horizonte que la posición, vía `rolling_range_n`:
       el recorrido de las últimas 24 h (288 velas), no el rango medio de una
       vela de 5 minutos. Ver el docstring de `rolling_range_n` para por qué
       `compute_n(period=288)` mide otra cosa y desdimensiona por ~13x.

    4. `n` se congela en el instante de la señal y viaja en el SignalEvent,
       igual que en las estrategias de reversión: el sizing y el stop del
       portfolio no vuelven a recalcularlo. `mid` lleva el ancla y `level` el
       cierre de la barra de apertura, para que el diario pueda reconstruir
       por qué se abrió la posición.

    5. Una señal por sesión y símbolo. El disparo se cierra con la fecha
       local de la sesión (`self.fired`), no con un contador de barras: una
       reconexión del websocket que reenvíe la barra de apertura no puede
       abrir una segunda posición.

    6. La salida NO es de esta clase. El cierre a las 15:45 es una barrera
       temporal del portfolio (`session_flat_at` en
       BinancePerpetualsPortfolio, `flat_at_session_end` en el motor): la
       estrategia solo emite entradas, como todas las demás de este paquete.
    """

    def __init__(self, pairs, events, price_handler=None,
                 session_tz="America/New_York", session_open="09:30",
                 or_bars=1, bar_seconds=300, vwap_bars=288,
                 vwap_mode="rolling", n_window=288, allow_short=True,
                 max_history=900):
        self.pairs = pairs
        self.events = events
        self.price_handler = price_handler
        self.session_tz = session_tz
        self.session_open = session_open
        self.or_bars = int(or_bars)
        self.bar_seconds = int(bar_seconds)
        self.vwap_bars = int(vwap_bars)
        self.vwap_mode = vwap_mode
        self.n_window = int(n_window)
        self.allow_short = allow_short
        self.max_history = max_history
        self.logger = logging.getLogger(__name__)

        # Histórico 24/7 por símbolo (el ancla necesita las overnight).
        self.history = {p: [] for p in pairs}
        # Fecha local de la última sesión en la que ya se disparó, por símbolo.
        self.fired = {p: None for p in pairs}
        # Diagnóstico: por qué se descartan señales. Sin esto, una regla que
        # rechaza el 90 % de las sesiones y una serie sin señales producen el
        # mismo resultado vacío.
        self.rejected = {"sin_n": 0, "sin_vwap": 0, "desacuerdo": 0,
                         "posicion_abierta": 0, "sin_muestra": 0}
        self.open_symbols = set()
        if price_handler is not None:
            self.seed_from_handler()

    # -- calentamiento ------------------------------------------------------

    def min_bars(self):
        """Barras 24/7 necesarias antes de poder evaluar una señal."""
        return max(self.vwap_bars, self.n_window) + self.or_bars

    def seed_from_handler(self):
        """Rellena el histórico con el warmup del price handler."""
        for pair in self.pairs:
            try:
                hist = self.price_handler.get_history(pair)
            except Exception as exc:
                self.logger.warning("Sin histórico inicial para %s: %s", pair, exc)
                continue
            if hist is None or not len(hist):
                continue
            bars = [{"time": ts, "open": float(r["open"]), "high": float(r["high"]),
                     "low": float(r["low"]), "close": float(r["close"]),
                     "volume": float(r.get("volume", 0.0) or 0.0)}
                    for ts, r in hist.iterrows()]
            self.history[pair] = bars[-self.max_history:]
            self.logger.info("Seed %s: %d barras (min_bars=%d)",
                             pair, len(self.history[pair]), self.min_bars())

    def set_open_symbols(self, symbols):
        self.open_symbols = set(symbols)

    # -- reloj de sesión ----------------------------------------------------

    def _local(self, ts):
        """Marca UTC-naive -> hora local del mercado. `sessions.py` no sirve
        aquí: pide un ContractDetails de IBKR y solo publica unos días hacia
        delante, mientras que esto tiene que funcionar contra Binance."""
        from datetime import timezone
        from zoneinfo import ZoneInfo
        import pandas as pd
        return (pd.Timestamp(ts).tz_localize(timezone.utc)
                .tz_convert(ZoneInfo(self.session_tz)))

    def _is_signal_bar(self, ts):
        """¿Es `ts` la apertura de la barra que CIERRA el rango de apertura?

        Se compara contra la hora local de apertura desplazada `or_bars-1`
        barras: con `or_bars=1` es la barra de las 09:30 (que cierra a las
        09:35). Solo días de semana; los festivos del NYSE no se filtran (no
        hay calendario aquí), pero en un festivo no hay apertura de contado
        que seguir y la señal es ruido — el backtest lo cuantifica.
        """
        import pandas as pd
        loc = self._local(ts)
        if loc.weekday() >= 5:
            return False, None
        hh, mm = (int(x) for x in self.session_open.split(":"))
        offset = (self.or_bars - 1) * self.bar_seconds
        open_local = loc.normalize() + pd.Timedelta(hours=hh, minutes=mm,
                                                    seconds=offset)
        return loc == open_local, loc.date()

    # -- señal --------------------------------------------------------------

    def _n(self, bars):
        n = rolling_range_n([b["high"] for b in bars], [b["low"] for b in bars],
                            window=self.n_window)
        val = n[-1]
        return float(val) if val == val else float("nan")

    def _vwap(self, bars):
        v = compute_vwap([b["high"] for b in bars], [b["low"] for b in bars],
                         [b["close"] for b in bars], [b["volume"] for b in bars],
                         mode=self.vwap_mode, bars=self.vwap_bars,
                         index=[b["time"] for b in bars])
        val = v[-1]
        return float(val) if val == val else float("nan")

    def calculate_signals(self, event):
        if getattr(event, "type", None) != "BAR" or not event.complete:
            return
        pair = event.instrument
        if pair not in self.history:
            return

        bar = {"time": event.time, "open": float(event.open),
               "high": float(event.high), "low": float(event.low),
               "close": float(event.close), "volume": float(event.volume or 0.0)}
        self.history[pair].append(bar)
        if len(self.history[pair]) > self.max_history:
            self.history[pair] = self.history[pair][-self.max_history:]

        # Una barra de relleno alimenta el ancla y la N pero no opera: llegó
        # tarde y su "siguiente open", que es donde se ejecutaría, ya pasó.
        if getattr(event, "backfill", False):
            return

        is_signal, session_day = self._is_signal_bar(event.time)
        if not is_signal:
            return
        if self.fired[pair] == session_day:
            return

        bars = self.history[pair]
        if len(bars) < self.min_bars():
            self.rejected["sin_muestra"] += 1
            self.fired[pair] = session_day
            return
        if pair in self.open_symbols:
            self.rejected["posicion_abierta"] += 1
            self.fired[pair] = session_day
            return

        n_val = self._n(bars)
        if not (n_val == n_val) or n_val <= 0:
            self.rejected["sin_n"] += 1
            self.fired[pair] = session_day
            return
        anchor = self._vwap(bars)
        if not (anchor == anchor) or anchor <= 0:
            self.rejected["sin_vwap"] += 1
            self.fired[pair] = session_day
            return

        # El rango de apertura: apertura de la primera barra, cierre de ésta.
        open_px = bars[-self.or_bars]["open"]
        close_px = bar["close"]
        side = opening_range_signal(open_px, close_px, anchor, n_val,
                                    allow_short=self.allow_short)
        self.fired[pair] = session_day
        if side is None:
            self.rejected["desacuerdo"] += 1
            self.logger.info(
                "%s %s: sin señal (O=%.5f C=%.5f VWAP=%.5f)",
                pair, event.time, open_px, close_px, anchor)
            return

        self.logger.info(
            "%s %s: SEÑAL %s rango_apertura O=%.5f C=%.5f VWAP24h=%.5f N=%.5f",
            pair, event.time, "compra" if side > 0 else "venta",
            open_px, close_px, anchor, n_val)
        self.events.put(SignalEvent(
            pair, "market", "buy" if side > 0 else "sell", event.time,
            n=n_val, level=float(close_px), mid=float(anchor)))
