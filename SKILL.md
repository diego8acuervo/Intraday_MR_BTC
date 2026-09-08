---
name: MR_BTC_System
description: Mean-reversion fade de ruptura Donchian sobre futuros de Bitcoin CME (MBT, 30 min) — la especificación completa del backtest validado (MeanReversion_IBKR_CME_Bitcoin.ipynb), su veredicto de validación, y la implementación en vivo sobre la arquitectura dirigida por eventos de qsCrypto (IBKRPriceHandler, MeanReversionFadeStrategy, IBKRFuturesPortfolio, IBKRExecutionHandler) más el catálogo de trampas medidas de la API de IBKR. Úsalo siempre que se toque la estrategia de reversión, el notebook Turtle_MR_Intraday_Live, el bus de eventos de qsCrypto, el seguimiento de MAE/MFE en R, o cualquier integración con Interactive Brokers vía ib_async. Triggers: mean reversion, fade, Donchian, MBT, MAE, MFE, R units, triple barrera, IBKR, ib_async, TWS, event-driven, PriceHandler, ExecutionHandler, front month, bracket order.
---

# MR BTC System — reversión sobre MBT, del backtest al vivo

Referencia única de la estrategia de reversión a la media sobre futuros de Micro Bitcoin
(CME `MBT`) y de su implementación en vivo. Cubre tres cosas que conviene no separar: **qué
hace la estrategia**, **qué dijo su validación**, y **qué hace la API de IBKR que no está en
su documentación**.

---

## 1. La estrategia

**No es un z-score ni bandas de Bollinger.** Es un **fade de ruptura Donchian con barra de
confirmación**: se espera a que una ruptura del canal **falle** y se opera contra ella.

### Máquina de tres barras

| Barra | Qué pasa | Qué se abre |
|---|---|---|
| `t` | `high > ent_hi` (o `low < ent_lo` si hay cortos). Se **congelan** `n`, `level`, `mid` de esta barra. | nada |
| `t+1` | Confirmación `inside`: el cierre debe volver **dentro** del canal. Barra plana no confirma. Si no confirma, la señal **se descarta sin reintento**. | nada |
| `t+2` | Señal con el lado **invertido** (`signal_mode="fade"`). | fill al **open** de esta barra |

`n`, `level` y `mid` vienen de la barra `t`, **no** de la barra de entrada. Recalcularlos en
`t+2` dimensionaría la posición con una volatilidad que la señal no vio.

**El filtro de confirmación no es cosmético:** baja los trades de 467 a 272, los stops de 329 a
165, y **duplica la esperanza por trade, de +0,186 R a +0,375 R**. Solo confirma el ~27 % de las
rupturas. Sin él la estrategia es otra cosa.

### Indicadores

```python
# N = EMA-20 del True Range, fórmula Turtle exacta. Se siembra con la SMA de los
# 20 primeros TR y luego es la EMA de Wilder. Una EMA de pandas da otro número.
n[period-1] = tr[:period].mean()
n[i] = ((period - 1) * n[i-1] + tr[i]) / period

ent_hi = high.rolling(20).max().shift(1)   # el .shift(1) evita el look-ahead:
ent_lo = low.rolling(20).min().shift(1)    # una barra en su propio canal nunca
mid    = (ent_hi + ent_lo) / 2             # podría romperlo
```

### Parámetros validados

```
SYMBOLS=["MBT"]  BAR_SIZE="30 mins"  USE_RTH=True  WHAT_TO_SHOW="TRADES"
RTH_OVERLAP=("13:30","20:15") UTC     ROLL_DAYS=2  (= front_month(min_days=2))

ENTRY_BARS=20   ATR_PERIOD=20   SIGNAL_MODE="fade"
REQUIRE_CONFIRMATION=True  CONFIRM_MODE="inside"  ENTRY_TIMING="next_open"
ALLOW_SHORT=True

RISK_PER_UNIT=0.01   STOP_N=1.0 (R = 1N)   MAX_UNITS=4   ADD_N=0.5
TP_MODE="mid_channel"   MAX_HOLD_BARS=10 (5 h)   TIE_BREAK="pessimistic"
MAX_GROSS_LEV=4.0  MAX_DIR_UNITS=12  MAX_CORR_UNITS=6  USE_DD_RULE=True
SIZE_ON="r"  ADD_UNTIL_MID=True  USE_DONCHIAN_EXIT=False  EQUITY_0=100_000
COSTS: MBT comm $0.62/contrato/lado, slip 1 tick (= $0.50). FUNDING=0 (los futuros no pagan).
```

- `exit_bars=10` se calcula pero **nunca se lee** (`USE_DONCHIAN_EXIT=False`).
- `ENTRY_TIMING="next_open"` es **inerte**: con confirmación activa siempre se toma la ruta de
  dos etapas. El comentario del notebook contradice al código; manda el código.
- `MAE_STOP_N_FIJO=3.0` en la celda 4 lleva un comentario **obsoleto** que habla de `R = 2N`.

### Sizing

```python
qty = floor(risk * eq_size / (stop_n * n_entry * multiplier))   # contratos ENTEROS
```
`floor`, nunca `round`: redondear hacia arriba excede el riesgo declarado en silencio. `qty < 1`
rechaza la señal. `eq_size` aplica la regla Turtle de drawdown: cada −10 % de equity ⇒ nocional
×0,80, con suelo en el 5 % del capital inicial.

### Barreras (prelación pesimista)

Orden por barra: **marcar → eq_size → salidas → adds → entradas**.

1. `gap_stop` / `gap_take` (el open ya está pasado el nivel)
2. `stop_tie` — si se tocan stop y objetivo en la misma barra, **gana el stop**. Sin datos
   intrabarra no se sabe cuál llegó antes, y suponer el favorable es el sesgo que infla backtests.
3. `stop` — 1N desde el **último fill** (la escalera lo arrastra)
4. `target` — `mid_px`, congelado en la ruptura, **nunca** reanclado
5. `vertical` — 10 barras, sale al `close`

Mezcla real (272 trades): stop 124, gap_target 43, target 42, gap_stop 41, vertical 22 →
**TP 31 % | stop 61 % | vertical 8 %**. Mediana de 3 barras hasta la salida, de 10 posibles.

### Escalera

`trig = last_fill + side*0.5*N`, hasta 4 unidades, con `add_until_mid` (no se engorda una
posición que ya llegó al objetivo). Cada add hace `Excursion.reanchor(fill)` y **sube el stop de
todas las unidades**. Riesgo agregado con 1/2/3/4 unidades cargadas: **1,0 / 1,5 / 1,5 / 1,0 R**
— no monótono. Con 4 unidades el stop común queda a −0,5N de la entrada inicial. Media observada
1,77 unidades por trade.

---

## 2. Lo que dijo la validación — leer antes de tocar nada

**Veredicto del propio backtest: 8/12 criterios, "No desplegar capital con esta evidencia".**

| Criterio | Exigido | Medido |
|---|---|---|
| Muestra | ≥ 2 años | **1,34 años** (4.284 barras, 2025-04 → 2026-08) |
| Folds OOS | ≥ 8 | **6** — el t-test no tiene potencia |
| Apuestas independientes | ≥ 8 | **n_eff = 1,00** |
| Deflated Sharpe | > 0,95 | **DSR = 0,374** |

Rendimiento nominal: CAGR +65,03 %, **Sharpe 1,86**, MaxDD −27,27 %, 272 trades, win rate
36,4 %, PF 1,32, payoff 2,31. Contraste: seguir la ruptura en vez de fadearla da Sharpe **−0,82**.

**Sobre el DSR el notebook es explícito:** el Sharpe seleccionado *no supera lo que cabría
esperar del mejor de esa rejilla sin ningún edge*. **El resultado es selección.** Y exige *más
muestra o menos búsqueda, no otra configuración*.

Otros hechos medidos que condicionan cualquier lectura:

- **`n_eff = 1,00`**: con una sola apuesta independiente, la ley de los grandes números que hacía
  viable un win-rate del 35 % no opera. **Un drawdown del 50 %+ no es cola, es el escenario
  central.** El notebook lo llama su hallazgo más importante. Monte Carlo: P(DD ≥ 50 %) = 10,6 %.
- **Concentración de P&L**: top 5 trades = **62,3 %** del total; top 10 = 114,5 %.
- **Empalme de vencimientos**: 12 rolls, salto medio +1,231 %, suma +14,77 % → **+11 %/año de
  retorno que no es mercado**. La serie **no está retroajustada**.
- **Regla de drawdown**: con `use_dd_rule=True` Sharpe 1,86; con `False`, 1,07. Buena parte del
  resultado viene de la regla de nocional, no de la señal.
- **Barras planas 8,5 %** (esperado ~1 %): por encima de eso la MAE queda **infravalorada**, que
  es el sentido peligroso — los stops parecen más seguros de lo que son.
- **E-ratio (MFE/MAE) = 0,99**: la entrada no tiene edge direccional inmediato. Es un problema de
  *timing*, no de stop; apretar el stop no lo arregla.
- **Hueco de entrada = 0 %** por construcción, al llenar al **open**. En el notebook de ruptura,
  un Sharpe de 3,5 resultó ser **íntegramente** hueco de entrada.
- El WFO converge en los mismos parámetros de la tesis fija (`entry=20`, `stop_n=1.0` 6/6 folds)
  pero **gana en 1 de 6 folds**: media OOS WFO +4,45 % frente a baseline +8,74 %.

### La calibración de MAE, y por qué no se puede usar

| Régimen | trades | win rate | MAE p90 ganadores | **MAE\*** | KS |
|---|---|---|---|---|---|
| sin stop | 178 | 49 % | 1,88 N | 1,88 N | 0,61 |
| **stop 3N (referencia)** | 191 | 47 % | 1,55 N | **1,55 N = 0,52 R** | 0,62 |
| stop 1N | 256 | 27 % | 0,73 N | 0,76 N | 0,62 |

La MAE **sí discrimina**: `P(ganador | MAE ≥ MAE*) = 11,8 %` frente a una base del 46,6 %,
monótona decreciente, KS 0,62.

**Pero con el stop operado de 1N, MAE\* = 1,55 N NO CABE dentro del stop.** El stop duro llega
siempre antes, así que **el cierre temprano por MAE nunca puede dispararse**. Se mide, se registra
y se muestra; no cierra. Subir `stop_n` lo haría caber a costa del único punto alto de la curva de
sensibilidad (Sharpe 1,86 a 1N frente a 0,31 a 3N).

**Circularidad sin resolver:** MAE\* es un percentil del MAE de los ganadores, pero quién gana
depende del stop; cambiar el stop cambia `t1`, que cambia los conjuntos de purga. El MAE\* del
notebook es un estadístico de muestra completa. Ni `lopez_de_prado.md` ni `MAE.md` lo abordan.

---

## 3. La implementación en vivo

Notebook: `qsCrypto/notebooks/Turtle_MR_Intraday_Live.ipynb` (solo cablea y observa; **no
contiene lógica de estrategia**).

### Flujo de eventos

```
trading (cola)  →  data       BarEvent (barra CERRADA) + TickEvent (bid/ask)
                →  strategy   consume BAR, emite SignalEvent(n, level, mid)
                →  portfolio  consume SIGNAL, dimensiona, emite OrderEvent(stop_price)
                →  execution  consume ORDER, bracket a IBKR, emite FillEvent
                →  portfolio  consume FILL, reancla al precio REAL, emite PortfolioEvent
                →  trading    enruta PORTFOLIO al monitor
```

**Híbrido deliberado:** la señal necesita OHLC de 30 min; el MAE/MFE necesita ticks. A
granularidad de media hora la excursión adversa sería invisible.

### Clases

| Fichero | Clase | Notas |
|---|---|---|
| `event/event.py` | `BarEvent`, `FillEvent`, `PortfolioEvent` | `SignalEvent` gana `n`, `level`, `mid` con defecto `None` (retrocompatible) |
| `trading/trading.py` | `trade(..., monitor=None)` | ramas `BAR`, `FILL`, `PORTFOLIO` |
| `data/streaming.py` | `IBKRPriceHandler(PriceHandler)` | `front_month()` reutilizado, `warmup()`, guarda de NaN |
| `strategy/strategy.py` | `MeanReversionFadeStrategy`, `compute_n`, `is_flat_bar` | máquina de 3 barras |
| `portfolio/portfolio.py` | `IBKRFuturesPortfolio(Portfolio)` | sizing, barreras, `update_fill`, `snapshot()` |
| `execution/execution.py` | `IBKRExecutionHandler(ExecutionHandler)` | bracket nativo, `modify_stop`, `flatten` |
| `portfolio/position.py` | — | fin del slicing 3/3; `multiplier` |

### Arreglos que hicieron falta en el motor

1. **`data/price.py:589` tenía `!pip install ib_insync`** — magia de Jupyter en un `.py`.
   `SyntaxError` al importar: **`streaming.py` no importaba y `trading.py` no arrancaba**.
   El bloque 578-751 (`HistIBDatos(AdminDatos)`, con `AdminDatos` inexistente) se eliminó.
2. **`Portfolio.execute_signal` nunca pasaba `initial_stop`** → `r_unit = None` →
   **`calculate_mae_r()` devolvía `None` SIEMPRE**. El MAE en R no existía en el camino
   dirigido por eventos. Es el arreglo que hace que el monitor tenga algo que enseñar.
3. **`Position.set_up_currencies` partía el símbolo 3/3** (`pair[:3]`/`pair[3:]`): con `MBT` o
   `MBTU6` produce base/quote basura y `calculate_profit_base` revienta con `KeyError`.
4. **`calc_risk_position_size` no tenía multiplicador ni N** y devolvía fracciones. Sin el
   multiplicador, MBT (0,1 BTC/contrato) da P&L 10× el real.
5. La arquitectura es **duck-typed**: solo `ExecutionHandler` declara ABC, y con
   `__metaclass__ = ABCMeta`, idiom de Python 2 que en Py3 **no hace nada**.

### Invariantes fijados por tests

`qsCrypto/strategy/strategy_test.py` (18) y `qsCrypto/portfolio/ibkr_portfolio_test.py` (19):

- N es la EMA de Wilder sembrada con SMA, y el TR usa el cierre anterior.
- Ruptura sola no abre; sin confirmar se descarta; barra plana no confirma; `n`/`level`/`mid`
  son los de `t`; la barra actual está excluida de su propio canal.
- `floor(0.01·100000/(1,0·N·0,1))` con N=500 → 20 contratos; `qty*R*mult ≤ 1 %` del equity.
- Prelación pesimista, objetivo en el centro del canal, vertical, barra plana no toca nada pero
  **sí avanza el reloj**.
- El fill reancla al precio real; `snapshot()` devuelve `mae_r`/`mfe_r` no nulos.

**8 fallos preexistentes y ajenos** en `portfolio_test.py`: `settings.OUTPUT_RESULTS_DIR = None`
hace reventar `create_equity_file` con `backtest=True`.

---

## 4. Trampas de IBKR — medidas, no supuestas

Todo esto se descubrió depurando en vivo. Vale para cualquier integración con `ib_async`.

### `IB.RequestTimeout = 0` significa SIN LÍMITE
`util.run` hace `if timeout:` — y `0` es falsy. **Toda llamada bloqueante puede colgar el proceso
para siempre.** Poner `ib.RequestTimeout = 20` nada más conectar.

### `reqAccountUpdates` cuelga en subcuentas FA
`ib_async` ya se suscribe al conectar (*"This is called at startup - no need to call again"*).
Volver a pedirlo para una subcuenta **nunca recibe `accountDownloadEnd`**. `ib.accountValues()`
funciona sin llamarlo.

### Las posiciones de divisa al contado NO se reportan
Medido con 25.000 EUR vivos: **tres pasadas de `reqPositions` en 9 s, ni una fila con cantidad
≠ 0**; `ib.portfolio()` vacío en las seis cuentas. Una posición spot es un **saldo en divisa**
(en TWS vive en el FX Portfolio). Hay que leerla de `accountValues` (`CashBalance` en la divisa
base) y contrastarla con el neto de ejecuciones.

### Login FA: reportar y operar son cuentas distintas
El master `DF*` **rechaza órdenes sin reparto**. Las posiciones pueden reportarse bajo una
etiqueta y las ejecuciones bajo otra. Reglas:
- Toda orden lleva `order.account` explícito.
- Buscar posiciones **por conId primero, por cuenta después**.
- La orden de cierre va a la cuenta **operable**, nunca al master.

### Nunca inferir "cerrada" de la ausencia de posición
Una lista vacía significa *no lo veo*, no *no está*. Puede ser propagación, etiqueta distinta o
una fuente caída. **Exigir prueba positiva**: una ejecución de cierre en la **misma cuenta** que
abrió, o el stop en `Filled`. Este error dio por saltado un stop a 7 segundos de la entrada y
abandonó tres posiciones vivas (70.000 EUR acumulados en tres runs).

### `reqExecutions` no va en el bucle caliente
Es pesada y sujeta a *pacing*. Una cada 20 s durante una hora la hizo **dejar de responder tres
veces**, bloqueando el bucle **43 s, 18,5 min y 16,1 min**. Con el bucle parado no se mide MAE ni
se comprueba la posición, y las barreras temporales se evalúan tarde. Sembrar **una vez** y leer
de `ib.fills()`, que se rellena por *push* con cada `execDetails`.

### El neto de ejecuciones no es la posición
Sirve para **enrutar** (dice qué subcuenta operó), no para saber si hay posición. Es un contador
del día: no ve una posición heredada de ayer. **Las posiciones mandan**; las ejecuciones solo
valen como posición si las fuentes de posición **no respondieron**.

### `reqPositions()` devuelve más filas que `ib.positions()`
El wrapper descarta las de tamaño cero. Una posición **cerrada** y una **nunca vista** son
indistinguibles en la caché.

### Datos retrasados envenenan `Position`
Con `IB_MARKET_DATA_TYPE=3` los bid/ask llegan `nan`, y `Decimal(str(nan))` produce
`Decimal('NaN')`, que los `is None` del Portfolio **no atrapan**. Descartar ticks no finitos.
IDEALPRO no publica datos retrasados: pedir tiempo real (`1`) para divisas.

### Redondeo al tick
`round(px/tick)*tick` con `tick=5e-05` da `1.0862500000000001` y **TWS rechaza la orden**. Hay
que redondear otra vez a los decimales del tick, y sacarlos con `Decimal(str(tick)).as_tuple()
.exponent`, no con `log10` (`log10(5e-05) = −4,30` redondea a 4 y deja stops inválidos).

### Otros
- `whatToShow="TRADES"` devuelve **vacío** para divisas: usar `MIDPOINT`. Las barras MIDPOINT
  traen `volume = -1`.
- El bracket se manda padre `transmit=False` + hijo `transmit=True`: no existe el instante con
  posición abierta y sin stop.
- Cancelar **antes** de aplanar; al revés el stop queda huérfano y puede abrir posición contraria.
- `qualifyContracts` actualiza el contrato **in place**.
- `front_month(ib, symbol, exchange, currency, min_days=2)` para futuros; `Forex(pair)` +
  `qualifyContracts` para contado (no tiene cadena de vencimientos).
- El multiplicador y el `minTick` se leen de `contract.multiplier` / `detail.minTick`, **nunca**
  se hardcodean.

---

## 5. Ficheros

| Ruta | Qué es |
|---|---|
| `qsCrypto/notebooks/MeanReversion_IBKR_CME_Bitcoin.ipynb` | El backtest. `run_turtle` en la celda 24 (22,7 KB) es el motor; `FADE()` en la 27 es la config autoritativa |
| `qsCrypto/notebooks/Turtle_MR_Intraday_Live.ipynb` | El vivo, sobre el bus de eventos |
| `qsCrypto/notebooks/Turtle_IBKR_Live_MAE.ipynb` | Parte A: sesión manual de un trade con MAE, con el interruptor `INSTRUMENT` (MBT / EURUSD para fuera de horario) |
| `qsCrypto/portfolio/mae.py` | `Excursion` y compañía: la definición **autoritativa** de MAE/MFE, en float |
| `qsCrypto/portfolio/position.py` | Contrapartida en `Decimal` de lo anterior, para el camino de ticks en vivo. **No se importan entre sí** |
| `qsCrypto/ibkr/{config,contracts,check_connection}.py` | Fontanería de conexión; `check_connection.py` demuestra todas las llamadas necesarias |
| `data_cache/ibkr/30mins/MBT_chain.csv` | Las barras del backtest, para tests de réplica offline |
| `Sun_Valley/MAE_wiring_plan.md` | El plan de diseño del MAE y la estructura de cuatro barreras |

**Naming: `mae.Excursion` frente a `Position`** — `anchor0`/`entry_anchor`, `anchor`/(sin
equivalente), `r0`/`r_unit`, `mae_px`/`mae_price`, `mae_r` (propiedad)/`calculate_mae_r()`
(método), `update_tick`/`update_position_price`, `reanchor`/`reanchor_entry`.
