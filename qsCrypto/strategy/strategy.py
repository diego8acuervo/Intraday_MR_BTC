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


class MeanReversionFadeStrategy(object):
    """Fade de ruptura Donchian con confirmación, sobre barras cerradas.

    Es la estrategia validada en `MeanReversion_IBKR_CME_Bitcoin.ipynb`. La
    lógica es una máquina de TRES barras y el orden importa:

      barra t    ruptura del canal de `entry_bars`. No se abre nada. Se congelan
                 N, el nivel roto y el centro del canal DE ESTA BARRA.
      barra t+1  confirmación: el cierre tiene que volver DENTRO del canal, es
                 decir, la ruptura tiene que fallar. Si no falla, la señal se
                 descarta sin reintento. Este filtro es lo que en el backtest
                 baja los trades de 467 a 272 y sube la esperanza por trade de
                 +0.186R a +0.375R: sin él la estrategia es otra cosa.
      barra t+2  se emite la señal, con el lado INVERTIDO respecto de la ruptura
                 (`signal_mode="fade"`), para llenar al open de esta barra.

    Por qué N, nivel y centro viajan congelados desde t: son los valores con los
    que se tomó la decisión. Recalcularlos en t+2 dimensionaría la posición con
    una volatilidad que la señal no vio.

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
        if len(bars) < self.min_bars():
            return

        flat = is_flat_bar(bar["high"], bar["low"], bar["volume"])
        pend = self.pending[pair]

        # Una posición viva mata la señal pendiente: una posición por símbolo.
        if pair in self.open_symbols:
            if pend is not None:
                self.rejected["posicion_abierta"] += 1
                self.pending[pair] = None
            return

        # ── etapa 3: abrir ──
        if pend is not None and pend["etapa"] == "abrir":
            self.pending[pair] = None
            side = -pend["side"] if self.signal_mode == "fade" else pend["side"]
            signal = SignalEvent(
                pair, "market", "buy" if side > 0 else "sell", event.time,
                n=pend["n"], level=pend["level"], mid=pend["mid"],
            )
            self.events.put(signal)
            self.logger.info("Señal %s %s n=%.5f nivel=%.5f mid=%.5f",
                             signal.side, pair, pend["n"], pend["level"],
                             pend["mid"])
            return

        # ── etapa 2: confirmar ──
        if pend is not None and pend["etapa"] == "confirmar":
            if flat:
                # Un cierre plano no confirma nada.
                self.rejected["plana"] += 1
                self.pending[pair] = None
                return
            ok = ((bar["close"] < pend["level"]) if pend["side"] > 0
                  else (bar["close"] > pend["level"]))
            if not ok:
                self.rejected["sin_confirmar"] += 1
                self.pending[pair] = None
                return
            self.pending[pair] = dict(pend, etapa="abrir")
            return

        # ── etapa 1: detectar ruptura ──
        n_val = self._n(bars)
        if not (n_val == n_val) or n_val <= 0:      # NaN o no positivo
            self.rejected["sin_n"] += 1
            return
        ent_hi, ent_lo, mid = self._channel(bars)
        if ent_hi is None:
            return

        if bar["high"] > ent_hi:
            brk, level = 1, ent_hi
        elif self.allow_short and bar["low"] < ent_lo:
            brk, level = -1, ent_lo
        else:
            return

        etapa = "confirmar" if self.require_confirmation else "abrir"
        self.pending[pair] = {"side": brk, "n": n_val, "level": float(level),
                              "mid": float(mid), "etapa": etapa}

    # ── el portfolio informa de qué símbolos tienen posición ────────────────
    def set_open_symbols(self, symbols):
        self.open_symbols = set(symbols)
