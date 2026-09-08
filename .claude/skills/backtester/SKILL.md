---
name: backtester
description: Diseña, audita y valida backtests de estrategias financieras con estándares anti-autoengaño. Detecta y cuantifica data leakage (look-ahead), survivorship bias y overfitting; aplica purga, embargo, walk-forward y Combinatorial Purged CV (Marcos López de Prado); mide Deflated Sharpe, PBO, sensibilidad a costes, concentración de P&L, apuestas efectivas y Monte Carlo. Úsalo SIEMPRE que se escriba, revise o interprete un backtest, un walk-forward, una optimización de parámetros, un split train/test sobre series financieras, o cuando alguien pregunte si los resultados de una estrategia son creíbles o desplegables. Triggers: backtest, walk-forward, out-of-sample, data leakage, look-ahead, survivorship, overfitting, purga, embargo, CPCV, Sharpe deflactado, PBO, Monte Carlo de trades, validación de estrategia.
---

# Backtester — validación honesta de estrategias cuantitativas

## 0. Postura por defecto

Un backtest no sirve para producir una curva bonita: sirve para **intentar matar la
estrategia** antes de que lo haga el mercado. Si al terminar no has cuantificado cuánto
del resultado es sesgo, el backtest no está terminado.

Tres reglas innegociables:

1. **Cada resultado va acompañado de su sesgo medido**, no de un descargo genérico.
   "Puede haber survivorship" no vale; "el CAGR cae 8.4 pp al restringir a veteranos" sí.
2. **Cuando dudes entre dos supuestos, elige el que perjudique a la estrategia.**
   Si en la misma vela se tocan stop y salida y no hay dato intradía, se ejecutó el stop.
3. **El baseline sin optimizar es la vara de medir.** Toda optimización debe
   demostrar que le gana de forma estadísticamente distinguible del ruido. Si no,
   la optimización *es* el ruido.

## 1. Flujo de trabajo

Al construir o revisar un backtest, sigue este orden. No saltes al paso 5 sin el 2.

| # | Fase | Entregable |
|---|------|------------|
| 1 | Declarar sesgos y supuestos al inicio | Tabla de sesgos con impacto y control (ver §2) |
| 2 | Datos: fuente, calendario, limpieza, validación | Inventario con fecha de listado por símbolo |
| 3 | Features: `shift(1)` + test de truncamiento | Assert anti-look-ahead ejecutado, no comentado |
| 4 | Motor: costes, funding, apalancamiento, límites | Curva + trades + **libro abierto** |
| 5 | Baseline canónico sin optimizar | Métrica de referencia |
| 6 | Validación: purga + embargo + WFO / CPCV | Track record OOS concatenable |
| 7 | Cuantificación de sesgos | §7 completa, con números |
| 8 | Estadística: DSR, PBO, Monte Carlo | ¿Sobrevive al ajuste por múltiples pruebas? |
| 9 | Checklist de validez → veredicto | Aprobado/rechazado con criterios explícitos |

## 2. Encabezado obligatorio del backtest

Todo notebook o script de backtest abre con una tabla como esta, rellenada para *ese*
caso. Es lo primero que lee quien vaya a creerse el resultado:

```markdown
| # | Sesgo | Impacto | Control en este backtest |
|---|-------|---------|--------------------------|
| 1 | Survivorship bias | 🔴 Severo | El universo es la composición ACTUAL. Medido en §7.1 con test de listing-date |
| 2 | Look-ahead | 🟠 Alto | Todas las features con .shift(1); test de truncamiento en §3 |
| 3 | Overfitting | 🔴 Severo | Rejilla de N combinaciones; WFO vs. baseline fijo en §6; PBO en §8 |
| 4 | Costes ignorados | 🟠 Alto | Fees + slippage + funding modelados; sensibilidad en §7.4 |
```

Y una frase que diga **cuál es el diagnóstico que importa** — normalmente no es el Sharpe.

## 3. Data leakage (look-ahead)

### Fuentes habituales, por frecuencia real de aparición

1. **Indicador sin `shift(1)`.** `rolling(20).max()` incluye la barra de hoy. Si operas
   la ruptura del canal hoy usando el máximo que *incluye* el máximo de hoy, entras
   siempre en el punto exacto. Todo canal, banda, media, z-score o percentil usado
   para decidir en `t` debe construirse con datos hasta `t-1`.
2. **Normalización sobre toda la muestra.** `(x - x.mean()) / x.std()` con media y desvío
   de todo el histórico mete el futuro en cada observación. Usa expanding/rolling, o
   ajusta el scaler **solo** en train dentro de cada fold.
3. **Relleno hacia atrás.** `bfill()`, `interpolate()` sobre precios o fundamentales.
   Nunca. Y `ffill` solo con la latencia real de publicación.
4. **Reetiquetado de datos revisados.** PIB, earnings, índices reconstruidos: usa
   *point-in-time*, no la serie revisada de hoy.
5. **Etiquetas que se solapan con el train.** El caso de López de Prado: si la etiqueta
   de la observación `i` se resuelve en `t1[i]` y `t1[i]` cae dentro del periodo de test,
   esa observación filtra información. Se resuelve con **purga** (§5).
6. **Selección del universo con información futura.** Filtrar por "liquidez media del
   histórico completo" o "market cap actual" es leakage de universo.
7. **Split aleatorio (`shuffle=True`) en series temporales.** `KFold` estándar sobre
   datos financieros es leakage por construcción. Usa `PurgedKFold`.

### Test de truncamiento — obligatorio, no opcional

La única prueba que realmente cierra el asunto: recalcula la feature sobre el histórico
**truncado** en `t` y compara con el valor que tenía en la serie completa. Si difieren,
la feature ve el futuro.

```python
from scripts.bias_audit import assert_no_lookahead
assert_no_lookahead(lambda d: prepare(d, 20, 10)["ent_hi"], df, n_checks=25)
```

Añade además la verificación directa contra el cálculo manual:

```python
_t = prepare(df, 20, 10)
_manual = df["high"].rolling(20).max().shift(1).reindex(_t.index)
assert np.allclose(_t["ent_hi"], _manual, equal_nan=True)
```

## 4. Survivorship bias y calidad de universo

- **Declara la fecha de composición del universo.** "Constituyentes a Ago-2026" backtesteado
  desde 2021 es survivorship puro: los que colapsaron no están.
- **Mídelo con el test de listing-date.** Parte el universo en *veteranos* (cotizando al
  inicio de la muestra) y *recientes*; corre la estrategia en ambos. La diferencia de CAGR
  es tu cota inferior del sesgo. `scripts/bias_audit.py:survivorship_split`.
- **Contratos muertos con precio congelado.** Varianza cero ⇒ correlación indefinida ⇒
  matrices que revientan con "eigenvalues did not converge". Detéctalos y sácalos, con
  motivo explícito, antes de que rompan algo cinco celdas más abajo.
- **Máscara de vida para cestas equiponderadas.** Un activo vivo con mercado cerrado no
  "falta": su retorno es 0. Dividir por los que tienen dato (en vez de por los vivos)
  convierte el fin de semana en "solo cripto al 100%" e infla el CAGR de forma brutal.
- **Nunca alinees con inner join antes de calcular retornos.** El activo más joven trunca
  a todos, y con calendarios heterogéneos la intersección puede quedar vacía. Calcula el
  retorno sobre el índice propio de cada símbolo y alinea después.
- **Calendario correcto por clase de activo.** Cripto 24/7, TradFi días hábiles. Aplicar
  "24/7" a un futuro diluye el ATR y, si el sizing es `riesgo/N`, infla la posición.

## 5. Purga, embargo y validación cruzada (López de Prado)

`scripts/purged_cv.py` implementa todo esto. Referencia completa en
`references/lopez_de_prado.md`.

### Conceptos

- **`t1` (horizonte de la etiqueta).** Para cada observación, el instante en que su
  etiqueta queda determinada. Sin `t1` no puedes purgar. En un backtest de trades,
  `t1` es la fecha de cierre del trade.
- **Purga.** Elimina del *train* toda observación cuyo intervalo `[t0, t1]` se solape con
  el intervalo del *test*. Sin purga, train y test comparten información y el OOS miente.
- **Embargo.** Además de purgar, descarta una fracción (típicamente 1%–5% de las barras,
  o el horizonte máximo de la etiqueta) **inmediatamente después** del test. Cubre la
  correlación serial residual que la purga no ve.
- **PurgedKFold.** K-fold con test contiguo + purga + embargo.
- **CPCV (Combinatorial Purged CV).** Elige `k` de `N` grupos como test en todas las
  combinaciones ⇒ genera **múltiples caminos OOS** en vez de uno. Da una distribución
  de Sharpe OOS, no un número. Es la mejor defensa contra "tuve suerte con el split".
- **Unicidad de muestra.** Etiquetas solapadas ⇒ observaciones no i.i.d. Pondera cada
  observación por su unicidad media (`sample_weights_by_uniqueness`) antes de entrenar
  cualquier modelo ML.
- **Fractional differentiation.** Estacionariedad conservando memoria, en vez de
  diferenciar a saco y tirar la señal.

### Walk-forward: el diseño que no engaña

- IS rodante o anclado; **OOS que no se solapan** (paso = tamaño OOS) ⇒ el track record
  OOS es concatenable sin doble conteo.
- **Embargo entre IS y OOS** de al menos el horizonte máximo de la señal.
- **Criterio de selección penalizado**, no Sharpe puro: `Sharpe_IS − λ·|MaxDD_IS|`.
  Optimizar Sharpe a secas selecciona sistemáticamente el parámetro más apalancado.
- **Cada fold OOS se compara contra el baseline canónico en la MISMA ventana.** Reporta
  `wins/n_folds`, la media de la diferencia, y un t-test sobre `OOS − BASE`.
- **Estabilidad de parámetros.** Si el ganador salta cada fold, no hay señal, hay ruido.
  Reporta la moda y su frecuencia por parámetro.
- **Advierte del tamaño muestral.** Con 5 años y ventanas de 90d salen ~16 folds. Para
  un sistema que captura 2–3 tendencias al año, 16 observaciones no son una muestra:
  es diagnóstico cualitativo, no evidencia estadística. Dilo explícitamente.

## 6. Overfitting

- **Rejilla deliberadamente pequeña.** Cada combinación probada es una prueba múltiple
  que infla el mejor Sharpe. Declara el número de configuraciones evaluadas (`n_trials`)
  — lo necesitarás para el Deflated Sharpe.
- **Sharpe deflactado (DSR).** Ajusta el Sharpe por el número de pruebas, la longitud
  de la muestra y la no-normalidad de los retornos. Un Sharpe 1.5 tras 200 pruebas puede
  tener DSR < 0.5. `scripts/stats_tests.py:deflated_sharpe_ratio`.
- **PBO por CSCV.** Probabilidad de que la configuración ganadora IS quede por debajo de
  la mediana OOS. PBO > 0.5 ⇒ el proceso de selección es peor que elegir al azar.
  `scripts/stats_tests.py:pbo_cscv`.
- **Minimum Track Record Length.** Cuántas observaciones necesitas para que ese Sharpe
  sea distinguible de cero con confianza dada.
- **Cuenta TODAS las pruebas**, incluidas las que hiciste "explorando" y descartaste.
  El sesgo de selección no distingue entre rejilla formal y prueba y error manual.

## 7. Cuantificación de sesgos — las cinco medidas

Ninguna es opcional. Todas están en `scripts/bias_audit.py`.

1. **Survivorship** — `survivorship_split`: CAGR veteranos vs. universo completo.
2. **Concentración de P&L** — `pnl_concentration`: si el top-3 de trades hace >50% del
   resultado, tu track record tiene tamaño muestral 3, no N. Reporta también el P&L sin
   el mejor trade, la cola negativa y los peores meses.
3. **Diversificación ilusoria** — `effective_bets`: número efectivo de apuestas
   independientes vía autovalores de la matriz de correlación. Correlación par a par con
   `min_periods`, proyección a PSD (clip de autovalores negativos + rediagonalización) y
   aviso si `T/N < 10` (matriz mal condicionada ⇒ `n_eff` sesgado al alza). "Crees tener
   50 mercados; tienes ~4."
4. **Sensibilidad a costes** — `cost_sensitivity`: escenarios sin costes / optimista /
   base / pesimista / estrés. Si el edge muere al doblar el slippage, no era un edge.
   Cobra el funding **solo** a los instrumentos que realmente lo pagan (perpetuos), nunca
   a futuros listados o índices cash: es un coste inventado que escala con el nocional
   mientras el sizing escala con el riesgo, y drena la cuenta en instrumentos de baja vol.
5. **Monte Carlo** — `mc_bootstrap_trades`: remuestreo **con reemplazo** de los trades,
   5.000 simulaciones. Permutar el orden no sirve: la suma de P&L es invariante y el
   equity final sale idéntico. Reporta percentiles de equity final, distribución de
   Max DD y probabilidad de ruina.

## 8. Realismo del motor

- **Costes explícitos**: fee taker + slippage + funding/carry, con el slippage al alza
  en estrategias de ruptura (se llenan mal por definición).
- **Tope de apalancamiento bruto.** El sizing por riesgo (`riesgo·equity/N`) no tiene
  techo de nocional: cuanto menor es `N/precio`, más nocional compra ese 1%. Sin tope,
  libros de 18x sobre la cuenta.
- **Mark-to-market con último cierre conocido (`ffill` por símbolo).** Si el índice de
  fechas es la unión de calendarios, sin esto la curva pierde las posiciones TradFi cada
  fin de semana y aparece un serrucho semanal falso.
- **Devuelve el libro abierto.** `(curva, trades)` solo contiene trades **cerrados**;
  las posiciones vivas el último día no aparecen en ninguno de los dos. Sin el libro
  abierto no sabes qué tiene el sistema puesto hoy. Marca el P&L como **no realizado** y
  señala contratos `stale` (sin datos recientes).
- **Prelación intradía pesimista** cuando no hay datos de menor frecuencia.

## 9. Checklist de validez — el veredicto

Cierra siempre con esto, calculado, no copiado. Adapta umbrales a la estrategia:

```
[ ] WFO/CPCV supera al baseline fijo en >60% de folds
[ ] Diferencia WFO − baseline estadísticamente significativa (t-test, p<0.05)
[ ] Parámetros estables entre folds (moda >50%)
[ ] Deflated Sharpe > 0 al 95% con n_trials declarado
[ ] PBO < 0.5
[ ] Edge sobrevive el escenario pesimista de costes
[ ] Top-3 trades < 50% del P&L total
[ ] Nº efectivo de apuestas independientes >= 8
[ ] Sharpe OOS > Sharpe del buy & hold del activo dominante
[ ] Prob. Monte Carlo de ruina (DD>=50%) < 20%
[ ] Tests anti-look-ahead ejecutados y en verde
```

Y el veredicto explícito: **"NO despliegues capital"** cuando corresponda. Un checklist
que sale mal no es un fracaso: es el coste que te ahorraste de descubrirlo con dinero real.

Cuando el resultado sea malo, di además **qué problema es estructural** (universo poco
diversificado, muestra demasiado corta, la optimización contradice la premisa de la
estrategia) — eso no lo arregla ningún ajuste de parámetros, y proponer más optimización
en ese punto es malgastar el tiempo del usuario.

## 10. Recursos del skill

| Archivo | Contenido |
|---|---|
| `references/lopez_de_prado.md` | Purga, embargo, CPCV, unicidad, triple barrera, frac-diff, DSR/PBO — con las fórmulas |
| `references/checklist.md` | Checklist extendida de revisión de un backtest ajeno |
| `references/MAE.md` | Maximum Adverse Excursion: umbral MAE*, curva P(gana | MAE≥x), E-ratio, calibración de stops con datos en vez de heurística |
| `references/pitfalls.md` | Catálogo de trampas medidas en producción (calendario, funding, PSD, cestas, inner join) |
| `scripts/purged_cv.py` | `PurgedKFold`, `CombinatorialPurgedCV`, `walk_forward_folds`, unicidad, frac-diff |
| `scripts/stats_tests.py` | PSR, Deflated Sharpe, MinTRL, PBO por CSCV, t-test de folds |
| `scripts/bias_audit.py` | `assert_no_lookahead`, `survivorship_split`, `pnl_concentration`, `effective_bets`, `cost_sensitivity`, `mc_bootstrap_trades` |

Los scripts son numpy/pandas/scipy puros, sin dependencias del repo. Impórtalos añadiendo
la carpeta `scripts/` al `sys.path`, o cópialos al proyecto si el backtest debe ser
autocontenido.
