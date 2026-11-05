# Graveyard — lo que no funcionó, y qué se aprendió midiéndolo

> Cada cifra de este documento viene de una celda ejecutada de
> `qsCrypto/notebooks/Turtle_IBKR_CME_Bitcoin.ipynb`, de
> `qsCrypto/notebooks/Digital_Turtles.ipynb`, o de un *probe* reproducible sobre la caché de
> `data_cache/ibkr/30mins/`. No hay ningún número estimado a ojo.
>
> Instrumento: futuros de bitcoin del CME (`MBT` 0.1 BTC, `BRR` 5 BTC) vía IBKR paper,
> front month resuelto programáticamente, `useRTH=True`, recortado al solape 08:30–15:15 CT.
> Muestra: 4.187 barras de 30 min comunes a los dos contratos, ≈1.33 años.

---

## 1. El compromiso de granularidad: por qué 30 min y no 1 min

La identidad que decide la granularidad operable, y que **no depende del capital**:

```
nocional/equity = RISK_PER_UNIT × precio / N
```

Con `RISK_PER_UNIT = 1%`, BTC ≈ 95.000 y un tope de apalancamiento bruto de 4×, hace falta
**N ≥ 237 puntos**. Medido sobre el front month con RTH:

| TF | N (MBT) | rango de vela / N | coste round-trip / R (MBT) | (BRR) | nocional / equity | |
|---|---|---|---|---|---|---|
| 1 min | 28.8 | 2.15 N | **78%** | — | **27.7×** | inviable |
| 5 min | 91.2 | 1.27 N | 25% | 11% | 8.8× | inviable |
| 15 min | 247.0 | 0.50 N | 9% | 4% | 3.2× | al límite |
| **30 min** | **424.1** | **0.48 N** | **5%** | **3%** | **1.9×** | **elegida** |
| 1 hour | 684.2 | 0.34 N | 3% | 2% | 1.2× | |
| 4 hours | 1.143.7 | 0.34 N | 2% | 1% | 0.7× | |
| 1 day | 2.155.7 | 0.23 N | 1% | 1% | 0.4× | |

**Descartado: 1 minuto.** El coste de ida y vuelta se come el **78% de la R**. Ninguna tasa de
acierto realista sobrevive a eso. Y no se arregla con el tamaño: el coste por contrato y el
riesgo por contrato escalan igual, así que el cociente es invariante. Solo se arregla subiendo N,
es decir engrosando la barra.

**Descartado: afinar para "ver mejor" la excursión.** Es lo contrario de lo que ocurre. El rango
típico de vela pasa de 0.48 N a 30 min a **2.15 N a 1 min**, porque el True Range de un minuto lo
domina el ruido de tick y la EMA-20 queda pequeña frente a las colas. A 1 min un stop de 1N cae
*dentro* del rango de una sola vela: casi toda salida sería ambigua. Un umbral de MAE por debajo
del rango típico de vela no es una regla, es ruido con nombre.

**Lo que sí arregló `useRTH=True`.** Las velas planas del feed demo (rango cero y volumen cero,
que tienen excursión nula por construcción y por tanto sesgan la MAE a la baja):

| | Sin RTH | Con RTH |
|---|---|---|
| MBT, 1 min, 5 días | 33% | **0.8%** |
| BRR, 1 min, 5 días | 36.5% | **6.6%** |

---

## 2. La ficción de entrar al precio del canal

El sesgo más caro de todos, y el más fácil de no ver. Una ruptura que **abre** ya pasada el nivel
del canal no se llena en el nivel: se llena en la apertura, peor. El motor original rellenaba al
nivel del canal siempre.

Medido sobre MBT, 30 min, RTH, 234 rupturas alcistas:

| | |
|---|---|
| Rupturas que **abren ya por encima** del canal | **59 / 234 = 25.2%** |
| Deslizamiento no modelado | mediana **1.21 N**, media 2.24 N, p90 2.330 pts |
| De esos huecos, primera barra de la sesión | **68%** |

Como `R = 1N`, la mediana equivale a **más de 1R de ventaja ficticia por entrada**. Y el 68% son
el hueco nocturno, así que operar solo RTH lo convierte en **estructural**, no anecdótico. El
mismo sesgo estaba en los *adds*, donde con la escalera de 4 unidades se paga cuatro veces.

Efecto de corregir ambos:

| Configuración | Antes (con la ficción) | Después (honesto) |
|---|---|---|
| Legado: stop 1N + canal Donchian 20 | +264.8% / Sharpe **3.03** | **−32.4% / −0.98** |
| Triple barrera con MAE\* calibrado | +139.4% / **3.21** | **−0.4% / −0.01** |
| Triple barrera, umbral fijo declarado | +139.1% / **3.55** | −30.5% / −0.81 |
| Triple barrera, stop 1N pleno | +129.1% / **3.13** | −14.0% / −0.30 |

**Todo el edge aparente eran esos dos sesgos.** Un Sharpe de 3.5 sobre 1.33 años de datos de 30
min tendría que haber sido sospechoso desde el principio; lo fue, y por eso se buscó.

**Descartado: la barra de confirmación como arreglo.** Exigir que la barra siguiente **cierre**
fuera del canal, y entrar a ese cierre, elimina el hueco por completo — el fill pasa a ser un
precio que realmente se negoció:

| | Ruptura inmediata | Con confirmación |
|---|---|---|
| Señales → trades | 276 → 276 | 586 → **157** |
| Tasa de confirmación | — | **27%** |
| Entradas con hueco | **32%** | **0%** |
| CAGR / Sharpe | −14.0% / −0.30 | −25.1% / **−0.77** |

Arregla la honestidad del precio de entrada, que es lo importante, pero **empeora el resultado**:
entrar un cierre más tarde cuesta más de lo que ahorra filtrar rupturas falsas. Con `R = 1N`, esa
barra extra se come una fracción grande de la R antes siquiera de empezar.

---

## 3. Calibración de MAE bajo tres regímenes de stop

El stop con el que se **observa** trunca lo que se puede **ver**: con un stop apretado, la MAE de
los ganadores no puede superarlo por construcción, el cuantil se pega al propio nivel y uno acaba
midiendo su propio stop en lugar del mercado.

Población: trades cerrados solo por barrera vertical de 10 barras, sin objetivo ni canal.

| Régimen | R | Trades | Ganadores | Sobreviven a la vertical | MAE\* (N) | **MAE\* (R)** | KS |
|---|---|---|---|---|---|---|---|
| sin stop | 1N | 164 | 56 (34%) | 100% | 2.10 | 2.10 | 0.39 |
| **stop 2N** | 2N | 165 | **45 (27%)** | **55%** | **1.38** | **0.69** | 0.55 |
| stop 1N | 1N | 177 | 28 (16%) | 15% | 1.00 | 1.00 | 0.60 |

- El **sesgo de truncamiento** es un factor **2.1×** en `MAE*` según con qué stop se mire. No es
  un matiz: es el orden de magnitud del propio estadístico.
- Ensanchar el stop a 2N **multiplica por 1.6 los ganadores** (28 → 45) y por 3.7 la supervivencia
  a la vertical (15% → 55%). Ésa es la muestra con la que se puede calibrar.
- **La MAE sí discrimina:** `P(ganador | MAE ≥ x)` cae de forma monótona del 35% (tasa base) al
  10.5%, con separación KS de 0.55.
- Pero **`MAE* = 1.38 N`, por encima del stop de 1N**. Con `R = 1N` **no existe cierre temprano
  posible**: el stop dispara antes que el umbral. Con `R = 2N` el mismo umbral vale **0.69 R** y
  sí cabe, con banda resoluble `[0.38 R, 1.00 R)`.

**Descartado: el umbral de MAE en 0.60 N.** Queda por debajo del suelo de resolución intrabarra
medido (0.76 N, el rango mediano de vela a 30 min). Un toque más fino que una vela no es
observable dentro de esa vela.

**Descartado: cortar antes con `R = 1N`.** El 34% de los trades que acaban ganando sufre más de
1N de excursión adversa antes de resolverse. El stop de 1N ya está cortando ganadores; apretarlo
más solo acelera la sangría. **Los datos no piden cerrar antes: piden un stop más ancho.**

Y el dato que apunta al problema real: **E-ratio = 0.83–0.90 < 1**. El MFE medio es menor que el
MAE medio, o sea que tras entrar en la ruptura el precio da de media más en contra que a favor.
Eso es un problema de *timing de la entrada*, no de colocación del stop, y ningún ajuste del stop
lo arregla.

---

## 4. El aprendizaje: a intradía el comportamiento es reversivo

Tres mediciones independientes, todas dentro de la misma muestra, apuntan a lo mismo:

1. **E-ratio < 1** — la entrada en ruptura no tiene ventaja direccional inmediata.
2. **Solo el 27% de las rupturas confirma** — casi tres de cada cuatro perforaciones del canal no
   consiguen cerrar fuera en la barra siguiente.
3. **El Turtle pierde en las cuatro configuraciones** a 30 min, una vez quitados los dos sesgos.

Y midiendo el *fade* directamente —entrando en contra al open de la barra siguiente a la ruptura—
sobre la misma caché de 30 min:

| | Fade de ruptura alcista (corto) | Fade de ruptura bajista (largo) |
|---|---|---|
| n | 234 | 287 |
| P(el fade gana) a +1 / +2 / +4 / +10 barras | 54% / 52% / 51% / **56%** | 53% / 52% / 53% / 52% |
| Retorno **medio** del fade a +10 barras | **−0.160 N** | **+0.117 N** |
| Retorno **mediano** a +10 barras | +0.392 N | +0.128 N |

La mediana es positiva en los dos lados y la probabilidad supera el 50% en todos los horizontes.
Pero **la media del fade corto es negativa**: gana más veces y pierde más cuando pierde. Hay
reversión, y es medible, pero es **débil y asimétrica** — y la asimetría bien puede ser
simplemente la deriva alcista del bitcoin en el periodo, no una propiedad estable del mercado.

### El contraste con el horizonte diario, citado entero

`Digital_Turtles.ipynb` corre el **mismo** sistema Turtle sobre **velas diarias**, 44 perpetuos de
cripto del índice COIN50, 2021-2026. Su backtest de parámetros fijos sí produce una curva
positiva (celda 20, salida ejecutada):

| | Turtle Sys2 (55/20) diario | B&H BTC |
|---|---|---|
| CAGR | **60.74%** | 15.32% |
| Sharpe | **0.96** | 0.27 |
| Max DD | −51.7% | −76.7% |
| Trades / win rate | 503 / 22.9% | — |
| Payoff (W/L) | 6.97 | — |

Es tentador leer eso como "el Turtle sobrevive en diario y muere en intradía". **Pero hay que
citarlo entero**, porque su propia auditoría lo rechaza (celda 49, salida ejecutada):

> `1/8 criterios superados`
> `→ NO despliegues capital. El backtest no soporta el peso.`

Los motivos, todos de salidas ejecutadas: apuestas efectivas **2.4** sobre 44 activos (correlación
media 0.63, PC1 = 64% de la varianza); los **3 mejores trades son el 70.7%** del P&L total;
probabilidad de ruina (DD ≥ 50%) del **52.9%** en Monte Carlo; y el walk-forward fuera de muestra
en **−34.9%** de CAGR, con el veredicto `→ Diferencia NO significativa: la optimización es ruido`.

**La advertencia metodológica, que importa más que la comparación:** ninguno de los dos notebooks
compara granularidades de forma controlada. Difieren en universo (44 perpetuos de cripto frente a
MBT/BRR), periodo (2021-2026 frente a 2025-2026), sede, horario y microestructura. Que el diario
dé positivo y el de 30 min negativo es una **observación**, no un experimento — no prueba que el
diario sea mejor, y `Digital_Turtles.ipynb` tampoco contiene ningún test de horizonte que lo
sostenga (lo argumenta en prosa, no lo mide).

Lo que **sí** está medido dentro de una sola muestra, con un solo instrumento y un solo periodo,
es el E-ratio < 1 a 30 min. Eso es lo que sostiene la hipótesis reversiva, y es lo único que se
usa para justificar el siguiente experimento.

---

## 5. Ideas descartadas, con su motivo

| Idea | Por qué se descartó |
|---|---|
| Granularidad de 1 min para el backtest | Coste/R del 78% y nocional 27.7× sobre un tope de 4× |
| Doble timeframe (N grueso, camino a 1 min) | Descartado por diseño: todo a 30 min, una sola serie |
| Umbral de MAE en 0.60 N | Por debajo del suelo de resolución de 0.76 N — irresoluble |
| Cierre temprano por MAE con `R = 1N` | `MAE* = 1.38 R` cae fuera del stop; no hay banda posible |
| Barra de confirmación en la ruptura | Arregla el hueco, pero el Sharpe empeora (−0.30 → −0.77) |
| TP en la banda opuesta con vertical de 10 b | El canal mide 9.9 N; solo se toca el **0.9–1.4%** |
| BRR junto a MBT como "diversificación" | Correlación **1.0000**, `n_eff` = 1.00 — mismo subyacente |
| Optimizar el Sharpe a secas en el walk-forward | Selecciona el parámetro más apalancado; se penaliza por drawdown |

---

## 6. Siguiente hipótesis: invertir el lado de la señal

Si a intradía las rupturas fallan de forma sistemática, el lado correcto de esa operación es el
contrario. La hipótesis se prueba en un notebook aparte
(`qsCrypto/notebooks/MeanReversion_IBKR_CME_Bitcoin.ipynb`), copia de éste, cambiando **solo el
lado de la señal en el motor**:

| | |
|---|---|
| **Señal** | Ruptura del canal Donchian-55 en la barra `t` |
| **Entrada** | Al **OPEN de la barra `t+1`, en dirección contraria**, sin condición de confirmación |
| **Stop** | 2N ⇒ **R = 2N** (el ancho que la calibración de §3 pedía) |
| **Sizing** | `Unit_Qty = RISK × Equity / (STOP_N × N × mult)` — por **R**, no por N |
| **Take profit** | **Punto medio del canal**, congelado en la entrada |
| **Vertical** | 10 barras |
| **Adds** | Cada ½N a favor, hasta que el precio cruce el medio; máx. 4 unidades |
| **Lados** | Simétrico, con métricas reportadas por lado |

**Por qué el TP va en el medio y no en la banda opuesta.** El canal Donchian-55 mide **9.9 N** de
ancho (mediana; p25 8.2, p75 12.2), así que la banda opuesta está a casi 10 N de la entrada. Tasas
de toque medidas dentro de las 10 barras de la vertical:

| Barrera alcanzada en 10 barras | Fade alcista | Fade bajista |
|---|---|---|
| **TP en el medio del canal** (−4.9 N) | **10%** | **7%** |
| Stop 2N | 29% | 40% |
| Vertical | **61%** | **53%** |
| *TP en la banda opuesta (−9.9 N) — descartado* | *0.9%* | *1.4%* |

Con el TP en la banda opuesta el objetivo es decorativo: se toca una vez de cada cien. En el medio
del canal se toca **entre 7 y 10 veces más**, y a 4.9 N frente a un stop de 2 N el ratio
riesgo/recompensa es **2.4 : 1** — pero conviene no maquillarlo: **la salida dominante sigue
siendo el vencimiento** (53–61%). Con una vertical de 10 barras, un objetivo a 4.9 N está lejos.

> **Corrección.** Una primera versión de esta tabla decía 32% y 29% de toques del medio. Estaba
> mal: el *probe* contaba **toques por barra** en lugar de por trade (incrementaba el contador sin
> cortar el bucle), lo que infla la cifra unas 3×. El motor daba 8–9% y la discrepancia obligó a
> revisar los dos; el equivocado era el probe. Las cifras de arriba son las corregidas, y
> coinciden con lo que reporta el motor.

**Por qué el sizing pasa a ir por R.** Mover el stop a 2N sin tocar nada más quintuplica el riesgo:
con `Unit_Qty = 1%·E/N` y la escalera de ½N, el riesgo agregado a plena carga sube a **5 N = 5%
del equity** (fills en 0, ½N, 1N, 1½N con el stop común en −½N: 0.5 + 1.0 + 1.5 + 2.0 = 5.0 N).
Dimensionando por R vuelve a **1% por unidad y 2.5% a plena carga**, y —lo que importa para poder
comparar— deja el Sharpe en la misma escala de riesgo que este notebook.

**Lo que hay que vigilar en ese experimento**, escrito antes de correrlo: la reversión medida es
débil (media del fade corto **negativa**), la muestra es de ~1.33 años y un solo subyacente, y la
asimetría entre lados puede ser deriva. Si el resultado sale bueno, el primer sospechoso es otra
vez un sesgo de ejecución, no un hallazgo.

### Primer resultado (preliminar): la inversión tampoco funciona

> **SUPERADO — ver §7.** Este resultado se corrió con stop de **2N**, y el 2N era el
> problema, no el lado de la señal. Con stop de 1N el mismo sistema pasa a **+8.9% de CAGR**
> (canal 55b) y **+58.7%** (canal 20b) sobre la misma muestra y la misma barrera vertical.
> Se conserva la sección porque el error de atribución es el aprendizaje.

Ejecutado el notebook con la configuración de arriba, sobre la misma muestra de 4.187 barras:

| Sistema (misma triple barrera, mismo sizing por R) | Trades | CAGR | Sharpe | Win rate |
|---|---|---|---|---|
| **Reversión** (fade, entrada al open siguiente) | 218 | **−33.5%** | **−1.09** | 36% |
| Ruptura (mismo motor, `signal_mode="breakout"`) | 348 | −6.5% | −0.66 | — |

**Invertir el lado lo empeora**, no lo mejora. La reversión que se midió a nivel de retornos
brutos (P(fade gana) 51–56%) no sobrevive al pasar por una estructura de barreras con costes.

Y el diagnóstico de por qué, que sale de §8.6 del propio notebook: **la escalera de ½N pelea
contra un objetivo lejano**. Cada add sube todos los stops a 2N del fill más reciente, así que con
4 unidades cargadas el stop queda a **0.5 N de la entrada** — un cuarto de su anchura original —
mientras el objetivo sigue a 5 N. Aislando el efecto:

| Escalera | Unidades medias | TP | Stop | Vertical | CAGR | Sharpe |
|---|---|---|---|---|---|---|
| Sin adds (`MAX_UNITS=1`) | 1.00 | 9% | 40% | 51% | **−14.9%** | −0.82 |
| 2 unidades | 1.67 | 8% | 45% | 47% | −28.3% | −1.07 |
| 4 unidades | 2.28 | 8% | 47% | 45% | −33.5% | −1.09 |

Cada unidad añadida empeora el resultado de forma monótona: el trailing a 2N convierte en stops
trades que sin escalera habrían llegado al vencimiento o al objetivo. **La escalera Turtle está
diseñada para tendencias que se extienden, no para un objetivo fijo a media anchura de canal.**

Queda pendiente el walk-forward con embargo; el recuento de ensayos está en §10. Antes de dar esto
por cerrado, pero la dirección del resultado no es ambigua.

---

## 7. El stop de 2N: lo que en realidad mató a la reversión

> **Nota de vigencia.** Las tablas de esta sección se midieron con la regla de entrada
> anterior (al open de `t+1`, sin confirmación). La regla vigente se describe en §9, que
> repite el barrido del stop: el patrón y la conclusión son los mismos, con la esperanza
> del óptimo subiendo de +0.186 R a +0.375 R.

La sección anterior daba la inversión por muerta. La atribución era incorrecta: lo que hundía al
sistema no era el lado de la señal sino el **stop de 2N**, adoptado para que el umbral de MAE
cupiera dentro de la R. Cambiando solo ese parámetro el resultado se invierte.

Muestra de referencia de esta sección: MBT + BRR, 30 min, RTH, **4.062 barras comunes**
(2025-05-05 → 2026-08-28), barrera vertical de 10 barras, sizing por R (1% del equity por unidad).

### El experimento de atribución

| Sistema | Stop | Trades | CAGR | Sharpe |
|---|---|---|---|---|
| **Reversión 20b (tesis)** | **2N** | 347 | **−20.5%** | **−0.54** |
| **Reversión 20b (tesis)** | **1N** | 467 | **+54.9%** | **+1.19** |
| Reversión 55b | 2N | 209 | −32.6% | −1.11 |
| Reversión 55b | 1N | 285 | +4.2% | +0.09 |

El stop explica el giro entero. El veredicto de "la inversión tampoco funciona" era correcto sobre
la configuración que se corrió, e incorrecto como conclusión sobre la hipótesis.

### La causa: lo que la MAE no mide

Barrido del stop con el **sizing acompañando**, de modo que cada unidad arriesga siempre el 1% del
equity y la comparación no confunda "stop más ancho" con "más dinero en riesgo". R = riesgo de la
primera unidad. Sistema de la tesis (canal de 20 barras):

| Stop | Trades | Win rate | **Ganador medio (R)** | **Perdedor medio (R)** | Esperanza (R) | CAGR | Sharpe |
|---|---|---|---|---|---|---|---|
| 0.75 N | 501 | 24% | **6.09** | −1.75 | +0.139 | +44.2% | 0.91 |
| **1.00 N** | 467 | 28% | **5.13** | −1.72 | **+0.186** | **+54.9%** | 1.19 |
| 1.25 N | 435 | 34% | 4.02 | −1.81 | +0.178 | +51.8% | **1.19** |
| 1.50 N | 402 | 37% | 3.26 | −1.80 | +0.061 | +14.6% | 0.33 |
| **1.55 N ← MAE\*** | 392 | **38%** | **3.12** | −1.78 | +0.073 | **+17.4%** | **0.42** |
| 2.00 N | 347 | 42% | 2.13 | −1.61 | −0.056 | −20.5% | −0.54 |
| 2.50 N | 311 | **45%** | **1.61** | −1.23 | +0.039 | +8.7% | 0.32 |

El mecanismo es directo:

- **La MAE cumple lo que promete.** Ensanchar el stop de 1N a 2.5N sube el win rate del 28% al
  45%: se retienen más ganadores.
- **El ganador medio cae de 5.13 R a 1.61 R** mientras el perdedor medio se mantiene entre −1.2 y
  −1.8 R. Los ganadores que el stop ancho rescata son los marginales: los que necesitan 1.5 N en
  contra para resolverse valen 2–3 R; los que nunca se van 1 N en contra valen 5–6 R.
- La esperanza por trade cae de +0.186 R a −0.056 R en el umbral de 2N.

`MAE*` es el percentil 90 de la MAE de los ganadores **contados uno a uno**: una función de la
cuenta, no de la suma de pagos. Da el mismo peso a un ganador de 0.3 R que a uno de 14 R. En esta
muestra el tamaño del ganador está correlacionado a la baja con su MAE.

### Cuántas barras hace falta para atravesar el canal

Medido sobre las mismas 4.062 barras, con la anchura del canal y el rango de vela en unidades de N.
Tres estimaciones del mismo recorrido: la cota balística (cada vela avanzando entera en el mismo
sentido, sin solape), la cota de paseo aleatorio ((anchura/σ)², con σ la desviación típica del
retorno cierre-a-cierre por barra) y la medición empírica de primer paso desde cada barra.

**Canal de 20 barras — el sistema de la tesis**

| | |
|---|---|
| Anchura mediana | **5.86 N** (p25 4.71, p75 7.35) |
| Rango mediano de vela | **0.76 N** |
| Cota balística | **7.7 barras** |
| Cota de paseo aleatorio (σ = 0.999 N por barra) | **34 barras** |
| **Empírico, primer paso en cualquier sentido** | **mediana 29 barras** (p25 14, p75 63) |
| P(atravesarlo en ≤ 10 barras) | **19.0%** |

El recorrido observado (29 barras) está mucho más cerca de la cota de paseo aleatorio (34) que de
la balística (7.7): el precio no recorre el canal de forma direccional, lo atraviesa por difusión.
Un sistema que necesite el recorrido completo dentro de la barrera vertical de 10 barras lo
consigue una de cada cinco veces.

**Canal de 55 barras**

| | |
|---|---|
| Anchura mediana | **10.01 N** (p25 8.30, p75 12.18) |
| Cota balística | 13.2 barras |
| Cota de paseo aleatorio | 103 barras |
| **Empírico** | **mediana 85 barras** (p25 38, p75 157) |
| P(≤ 10 barras) | **2.8%** |

Ésta es la medición que respalda el descarte de la banda opuesta como objetivo (§5 de este
documento): 85 barras de mediana frente a una barrera vertical de 10. El 2.8% de aquí es el mismo
hecho que el 0.9–1.4% de toque medido sobre trades reales; aquella cifra era condicional a haber
entrado en una ruptura y con el stop activo.

**Medio canal — el objetivo que sí opera el sistema**

| Canal | Distancia | Mediana (cualquier sentido) | P(≤10 b) | Direccional: mediana / P(≤10 b) |
|---|---|---|---|---|
| 20 b | 2.93 N | **10 barras** | 54.2% | 18–20 b / **27–29%** |
| 55 b | 5.01 N | 23 barras | 23.9% | 39–42 b / 11–12% |

En el canal de 20 barras la mediana del tiempo de primer paso a medio canal es **exactamente 10
barras**, que es la barrera vertical. El objetivo está colocado en la mediana de la distribución.

Dos matices sobre esa cifra:

- El 54.2% cuenta el primer paso en **cualquiera de los dos sentidos**. El trade cobra en uno solo,
  y la versión direccional baja a **27–29%**.
- Ese 27–29% es el techo sin stop. Con el stop de 1N por delante, el motor reporta un 17% de
  toques efectivos del objetivo (40 `target` + 41 `gap_target` sobre 467 trades). La diferencia
  entre 28% y 17% es lo que el stop intercepta.

**Sobre las colas.** σ por barra sale **0.999 N** mientras el rango mediano de vela es 0.76 N. Que
la desviación típica del cierre-a-cierre supere al rango típico intrabarra indica colas gruesas —
los huecos entre sesiones, que con RTH quedan dentro de la muestra. Es la misma cola que produce
el 25.5% de stops ejecutados con hueco que se mide más abajo.

### El balance contable estático no decide esto (y por qué)

Un intento natural de cuantificar la decisión es contar, sobre la población de calibración con
stop 2N, qué ganadores sacrifica el stop de 1N y qué perdedores recorta. **Ese cálculo no es
concluyente**, y conviene dejar escrito por qué antes de que alguien lo repita.

Sobre el canal de 20 barras (348 trades, 145 ganadores), separando los tres tramos que un stop de
1N y uno de 1.55 N tratan de forma distinta:

| Tramo | Trades | Observado (stop 2N) |
|---|---|---|
| MAE < 1.00 N — idénticos bajo ambos stops | 145 | +$239.021 |
| **Banda [1.00 N, 1.55 N) — el stop ancho los rescata** | **55** | **−$24.391** (16 gan. +$20.491 / 39 perd. −$44.882) |
| Cola MAE ≥ 1.55 N — parados bajo ambos stops | 148 | −$226.311 |

Los 55 trades de la banda son los que el stop de 1N corta y el de 1.55 N deja correr: rescatarlos
produce −$24.391, o sea que **rescatarlos pierde dinero**. Pero cerrar el argumento exige valorar
qué habrían hecho bajo el otro stop, y ahí el cálculo estático se rompe por tres motivos:

1. **Las unidades no son las mismas.** Un trade que se va 1.2 N en contra, bajo un stop de 1N ya
   estaría cerrado y nunca habría cargado las unidades que se le observan bajo 2N. Usar el número
   de unidades observado para valorar el contrafactual infla el riesgo de escalera atribuido al
   stop estrecho.
2. **La población no es la misma.** Con 1N hay 467 trades y con 1.55 N hay 392: cerrar antes
   libera capital y la señal siguiente genera un trade distinto. Los tramos no son comparables
   uno a uno.
3. **El camino del equity no es el mismo**, y el sizing va sobre equity marcado.

Aplicando el recorte de escalera a la cola y a la banda, el saldo estático sale **a favor del stop
ancho** (+$27.534), en contradicción directa con lo que hace el motor al re-correrlo. La
contradicción no es un misterio: es el sesgo del punto 1.

**La evidencia que sí decide es el barrido dinámico**, porque re-corre el sistema entero con cada
stop en lugar de reetiquetar una población fija. Y es inequívoco: esperanza por trade de
**+0.186 R con 1N** frente a **+0.073 R con 1.55 N** y **−0.056 R con 2N**, con el ganador medio
cayendo de 5.13 R a 2.13 R mientras el perdedor medio se mantiene entre −1.2 y −1.8 R.

*(Corrección: una versión anterior de esta sección presentaba un balance estático de +181.5 R,
razón 5.8:1, a favor del stop estrecho. Estaba mal construido: atribuía al stop ancho la pérdida
íntegra de los perdedores de la cola, cuando un stop de 1.55 N también los corta —solo que 0.55 N
más tarde—, y no contabilizaba los ganadores rescatados en la banda. La conclusión sobre qué stop
operar no cambia, pero el número no la sostenía.)*

### El "1% por trade": la aritmética es correcta, el promedio no la contradice

Con adds cada ½N y el stop común recolocado a `m`·N del fill más reciente, el riesgo de una
posición plenamente cargada no es la suma de los riesgos individuales de entrada: las primeras
unidades ya se han valorizado y el stop común queda por encima de sus entradas. Con `m = 1` y
cuatro unidades (fills en E0, E0+½N, E0+N, E0+1½N; stop común en E0+½N):

| Unidad | Entrada | Entrada − stop | Riesgo |
|---|---|---|---|
| U1 | E0 | −0.5 N | **−0.5%** (bloquea ganancia) |
| U2 | E0 + 0.5 N | 0 | 0% |
| U3 | E0 + 1.0 N | +0.5 N | +0.5% |
| U4 | E0 + 1.5 N | +1.0 N | +1.0% |
| | | **+1.0 N · q** | **1% del equity** |

Generalizando a `k` unidades cargadas, adds a ½N y stop a `m`·N del último fill, con `q·N = 1%·E`:

- **Sizing canónico** (`size_on="n"`, `q = 1%·E/N`, independiente de `m`):
  `Riesgo(k) = [k·m − k(k−1)/4] × 1%`. A plena carga, **(4m − 3) × 1%**.
- **Sizing por R** (`size_on="r"`, `q = 1%·E/(m·N)`, el que corre este notebook):
  `Riesgo(k) = k − k(k−1)/(4m)` en unidades de R. A plena carga, **4 − 3/m** — 1 R con m=1,
  2.5 R con m=2.

Contrastado contra el blotter del sistema de la tesis (467 trades, stops sin hueco, `m = 1`):

| Unidades | Teoría | Mediana medida | n |
|---|---|---|---|
| 1 | −1.00 R | **−1.018 R** | 118 |
| 2 | −1.50 R | **−1.519 R** | 80 |
| 3 | −1.50 R | **−1.502 R** | 33 |
| 4 | −1.00 R | **−0.968 R** | 12 |

La fórmula se cumple en las cuatro cargas. **La mediana de todos los perdedores es −1.18 R.**

El promedio (−1.72 R) no lo produce la escalera:

- La escalera es **no monótona** —1.0, 1.5, 1.5, 1.0 R— y con la mezcla de unidades observada pesa
  **−1.23 R**. El máximo está en 2–3 unidades, no a plena carga.
- El resto viene de los **stops ejecutados con hueco**: 86 de 337 perdedores (**25.5%**), pérdida
  media **−3.38 R**, que aportan **−0.86 R** de los −1.72 R totales — la mitad del promedio la
  produce un cuarto de los trades.
- El hueco escala con la carga, porque la escalera aprieta el stop: −2.38 R con una unidad,
  **−9.85 R con cuatro**.

El "1% por trade" es exacto como diseño y se cumple en la mediana. Lo que lo rompe es el riesgo de
hueco, que es riesgo de ejecución y no de dimensionamiento: no se corrige con el sizing sino con
la colocación de la orden de stop.

### Asimetría por lado

| Lado | Señal | Trades | P&L | Win rate | P&L medio (R) | Unidades |
|---|---|---|---|---|---|---|
| CORTO | fade de ruptura alcista | 213 | +$45.402 | 26.8% | +0.224 | 1.911 |
| LARGO | fade de ruptura bajista | 254 | +$36.785 | 28.7% | +0.154 | 1.913 |

Con unidades medias prácticamente iguales, los dos lados aportan P&L positivo y la diferencia
entre ellos es menor que en la configuración de 2N. No se usa para seleccionar lado: con 1.31 años
de un solo subyacente la diferencia no es separable de la deriva del bitcoin en el periodo.

### Lo que esto no autoriza a concluir

- **El walk-forward OOS está en −26.6% de CAGR** (Sharpe −0.49). El +54.9% es dentro de muestra.
- El checklist de validez marca **5 de 11** criterios.
- La probabilidad Monte Carlo de un DD ≥ 50% es del **36.6%**.
- Los 3 mejores trades son el **65.7%** del P&L; sin el mejor, el resultado sigue positivo
  (+$60.782 sobre +$82.187).
- Solo el **46%** de las señales fue ejecutable con contratos enteros a este capital.
- El barrido del stop es **un ensayo más** a contar en el DSR/PBO — hecho en §10, y el DSR
  sale en 0.374. Que el óptimo caiga en
  1.00–1.25 N y no en un extremo, y que la curva sea monótona a ambos lados, reduce la sospecha
  pero no sustituye al recuento de ensayos.

La conclusión defendible es la negativa: calibrar el stop por el percentil 90 de la MAE de los
ganadores **empeora el sistema** de forma medida y reproducible, porque optimiza una cuenta de
ganadores en lugar de una suma de pagos. Un stop duro en 1N domina a la calibración probabilística
en esperanza, Sharpe, CAGR y máxima caída.

### Defecto conocido en el panel 6 de §8.5

El barrido de sensibilidad que dibuja el notebook mueve `mae_stop_n` dejando el sizing anclado en
`STOP_N`. Eso mezcla dos efectos: al ensanchar la barrera también se arriesga más dinero por
trade. La dirección se mantiene en las dos lecturas, pero la magnitud no:

| Barrera | CAGR (sizing anclado) | CAGR (riesgo normalizado) |
|---|---|---|
| 0.75 N | +19.7% | +44.2% |
| 1.00 N | +54.9% | +54.9% |
| 1.50 N | −0.9% | +14.6% |
| 2.00 N | −38.6% | −20.5% |

Las cifras de riesgo normalizado de este documento son las interpretables como efecto del umbral.

---

## 8. La fragilidad de la muestra: el sesgo más grande medido

> **Nota de vigencia.** Este diagnóstico corresponde a la regla de entrada anterior (sin
> confirmación). Con la regla de §9 el sesgo cae de 42.3 pp a 5.2 pp. Se conserva porque
> explica POR QUÉ la confirmación estabiliza el sistema, no solo que lo mejora.

Punto de partida: al cambiar `SYMBOLS` de `["MBT","BRR"]` a `["MBT"]`, el CAGR se desploma. La
sospecha razonable era que el motor estuviera sumando dos posiciones pese a que las validaciones
previas declaran BRR no operable con este capital. **No es eso.**

### BRR no opera — con la configuración por defecto

En el blotter de 467 trades, los 467 son de MBT y el P&L por símbolo es MBT $82.187 / BRR $0. El
rechazo funciona: con multiplicador 5.0 una unidad de BRR sale a 0,5 contratos y `floor(qty) = 0`.

**Con un matiz que importa.** El rechazo depende del *equity*, no solo del contrato. La regla de
drawdown ancla el sizing en `equity0` mientras la cuenta crece, así que BRR nunca llega a caber.
Desactivándola (`use_dd_rule=False`) el equity marcado crece hasta $294.537 y **BRR sí entra: 78
trades de 530**. "No operable" es una condición del capital, no una propiedad del contrato.

Prueba de aislamiento, configuración por defecto:

| Configuración | Trades | CAGR | Sharpe | P&L |
|---|---|---|---|---|
| MBT + BRR, índice común | 467 | +54.90% | 1.19 | $82.187 |
| **MBT solo, mismo índice común** | **467** | **+54.90%** | **1.19** | **$82.187** |
| MBT solo, su índice completo | 474 | +12.62% | 0.22 | $26.282 |

Las dos primeras filas son **idénticas bit a bit**. Añadir BRR al universo no altera el resultado;
lo que lo altera es **qué barras entran en la muestra**.

### El mecanismo: la intersección de calendarios

Meter BRR en `SYMBOLS` dispara la intersección de calendarios de §3 y MBT pierde **167 barras**:
34 antes del primer instante común y **133 dispersas dentro de la muestra** — sesiones en que BRR
no cotizó y MBT sí, como el 2025-05-19 completo.

El efecto no es de P&L. **El único trade de la ventana previa aporta −$55.** La diferencia de
$56.000 viene de que quitar barras desplaza la EMA-20 de N y los canales Donchian, eso cambia qué
barra dispara, y el desplazamiento se propaga a toda la serie. El primer trade ya difiere:
2025-05-30 frente a 2025-05-02.

### Descomposición del efecto

Hay que separar dos cosas que es fácil confundir, porque su magnitud es muy distinta:

| Qué se mueve | Rango de CAGR |
|---|---|
| **El conjunto de barras** (índice común de 4.117 vs índice propio de MBT de 4.284) | **+12.62% → +54.90%, 42.3 pp** |
| La fecha de inicio, sobre el índice común fijo (8 desplazamientos, 0–160 barras) | +44.40% → +54.90%, **10.5 pp** |
| La fecha de inicio, sobre el índice completo de MBT | +5.90% → +43.27%, **37 pp** |

Barrido sobre el índice común, que es el que corre el notebook:

| Inicio | Offset | Trades | CAGR | Sharpe |
|---|---|---|---|---|
| 2025-05-05 | 0 | 467 | +54.90% | 1.19 |
| 2025-05-30 | 20 | 463 | +44.40% | 0.94 |
| 2025-06-03 | 40 | 460 | +45.67% | 0.96 |
| 2025-06-12 | 60 | 460 | +48.01% | 1.00 |
| 2025-06-17 | 80 | 460 | +50.56% | 1.09 |
| 2025-06-18 | 100 | 454 | +46.60% | 0.98 |
| 2025-06-24 | 120 | 453 | +44.52% | 0.92 |
| 2025-06-27 | 160 | 455 | +45.23% | 0.93 |

Sobre el índice completo de MBT el mismo barrido es mucho más inestable —+12.62%, +5.90%, +27.40%,
+34.76% en los cuatro primeros desplazamientos— y los valores bajos se concentran en los offsets
que **incluyen las 34 barras iniciales** (2025-04-25 → 2025-05-05). A partir del offset 40 el
resultado converge a la banda del 27–43%.

Conclusión de la descomposición: **el conjunto de barras pesa más que la fecha de inicio**, y
dentro de él, un puñado de barras de calentamiento al principio de la serie pesa
desproporcionadamente, porque siembra la EMA de N y decide el alineamiento de todas las señales
posteriores.

### Qué significa

Dos lecturas, y conviene no confundirlas:

- Sobre un conjunto de barras fijo, el sistema es **razonablemente estable** al punto de inicio:
  10.5 pp de rango con el Sharpe entre 0.92 y 1.19 y los trades entre 453 y 467.
- Pero **el nivel absoluto del CAGR no es transferible entre conjuntos de barras**. 167 barras de
  diferencia —un 3.9% de la muestra, con un P&L directo de −$55— mueven el resultado 42 pp.

Es coherente con el resto de la evidencia: 1.31 años de muestra, walk-forward OOS en −26.6%,
probabilidad Monte Carlo de DD ≥ 50% del 36.6%, y `n_eff` = 1.00. Ninguno de esos diagnósticos
mejora ajustando parámetros: **el problema es estructural — la muestra es demasiado corta y el
universo demasiado estrecho para que el número absoluto signifique algo.**

### Consecuencias prácticas

1. **Usar `SYMBOLS = ["MBT"]`.** BRR no aporta trades con la configuración por defecto ni
   diversificación (`n_eff` = 1.00, correlación 1.0000 — es el mismo subyacente) y su único efecto
   real es recortar el calendario de MBT en 167 barras. Que además cambie el CAGR es incidental,
   no un argumento a favor de ninguna de las dos opciones.
2. **Comparar configuraciones solo sobre la muestra exacta.** Todas las tablas de §7 lo cumplen
   —se corrieron sobre el índice común— pero **ningún número absoluto de este documento es
   transferible a otra ventana**.
3. Lo que sí sobrevive al cambio de ventana es la **dirección** de los efectos medidos por trade:
   el ganador medio cae al ensanchar el stop, el perdedor medio no, y la MAE discrimina de forma
   monótona. Ésas son las conclusiones que el graveyard sostiene; el nivel de la curva de equity,
   no.

*(Este diagnóstico está implementado como SESGO 5 en §8 del notebook y se recalcula en cada
corrida.)*

---

## 9. La confirmación de ruptura fallida: el cambio que sí funcionó

Síntoma observado al leer el blotter: entrando al open de `t+1` sin condición alguna, el sistema
opera también contra rupturas que **se extienden**. La posición salta al stop, la barra siguiente
vuelve a romper el canal, se entra otra vez y se vuelve a pagar el stop. Los stops no venían de
uno en uno: venían en cadena.

### La regla

| | |
|---|---|
| **Señal** | Ruptura del canal Donchian de 20 barras en `t` |
| **Confirmación** | La barra `t+1` cierra **de vuelta DENTRO** del canal: `close < banda_sup` en una ruptura alcista, `close > banda_inf` en una bajista |
| **Entrada** | Al **OPEN de `t+2`**, en dirección contraria a la ruptura |

El nivel contra el que se compara el cierre de `t+1` es **el que se rompió**, congelado en `t`. El
canal recalculado en `t+1` ya incorpora el máximo de la barra de ruptura, así que compararlo
contra él haría la condición trivialmente más fácil de cumplir.

Implementado en el motor como `confirm_mode="inside"`, con `pending` en dos etapas. El modo
`"beyond"` (cerrar **fuera** del canal, la confirmación clásica de ruptura) sigue disponible y
reproduce el comportamiento anterior.

### Impacto

| | Sin confirmar | **Confirm inside** |
|---|---|---|
| Trades | 467 | **272** |
| Stops (`stop` + `gap_stop`) | **329** | **165** |
| Win rate | 27.8% | 36.4% |
| **Esperanza por trade** | **+0.186 R** | **+0.375 R** |
| CAGR | +54.90% | +70.23% |
| Sharpe | 1.19 | **1.99** |
| Max DD | −31.4% | −27.3% |
| Entradas con hueco | 0.0% | 0.0% |

Se opera el 58% de los trades y se paga la mitad de los stops. La esperanza por trade —el
estadístico que agrega sobre cientos de observaciones, y por tanto el que sí sobrevive al cambio
de ventana— se duplica.

### El efecto colateral: corrige el sesgo de §8

Éste no se buscaba, y es más importante que la mejora de la curva:

| | Sin confirmar | Confirm inside |
|---|---|---|
| Rango de CAGR por ventana (8 desplazamientos) | 10.5 pp | **6.4 pp** |
| Variación de trades entre ventanas | 453–467 (3.0%) | **272–273 (0.4%)** |
| **Efecto del conjunto de barras** (índice común vs propio de MBT) | **42.3 pp** | **5.2 pp** |
| Esperanza por trade en las 8 ventanas | +0.159 … +0.186 | **+0.375 en todas** |

El mecanismo es claro: sin filtro, **cada** ruptura genera una entrada al open siguiente, así que
un desplazamiento mínimo del índice realinea cientos de señales y el resultado se vuelve una
lotería de calentamiento. El filtro de ruptura fallida selecciona un subconjunto pequeño y bien
separado de eventos, que no depende de dónde empiece la serie. **El sesgo estructural de §8 pasa
de 42.3 pp a 5.2 pp.**

### La conclusión de §7 se mantiene, y se afila

Barrido del stop con sizing acompañando, bajo la nueva regla de entrada:

| Stop | Trades | Win rate | Ganador medio (R) | Perdedor medio (R) | Esperanza (R) | CAGR | Sharpe |
|---|---|---|---|---|---|---|---|
| 0.75 N | 284 | 30% | 4.53 | −1.64 | +0.208 | +39.63% | 0.89 |
| **1.00 N** | 272 | 36% | **4.24** | −1.83 | **+0.375** | **+70.23%** | **1.99** |
| 1.25 N | 262 | 41% | 3.13 | −1.85 | +0.182 | +30.35% | 0.78 |
| 1.50 N | 244 | 45% | 2.61 | −1.88 | +0.128 | +24.01% | 0.69 |
| 1.55 N ← MAE\* | 243 | 45% | 2.52 | −1.85 | +0.129 | +21.25% | 0.62 |
| 2.00 N | 228 | 49% | 1.95 | −1.65 | +0.114 | +17.77% | 0.64 |
| 2.50 N | 217 | **52%** | **1.52** | −1.40 | +0.109 | +18.21% | 0.86 |

El patrón de §7 es el mismo y más limpio: el win rate sube monótonamente del 30% al 52% al
ensanchar el stop mientras el ganador medio se derrumba de 4.53 R a 1.52 R, y el óptimo de
esperanza está en 1N. La aritmética de la escalera sigue cumpliéndose (medianas de −1.017,
−1.527, −1.516 y −1.016 R para 1–4 unidades) y los stops con hueco siguen siendo el 24.8%.

### No contradice la barra de confirmación descartada en §5

En §5 figura "barra de confirmación en la ruptura" como idea descartada, con el Sharpe empeorando
de −0.30 a −0.77. **No es la misma regla, es la opuesta**, y se aplicaba al sistema opuesto:

| | §5 (descartada) | §9 (adoptada) |
|---|---|---|
| Sistema | Ruptura (seguir el movimiento) | Reversión (operar contra él) |
| Condición en `t+1` | Cierra **FUERA** del canal | Cierra **DENTRO** del canal |
| Qué selecciona | Rupturas que se extienden | Rupturas que fallan |
| Entrada | Al cierre de `t+1` | Al open de `t+2` |

Las dos reglas seleccionan poblaciones **complementarias** de rupturas. Que una perjudique al
sistema de ruptura y la otra beneficie al de reversión es la misma observación vista dos veces:
en esta muestra, a 30 minutos, las rupturas que fallan son más operables que las que continúan.

### Lo que esto no arregla

- **Es un ensayo más**, contabilizado ya en §10: con los 48 ensayos declarados el DSR es 0.374
  y el Sharpe no sobrevive a la deflación. La regla se probó porque un patrón
  del blotter la sugería, no como parte de una rejilla declarada de antemano.
- El checklist pasa de 5/11 a **6/11** (entra el criterio de Monte Carlo: la probabilidad de
  DD ≥ 50% cae por debajo del 20%). El veredicto sigue siendo **no desplegar capital**.
- Los cuatro fallos que quedan son estructurales y no los toca ningún parámetro: 1.31 años de
  muestra, 6 folds OOS, 46% de señales ejecutables con contratos enteros, y `n_eff` = 1.00.
- Los folds OOS del sistema fijo pasan de +2.25% a **+7.62%** de retorno medio, pero siguen siendo
  6 folds sin potencia estadística.

---

## 10. Deflated Sharpe Ratio: el recuento de ensayos, y el veredicto

Las secciones anteriores dejaron tres veces la misma deuda escrita: *"es un ensayo más y no está
en el recuento de DSR/PBO"*. Ésta la salda, y el resultado invalida la lectura optimista de todo
lo anterior.

### Qué mide, y por qué no es el Monte Carlo de §9

El Monte Carlo responde *"¿qué tan afortunada fue esta secuencia de trades?"*. El DSR (Bailey &
López de Prado, 2014) responde otra cosa: ***"¿qué tan afortunada fue esta configuración entre
todas las que se probaron?"***. Probando N configuraciones sin ninguna habilidad, el mejor Sharpe
de la rejilla es alto **por construcción**; el DSR compara el Sharpe elegido contra `SR*`, el
máximo esperable bajo la nula de que ninguna configuración tiene edge, y devuelve
`P(SR verdadero > SR*)`.

Implementación en `deflated_sr.py` (raíz del repo), especificada por los 20 contract tests de
`test_deflated_sr.py` — incluidos los dos de Monte Carlo que fijan la propiedad que importa: el
mejor de N estrategias sin edge **no** debe parecer hábil (DSR ≈ 0.5) y un edge genuino sí debe
sobrevivir a la deflación (DSR ≈ 1).

### El resultado

Matriz de ensayos: 2 canales × 6 stops × 2 modos de confirmación × 2 lados de señal =
**48 configuraciones**, cada una con sus retornos por barra.

| | |
|---|---|
| Ensayos declarados | 48 → **40 efectivos** (`N_eff = M(1−ρ̄) + ρ̄`) |
| SR observado (por barra) | **0.0283** — anualizado 1.60 |
| **SR\*** (listón bajo la nula) | **0.0332** — anualizado 1.87 |
| σ(SR) con no-normalidad | 0.0153 (sesgo +0.53, **curtosis 86.28**) |
| PSR contra SR = 0 | **0.9674** |
| **DSR contra SR\*** | **0.3743** |
| MinTRL al 95% frente a SR\* | **∞** — el SR no supera el listón |

**El Sharpe no sobrevive.** El SR observado queda **por debajo** del máximo esperable de buscar
entre 40 ensayos independientes sin edge alguno. Y la configuración operada es la **número 1 de
48** de su propia rejilla: exactamente la condición que el DSR está construido para penalizar.

Dos detalles que agravan el diagnóstico:

- **El PSR de 0.967 contra un benchmark de cero parecería tranquilizador.** La distancia entre
  0.967 y 0.374 **es** el coste de haber buscado. Reportar el PSR y callar el DSR es la forma
  educada de mentir con este dato.
- **La curtosis de 86.28** —la cola de stops con hueco medida en §7— infla σ(SR) por sí sola y
  sube el listón. El mismo Sharpe con retornos normales exigiría menos track record.

### 48 es una cota inferior

El sesgo de selección no distingue entre una rejilla declarada de antemano y el prueba y error de
ir mirando resultados. La matriz cubre lo que se puede parametrizar, pero **estas decisiones se
tomaron mirando resultados intermedios y no aparecen como columnas**:

| Decisión | Dónde se tomó |
|---|---|
| Granularidad de 30 min | §1 — tras medir siete granularidades |
| Objetivo en el medio del canal (y no en la banda opuesta) | §6 — tras medir tasas de toque |
| Barrera vertical de 10 barras | §6 |
| Regla de confirmación de ruptura fallida | §9 — sugerida por un patrón del blotter |
| Inversión del lado de la señal | §6 — tras el fracaso del sistema de ruptura |
| Sizing por R en vez de por N | §6 |

Contadas, `N_eff` sube y el DSR baja. **El 0.374 es el mejor caso, no el peor.**

### Lo que esto cierra

El checklist pasa a **8 de 12** con el nuevo criterio, y el veredicto no se mueve: **no desplegar
capital**. Pero cambia cuál es el argumento principal para no hacerlo.

Hasta aquí, la razón era la muestra: 1.31 años, 6 folds, `n_eff` = 1.00. Ahora hay una razón más
directa y que no depende de conseguir más datos: **con la búsqueda que se hizo sobre la muestra
que hay, un Sharpe anualizado de 1.60 es indistinguible de haber elegido el mejor de 40 monedas al
aire.** No es un problema que se arregle con otra configuración —buscar más lo empeora, porque
sube `SR*`—. Se arregla con **más muestra o menos búsqueda**, y de las dos, la única disponible
aquí es la segunda.

Es también el motivo por el que las conclusiones que este documento sí sostiene son las que **no**
dependen de la selección: la aritmética de la escalera, el desplome del ganador medio al ensanchar
el stop, la falta de poder del `MAE*` como criterio, el E-ratio < 1 y la fragilidad de muestra.
Todas se miden sobre cientos de trades dentro de una configuración, no eligiendo entre
configuraciones.

---

## 11. Binance perpetuo vs. CME MBT: el coste no explica la diferencia

> Cifras de `qsCrypto/notebooks/MeanReversion_Binance_Perp.ipynb` §12, sobre la **rejilla
> idéntica** de 4.354 barras de 30 min (los timestamps de CME intersecados contra BTCUSDT,
> 2025-04-25 → 2026-09-04). Mismo sistema, mismos parámetros, mismas barras. La comparación
> contra los 277 trades reales de `output/turtle_ibkr_MBT_30mins_trades.csv`.

**Hipótesis puesta a prueba:** *la diferencia de desempeño se explica en su totalidad por
mayores costes.* **Rechazada por los datos: el coste explica el 22 %.**

### 11.1 La tarifa, por lado y por unidad de nocional

El coste del CME es **fijo por contrato** (0,62 USD comisión + 1 tick de 0,50 USD) y el de
Binance es **proporcional al nocional** (2 pb taker + 1 pb de deslizamiento). Para compararlos
hay que expresar el fijo en pb, dividiendo por el nocional real de un contrato MBT
(`entry × 0,1`, media de 8.770 USD en los 277 trades):

| | Comisión / lado | Deslizamiento / lado | Total / lado | **Round-trip** |
|---|---|---|---|---|
| **CME MBT (IBKR)** | 0,62 USD = **0,746 pb** | 0,50 USD = **0,601 pb** | 1,347 pb | **2,69 pb** |
| **Binance BTCUSDT perp** | **2,000 pb** | **1,000 pb** | 3,000 pb | **6,00 pb** |
| | | | | **2,23× más caro** |

### 11.2 Lo que eso cuesta con una cuenta de 100.000

El sizing es el mismo en los dos casos (`qty = 1 % × equity / (1N)`), y **N/precio es
prácticamente idéntico** en las dos series sobre la misma rejilla —BTCUSDT 0,6725 %, MBT
0,6688 %—, así que una unidad tiene el mismo nocional en ambos y la diferencia es **enteramente
la tarifa**, no el tamaño:

| Con 100.000 y riesgo 1 % | Binance BTCUSDT | CME MBT |
|---|---|---|
| Nocional de 1 unidad | 148.689 USD | 149.532 USD |
| Coste round-trip, 1 unidad | **89,21 USD = 0,089 R** | **40,28 USD = 0,040 R** |
| Coste round-trip, 2 unidades (riesgo 1,5 R) | **178,43 USD = 0,119 R** | **80,57 USD = 0,054 R** |

Medido sobre los trades reales, con la escalera cargada de verdad (1,72 unidades medias en
Binance, 1,76 en CME) y cobrando cada add:

| Por trade, medido | Binance | CME MBT |
|---|---|---|
| Coste medio | **95,03 USD** | **63,31 USD** |
| Coste en R inicial | **0,140 R** | **0,065 R** |
| Trades | 302 | 277 |
| **Drag total de la fricción** | **42,2 R** | **18,1 R** |
| Coste facturado en la ventana | **28.698 USD** | **17.537 USD** |
| Turnover (suma de nocional ejecutado) | 102,5 M USD | 131,5 M USD |

El CME **mueve un 28 % más de nocional y paga un 39 % menos**. Es toda la aritmética del coste
fijo: a 8.770 USD por contrato, 1,12 USD por lado son 1,35 pb; en Binance son 3,00 pb sin
importar el tamaño.

**Funding: irrelevante.** 1.494 liquidaciones en la ventana suman **−199 USD**, un −0,15 %/año
sobre el capital inicial. Era la sospecha inicial y no es el mecanismo.

### 11.3 El contrafactual que rechaza la hipótesis

Mismas barras, mismas señales, mismos trades (302 en los cuatro casos: la tarifa no cambia
*qué* se opera, solo cuánto queda):

| Escenario, rejilla CME de 4.354 barras | P&L | CAGR | mean R | win |
|---|---|---|---|---|
| Binance, tarifa real (2+1 pb) + funding real | **−10.611** | **−7,91 %** | +0,012 | 29,5 % |
| Binance, tarifa real, sin funding | −10.944 | −8,17 % | +0,014 | 29,5 % |
| **Binance con la tarifa del CME MBT (0,75+0,60 pb)** | **+12.946** | **+9,36 %** | +0,091 | 30,1 % |
| Binance, sin costes ni funding | **+47.542** | **+33,09 %** | +0,167 | 31,1 % |
| **CME MBT, real, con sus costes** | **+97.877** | **+63,22 %** | — | 36,1 % |

**Lo que cierra la hipótesis:** poner a Binance la tarifa exacta del CME recupera 23.557 USD.
La distancia total a CME es de 108.488 USD. **El coste explica el 21,7 %; el 78 % restante
sobrevive a igualar la tarifa.** Y el techo del argumento es todavía más claro: **regalando por
completo la operativa** —cero comisión, cero deslizamiento, cero funding— Binance rinde
+33,09 % frente al +63,22 % que CME obtiene *pagando* sus costes. El coste no puede explicar una
diferencia que persiste cuando el coste es cero.

### 11.4 El coste sí importa — pero por otra vía

Lo que sí muestran los datos es que **la fricción se amplifica** al pasar por el sizing. Cobrar
15.100 USD de coste (tarifa CME) destruye 34.596 USD de P&L: **2,3× lo facturado**. Cobrar
28.698 USD (tarifa Binance) destruye 58.153 USD: **2,0×**. El motivo es mecánico y ya está en
§7: `qty` se calcula sobre el `balance` vivo y la regla de drawdown recorta el nocional otro
20 % por cada −10 % de equity. Cada dólar de coste no se resta una vez; se resta y además
encoge todas las posiciones siguientes.

Por eso, con un edge desnudo de **+0,167 R** por trade, una fricción de **0,140 R** no deja
margen: **se lleva el 84 % de la esperanza**. Con la tarifa del CME se lleva el 39 %. Ése es el
número que decide si esta estrategia es operable en un perpetuo, y no depende de nada que se
pueda ajustar en el código.

### 11.5 Qué queda sin explicar, y qué ya estaba medido

El 78 % de la diferencia que no es coste no se atribuye aquí a nada nuevo. Dos efectos ya
medidos en este documento y en los notebooks apuntan en la dirección correcta, y **ninguno de
los dos favorece a la estrategia**:

- **Velas planas: MBT 8,4 %, BTCUSDT 0,0 %** (§1 y notebook Binance §12.5). Una vela sin rango
  ni volumen no puede tocar ninguna barrera horizontal, así que en el 8,4 % de las barras la
  posición del CME es **inmune al stop**. Es un artefacto del feed, no una propiedad del
  mercado, y **infla el resultado de CME**.
- **Empalme de vencimientos: +14,77 % acumulado en 12 rolls, ≈ +11 %/año** (§8). La serie de
  CME **no está retroajustada**; BTCUSDT no tiene rolls que empalmar.

Cuantificar cuánto de los 84.931 USD restantes corresponde a cada uno exige rehacer el backtest
de CME con la serie retroajustada y sin velas planas. **No se ha hecho, y hasta que se haga la
diferencia queda como no explicada.** Lo que sí queda cerrado es que no es el coste.

---

## 12. Momentum del rango de apertura vs VWAP de 24 h: la tasa base es 50 %

> Fuente: `qsCrypto/notebooks/Intraday_Momentum_OR_VWAP.ipynb`, ejecutado de punta a punta
> sobre 4,7 años de velas de 5 minutos de Binance USDⓈ-M (1 225 sesiones, BTCUSDT/ETHUSDT/
> SOLUSDT + QQQUSDT como control). Veredicto: **no desplegable — falla los 4 criterios
> críticos** (DSR, supervivencia a costes, R de cartera > 0, significancia del WFO), con la
> higiene de la medición pasando entera (12/12).

La hipótesis: la primera vela de 5 minutos de la sesión NYSE/CME predice el color del día. Si
es alcista y cierra por encima del VWAP de las últimas 24 h (288 velas, incluidas las
overnight), comprar; bajista y por debajo, vender; plano a las 15:45 ET.

### 12.1 El número que cierra la discusión

El cierre de la sesión cae del lado de la señal:

| símbolo | sesiones | acuerdo color/VWAP | señales | **acierto bruto** |
|---|---|---|---|---|
| BTCUSDT | 1 225 | 57,1 % | 699 | **49,8 %** |
| ETHUSDT | 1 225 | 55,4 % | 679 | **49,9 %** |
| SOLUSDT | 1 225 | 54,2 % | 664 | **46,8 %** |
| QQQUSDT | 114 | 61,4 % | 70 | **48,6 %** |

Esto es antes de costes, de ejecución y de sizing: es la tasa base que la estrategia afirma.
Es una moneda al aire. Todo lo demás del cuaderno es consecuencia de esto, y **ninguna
calibración arregla una tasa base del 50 %**.

### 12.2 El control desmiente el mecanismo, no lo confirma

QQQUSDT existe en este cuaderno precisamente para hacer interpretable un nulo en BTC: si la
tesis es flujo real de la sesión de contado estadounidense, el efecto tiene que verse **más
fuerte** en el proxy del Nasdaq. Y la señal sí es más nítida ahí — mayor cuerpo de apertura en
N (0,099 contra 0,043 de BTC) y mayor acuerdo color/VWAP (61 % contra 57 %). Lo que no hace es
predecir: 48,6 % de acierto, y la fricción se lleva el **99 %** del bruto (+0,043 R → +0,001 R).

La señal existe más donde el mecanismo lo predice. El poder predictivo, no.

### 12.3 La fricción, otra vez

| símbolo | R bruto | comisiones | deslizamiento | funding | **R neto** | fricción/bruto |
|---|---|---|---|---|---|---|
| BTCUSDT | +0,0408 | −0,0124 | −0,0062 | −0,0001 | **+0,0221** | **46 %** |
| ETHUSDT | +0,0187 | −0,0097 | −0,0048 | −0,0003 | **+0,0039** | **77 %** |
| SOLUSDT | −0,0202 | −0,0067 | −0,0033 | −0,0002 | **−0,0304** | — |
| QQQUSDT | +0,0432 | −0,0285 | −0,0143 | +0,0005 | **+0,0009** | **99 %** |

Referencia: el fade Donchian 24/7 estaba en 56 %. Aquí BTC está mejor (46 %) y todo lo demás
peor. Con **costes cero** el flagship daría +6,0 % CAGR y Sharpe 7,25; en el escenario
*optimista* del barrido de sensibilidad (2 pb comisión + 5 pb deslizamiento) ya es **−1,1 %**.
El único resultado positivo del cuaderno vive enteramente dentro del supuesto de 2 pb + 1 pb.

### 12.4 Diversificar no ayuda: 3 símbolos son 1,4 apuestas

La cartera de los tres símbolos con muestra suficiente pierde: **CAGR −2,9 %, Sharpe −1,99,
Max DD −22 %** sobre 2 040 operaciones, aunque BTCUSDT aislado gane un 15 % en 4,7 años.
`effective_bets` da correlación media **0,77** y **1,37 apuestas efectivas de 3 símbolos**, con
el 100 % de los pares por encima de 0,70. No es una cartera; es la misma apuesta tres veces.

### 12.5 Los estadísticos de sobreajuste, y el patrón repetido

- **DSR = 0,0** con 70 ensayos: el Sharpe OOS (0,12) está muy por debajo del que produciría el
  azar tras esa búsqueda (17,09).
- **Walk-forward**: gana a la base en 9 de 14 folds, pero **p = 0,26**. No significativo.
- **PBO = 0,39** y ganador modal estable al 64 %.

Ese último par es el mismo patrón que `MeanReversion_Binance_Perp.ipynb`: la selección de
parámetros **es** consistente, solo que converge de forma estable a una configuración que
tampoco gana. Un PBO bajo dice que la optimización no se engaña a sí misma; no dice que haya
edge que encontrar.

### 12.6 Lo que sí se aprendió, y que sobrevive al veredicto

**1. N tiene que medir el horizonte de la posición, no el de la vela.** Al primer intento se
usó `compute_n(period=288)` sobre velas de 5 minutos, creyendo que daba "volatilidad de 24 h".
No la da: es la media de Wilder del rango verdadero de **cada vela**, así que devuelve el rango
medio de una vela de 5 minutos suavizado sobre un día — ~104 $ con BTC a 46 000 $, cuando el
recorrido real de 24 h son ~2 055 $ y el de una sesión ~1 370 $.

El síntoma fue inmediato y fácil de leer mal: **R de ±20** y la cuenta partida por la mitad
"arriesgando el 1 %". Un recorrido de sesión son **13,6** de esas N. Dimensionar una posición
de 6 h 15 min con volatilidad de 5 minutos infradimensiona el riesgo por ese factor.
`rolling_range_n` (rango de las últimas 288 velas) lo corrige y además sale del mismo búfer de
288 velas que ya necesita el ancla VWAP, así que el motor en vivo no necesita una serie diaria
aparte. La N Turtle diaria clásica da ~2 175 $, un 6 % de diferencia: la elección concreta no
importa, lo que importaba era el orden de magnitud.

**2. Esta vez el backtest no miente.** Los dos sistemas anteriores murieron por errores de
simulación (ceguera al stop overnight en el del VWAP). Éste es **estructuralmente inmune**: la
posición se cierra a las 15:45 ET y nunca hay orden en reposo cruzando la noche. Se comprueba
en vez de afirmarse — correr con y sin el camino 24/7 completo da resultados **idénticos** al
bit. Cuando el mecanismo de la estrategia elimina una clase de error, conviene medirlo: es la
diferencia entre "no debería pasar" y "no pasa".

**3. Correr sin stop es lo que permite decidir sobre el stop.** La línea base usa
`mae_stop_n=inf`: `stop_n` fija la unidad de riesgo (sizing y R) pero nada cierra la posición
salvo el reloj. Con un stop real, la MAE queda truncada en el stop y la distribución ya no
puede decir dónde ponerlo — se mediría el propio stop. Sin él: la MAE **no discrimina**
(edge ratio 0,92, la excursión favorable ni siquiera supera a la adversa), y barrer el stop
entre 0,25 N y 2 N no mejora nada de forma monótona. Un stop no salva a este sistema, y eso se
sabe *porque* se corrió sin él.

**4. Una lista de criterios plana deja que la higiene vote sobre el edge.** La lista de
verificación heredada de los cuadernos anteriores pondera igual los 15-16 criterios y dispara el
veredicto en el 70 %. Al corregir un criterio que estaba mal calculado (`pnl_concentration`
devuelve `top_3`, no `top_3_pct`: el `.get()` daba NaN y el criterio salía False siempre) y
añadir otro de higiene, este cuaderno pasó de 67 % a **75 %** y el veredicto se dio la vuelta a
"desplegable" — **sin que cambiara un solo número sobre el edge**: DSR seguía en 0,0, la cartera
seguía en Sharpe −1,99 y seguía sin sobrevivir al escenario optimista de costes.

La lista se partió en dos: **críticos** (DSR, supervivencia a costes, R de cartera > 0,
significancia del WFO) que son de **veto**, y **higiene** (look-ahead, arranque desigual,
ceguera overnight, concentración, PBO, muestra) que hacen creíble un resultado positivo pero no
lo producen. No tener look-ahead es condición para creerse un número positivo, no un sustituto
de tenerlo. Los cuadernos anteriores tienen el mismo defecto en su lista; sus veredictos no
cambian porque fallaban también en los críticos, pero el mecanismo estaba ahí.

**5. Dos piezas reutilizables quedan en el motor.** `BinancePerpetualsPortfolio` gana
`session_flat_at` (cierre por reloj de sesión, comprobando **ambos** bordes: sin el borde de
apertura, una posición que sobreviviera a la medianoche no se cerraría hasta las 15:40 del día
siguiente) y la separación `stop_n` / `stop_n_order` — unidad de riesgo contra dónde va la
orden de stop, el mismo desdoblamiento que `stop_n`/`mae_stop_n` en el motor de backtest. Hace
falta para cualquier estrategia cuyo backtest no lleve stop: en vivo el stop es una red de
seguridad ancha, y si además dimensionara, la posición saldría varias veces más pequeña que la
validada sin que nada lo avisara. Tests en `qsCrypto/portfolio/binance_session_test.py`.

### 12.7 Qué no merece la pena, y qué sí

**No merece la pena** seguir barriendo parámetros: cada barrido encarece el DSR y el problema
no está en la calibración.

Merecen la pena, como preguntas **nuevas** y no como rescate de ésta:

1. **QQQUSDT con dos años de muestra.** Es donde la señal es más nítida y el único sitio donde
   el mecanismo postulado podría verificarse. Con 115 sesiones no se puede decidir.
2. **Volume bars o dollar bars** — muestrear por tiempo de mercado en vez de por reloj, como
   apunta el docstring original de la estrategia. Requiere tick data de los primeros 5 minutos
   de cada sesión.
3. **CME MBT en lugar del perpetuo**: sin funding, y la sesión regular coincide con el
   mecanismo postulado. Bloqueado hoy por el entitlement de datos de IBKR (10197/10089).

---

## 13. Muestreo por información: el nulo era de la hipótesis, no del reloj

> Fuente: `qsCrypto/notebooks/Information_Bars_Opening_Range.ipynb`, ejecutado de punta a punta
> sobre 2.471.041 velas de 1 minuto de BTCUSDT (um), 1.225 sesiones, 2022-01 → 2026-09. Once
> esquemas de muestreo más un control. Veredicto: **ninguno cruza la puerta**.

El §12 dejó abierta una pregunta legítima: si el 49,8 % de acierto de la vela de apertura era de
la **hipótesis** o de **cómo se muestrearon los datos**. López de Prado (AFML cap. 2) sostiene lo
segundo: la información no llega uniformemente en tiempo de reloj, así que las barras de tiempo
sobremuestrean lo tranquilo e inframuestrean lo activo. Se probó: mismo instrumento, misma regla
de señal (`opening_range_signal`), misma ancla, misma salida — **solo cambia el muestreo**.

### 13.1 El control es lo que da valor al nulo

Las velas de 5 minutos pasadas por el camino de código nuevo dan **49,79 %**, la misma cifra
hasta la centésima que el §12. Cuando los otros once esquemas no separan del ruido, es porque no
hay señal, no porque la tubería esté rota. Un experimento de muestreo sin este control no
distingue "no hay efecto" de "lo he construido mal".

### 13.2 Resultados

n ≈ 700 señales por celda (solo ~57 % de las sesiones producen señal), error típico 1,89 %.
Puerta pre-registrada con corrección de Bonferroni sobre 11 celdas: **55,36 %**.

| esquema | min/barra | acierto | IC 95 % |
|---|---|---|---|
| **dollar $115M** | 5 | **52,06 %** | [48,37 ; 55,72] |
| dollar $58M | 3 | 51,29 % | [47,58 ; 54,98] |
| imbalance 1.200 BTC | 32 | 50,88 % | [47,53 ; 54,22] |
| volume 2.250 BTC | 5 | 50,85 % | [47,17 ; 54,52] |
| **volume 5.000 BTC** (la hipótesis) | 11 | **49,93 %** | [46,40 ; 53,47] |
| *CONTROL time 5min* | 5 | *49,79 %* | [46,09 ; 53,48] |

Ninguno excluye el 50 % ni **sin** corregir. Los 5.000 BTC que motivaban el experimento dan
exactamente la moneda al aire del §12.

**LdP acierta en la dirección y no le alcanza**: a igualdad de duración, las barras de dólar
baten a las de volumen en las dos duraciones cortas (52,06 vs 50,85 a 5 min; 51,29 vs 49,64 a 3),
que es lo que predice su argumento de invariancia al nivel de precio. Pero ~1,2 puntos con error
típico 1,9 es ruido.

### 13.3 El defecto que la hipótesis traía de fábrica, medido

Un umbral fijo en BTC muestrea un trozo distinto de la sesión cada año:

| año | barra de 5.000 BTC | barra de $115M |
|---|---|---|
| 2022 | 6 min | 5 min |
| 2025 | **17 min** | 5 min |

El volumen en BTC de la vela 09:30–09:35 cayó de 4.683 a 1.366 (−71 %) mientras el volumen en
dólares se mantuvo plano (~$120–140 M). "Los primeros 5.000 BTC" no es la misma pregunta en 2022
y en 2025. Es exactamente el argumento del libro a favor de las barras de dólar, medido en datos
propios.

### 13.4 Tres trampas que costó encontrar

**1. Las barras de desequilibrio de la EWMA degeneran sobre velas de 1 minuto, en silencio.** La
formulación canónica dispara cuando `|theta_T| >= E[T]·|2v⁺−E[v]|`. Pero `theta` es una suma de
flujo **firmado**: se comporta como un paseo aleatorio y crece con `sqrt(T)`, no con `T`. Un
disparador que escala con `E[T]` se va por encima de lo alcanzable — a 5 minutos pedía 297 BTC
cuando el paseo aleatorio alcanza ~340 en el mejor caso — así que la barra topaba en
`max_minutes`, `E[T]` se actualizaba hacia el tope, y el disparador subía más. Realimentación
positiva.

En la primera ejecución las tres celdas de desequilibrio daban una barra de apertura de **75
minutos exactos —el tope— en todas las sesiones**. No eran barras de desequilibrio: eran el tope
disfrazado, y nada en el resultado lo delataba (salían cifras plausibles, ~50 %). Se detectó
mirando `min/barra`, no el acierto.

La solución fue un **umbral fijo calibrado por duración** (300 BTC ≈ 5 min, 600 ≈ 12, 1.200 ≈ 31),
que además es lo único que permite comparar los tres esquemas a igualdad de tamaño de barra en
vez de comparar duraciones distintas. La EWMA se conserva en `imbalance_bars(threshold=None)` con
la inestabilidad documentada.

**2. "El color de la vela diaria" no es una predicción.** La vela diaria de Binance abre a las
00:00 UTC; la señal se emite a las 14:35 UTC. En ese instante **ya ha ocurrido el 54 % del
recorrido del día**, y el signo del movimiento hasta ese momento ya coincide con el de la vela
completa el **75,1 %** de los días. Por eso la columna de "acierto diario" salía en ~75 % para
*todos* los esquemas, **incluido el control que ya sabíamos que no predice nada**. No predice la
vela: la observa a mitad de camino. Un 75 % que parece extraordinario y no es nada.

**3. El archivo de Binance no es idéntico al REST.** `data.binance.vision` rellena algunos tramos
con velas planas (volumen 0, precio congelado: 2023-11-10, 2024-03-27, 2024-10-28) donde el REST
sí devuelve mercado: 308 minutos en toda la muestra, 114 dentro de sesión, 2 sesiones afectadas.
Además hay ~3 barras donde el archivo trae ~3× el volumen del REST (la caché REST se pobló en
vivo y capturó datos incompletos). En agregado las dos fuentes cuadran al 0,009 %.

La puerta de integridad del cuaderno no afloja la tolerancia: exige que **toda** discrepancia esté
explicada por minutos de volumen cero y acota el residuo. Un desplazamiento de columnas o un
desfase de huso produciría discrepancias en barras *con* volumen y por todo el rango — y haría
fallar el test aunque el error relativo fuese pequeño. Es la diferencia entre "artefacto conocido
y acotado" y "tolerancia generosa".

### 13.5 El proxy de 1 minuto no llega, y el nulo aguanta igual

Contra aggTrades reales en 40 sesiones estratificadas: la frontera de barra se desplaza ~28
segundos y el cierre ~0,009 N, pero **el signo de la señal se vuelca en el 6,7 %** de las
sesiones — por encima del 2 % que se había fijado como aceptable. Limitación real, declarada.

No cambia la conclusión, y el motivo importa: un volteo no correlacionado con el resultado es
ruido de clasificación, y el ruido de clasificación **atenúa hacia el 50 %, nunca aleja**.
Desatenuando (`p = (p_obs − f)/(1 − 2f)`), el mejor esquema pasa de 52,06 % a 52,37 %: sigue a
tres puntos de la puerta. El proxy puede estar **escondiendo** señal; no puede fabricarla. Si el
veredicto fuera positivo, este mismo fallo lo invalidaría — la asimetría es deliberada.

### 13.6 Qué queda

La pregunta del §12 queda cerrada: el nulo es de la **hipótesis**, no del muestreo. Es un
resultado más fuerte que el del §12 por sí solo, que no podía descartar el reloj.

No merece la pena: más umbrales (cada uno encarece la puerta), barras de tick (el esquema más
débil de los cuatro según AFML), ni el backtest con costes — el suelo económico es lo de menos
cuando la tasa base es 50 %.

Sí podrían merecerla, como preguntas **nuevas**:

1. **Desequilibrio sobre tick data de verdad.** Es el único esquema cuya versión de 1 minuto
   pierde información de forma sustantiva: el flujo alternante *dentro* del minuto se cancela
   antes de contarse, así que el desequilibrio medido es una cota inferior. El lector ya está
   escrito (`fetch_aggtrades_day`); el coste son ~4 GB por cada 200 sesiones.
2. **Otro predictor sobre estas mismas barras.** La infraestructura (`qsCrypto/backtest/bars.py`,
   la validación contra tick data, la puerta pre-registrada) es agnóstica a la señal. Lo que se
   agotó es el rango de apertura contra el VWAP, no el muestreo por información.

### 13.7 Rentabilidad medida, y el A/B de la hora de cierre

> Añadido tras la pregunta correcta: *"me interesa rentabilidad, no accuracy del predictor"*.
> Backtest en `qsCrypto/backtest/session_backtest.py`, con costes reales, funding histórico y
> curva de equity. A/B entre cerrar a **16:00 ET** (cierre del NYSE) y a **23:59 UTC** (fin del
> día UTC, para que la posición nunca cruce la medianoche).

**Gana 16:00 ET, en los 12 esquemas sin excepción** (diferencia mediana de Sharpe: −0,23 al
extender). Duplicar la exposición de 6,4 h a 10,3 h **baja** el R neto medio de 0,0395 a 0,0294
y empeora el drawdown de −8,5 % a −11,3 %. Las cuatro horas posteriores al cierre del contado
añaden varianza sin añadir deriva. La objeción de que la vela diaria abarca 24 h era correcta
como crítica de la métrica, pero extender la posición no la arregla: la confirma al revés — si
queda algo, vive dentro de la sesión de contado.

**El mejor sistema posible** (`dollar $115M`, salida 16:00 ET, 705 operaciones, 4,7 años) da
CAGR +6,07 %, Sharpe 0,64, Max DD −8,5 %. Parece vivo. No lo está:

| | |
|---|---|
| **top-10 operaciones (1,4 %)** | **97,6 % del P&L** |
| top-5 | 63 % del P&L |
| R neto **mediano** | +0,0049 (≈ cero) |
| MAE mediana / R medio | **6,8x** |
| t-stat(R) | 1,76 (puerta con 24 intentos: 3,08) |
| CAGR con costes *optimistas* (2+5 pb) | **+2,1 %**, Sharpe 0,18 |
| CAGR con costes *base* (5+10 pb) | **−4,5 %** |

Quita diez días de 1.225 y el sistema no existe. Eso es la objeción de las velas planas
cuantificada: la media es positiva, la operación típica no gana nada, y el riesgo soportado
—6,8 unidades de excursión adversa mediana por unidad de R devuelta, sin stop— no está
remunerado.

**Lección de método, y es la tercera vez que aparece en este documento:** `pnl_concentration`
juzga por `top_3` y aquí devolvía "concentración aceptable" con el 98 % del P&L en diez
operaciones. El criterio del checklist se reescribió sobre `top_10 < 50 %` y pasó a ser de veto.
Un helper que resume bien el caso corriente puede dar un OK falso en el caso extremo — y el caso
extremo es justo donde importa.

Veredicto de rentabilidad: **2 de 7 criterios críticos**. Lo que la tasa de acierto del 52 %
insinuaba como "casi nada", la medición de P&L lo confirma como "nada, sostenido por diez días
y un supuesto de costes optimista".
