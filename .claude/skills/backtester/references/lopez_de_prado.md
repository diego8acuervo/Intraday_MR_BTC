# Purga, embargo y validación cruzada financiera — López de Prado

Referencia de fórmulas y criterios para `scripts/purged_cv.py` y `scripts/stats_tests.py`.
Fuente: Marcos López de Prado, *Advances in Financial Machine Learning* (Wiley, 2018);
Bailey & López de Prado (2012, 2014); Bailey, Borwein, López de Prado & Zhu (2017).

---

## 1. Por qué el K-fold estándar no vale en finanzas

Tres supuestos que la validación cruzada clásica da por buenos y que en series
financieras son falsos:

1. **Observaciones i.i.d.** No lo son: las etiquetas se construyen sobre ventanas
   solapadas (el retorno a 20 días de hoy comparte 19 días con el de mañana).
2. **Train y test independientes.** El solape de etiquetas hace que una observación
   de train contenga información del periodo de test. Es leakage puro.
3. **Una sola partición basta.** Un único camino OOS no distingue "funciona" de
   "tuve suerte con el corte".

El barajado (`shuffle=True`) agrava las tres. En series temporales financieras es
leakage por construcción.

---

## 2. `t1` — el objeto que lo hace todo posible

Para cada observación abierta en `t0`, `t1` es el instante en que **su etiqueta queda
determinada**:

| Caso | `t0` | `t1` |
|---|---|---|
| Retorno a h barras | barra de la señal | `t0 + h` |
| Triple barrera | señal | primer toque de PT / SL / vencimiento |
| Trade de un backtest | apertura | cierre (stop, salida o vencimiento) |
| Feature con lookback L | barra | la propia barra (la feature mira atrás) |

Sin `t1` no hay purga posible. Si tu pipeline no lo produce, ese es el primer bug
que arreglar, antes de mirar ninguna métrica.

---

## 3. Purga (AFML §7.4.1)

Elimina del **train** toda observación cuyo intervalo `[t0_i, t1_i]` se solape con el
intervalo del test `[t0_test, t1_test]`. Tres formas de solaparse:

```
t0_test <= t0_i <= t1_test          # empieza dentro del test
t0_test <= t1_i <= t1_test          # termina dentro del test
t0_i <= t0_test  y  t1_test <= t1_i # envuelve al test
```

`purged_cv.get_train_times(t1, test_times)`.

## 4. Embargo (AFML §7.4.2)

La purga solo mira solapes de etiqueta. La **correlación serial** de features y ruido
sobrevive a eso: una observación que arranca justo después del test sigue contaminada.
El embargo descarta una fracción `h` de barras posteriores a cada bloque de test.

- Valor típico: **1% de las barras**. Con etiquetas de horizonte largo, usa al menos
  el **horizonte máximo de la etiqueta** (o el lookback del indicador más largo).
- No hace falta embargo *antes* del test: la purga ya lo cubre por ese lado.

`purged_cv.apply_embargo(t1, test_times, embargo_pct)`.

## 5. `PurgedKFold` (AFML §7.4.3)

K-fold con test **contiguo** (nada de barajar) + purga + embargo. Es el mínimo
aceptable para cualquier CV sobre datos financieros.

```python
cv = PurgedKFold(n_splits=5, t1=t1, embargo_pct=0.01)
scores = [fit_and_score(X.iloc[tr], X.iloc[te]) for tr, te in cv.split(X)]
```

Señal de que la purga está actuando: `len(train) < n_total − len(test)`. Si son iguales,
`t1` está mal construido (probablemente `t1 == t0`).

## 6. CPCV — Combinatorial Purged Cross-Validation (AFML §12)

Parte la muestra en `N` grupos y usa `k` de ellos como test en **todas** las
combinaciones. Produce `k·C(N,k)/N` caminos OOS distintos.

- `N=6, k=2` → 15 splits, 5 caminos.
- El resultado es una **distribución de Sharpe OOS**, no un número. Reporta media,
  desviación y percentil 5. Si el p5 es negativo, la estrategia depende del corte.
- Es la defensa más fuerte contra el "walk-forward afortunado", que es el modo más
  común de auto-engaño en el backtesting profesional.

`purged_cv.CombinatorialPurgedCV(n_splits=6, n_test_splits=2, t1=t1, embargo_pct=0.01)`.

## 7. Unicidad y pesos de muestra (AFML §4)

- `num_co_events`: cuántas etiquetas están vivas en cada barra.
- `average_uniqueness`: media de `1/concurrencia` durante la vida de la etiqueta.
  Un valor de 0.09 significa que cada observación es ~9% información nueva.
- `sample_weights_by_uniqueness`: pondera cada observación por su unicidad
  (opcionalmente escalada por el retorno atribuido). **Obligatorio antes de entrenar
  cualquier modelo ML** sobre etiquetas solapadas; si no, sobre-pondera los periodos
  con muchas señales simultáneas, que son justo los de régimen extremo.
- `time_decay_weights`: decaimiento lineal por antigüedad. `last_weight=0.5` deja la
  observación más vieja con la mitad de peso; negativo la borra del todo.

## 8. Diferenciación fraccionaria (AFML §5)

`pct_change()` hace la serie estacionaria a costa de **borrar toda la memoria**. La
diferenciación fraccionaria de ventana fija (FFD) busca el `d` mínimo que pasa el ADF
conservando el máximo de memoria (normalmente `d ∈ [0.2, 0.6]`, no 1.0).

```python
d = min_ffd_order(close)              # requiere statsmodels
x = frac_diff_ffd(np.log(close), d, thres=1e-4)
```

Si la ventana de pesos supera la longitud de la serie, sube `thres` — la función
lo avisa con un error explícito en vez de devolver una serie vacía.

## 9. Triple barrera (AFML §3)

Etiquetado por primer toque entre take-profit, stop-loss y vencimiento. Su valor aquí
no es tanto el ML: es que **produce el `t1` correcto**. Un stop es una barrera
horizontal, y la fecha de su toque es exactamente el instante en que la información
deja de ser futura.

---

## 10. Deflated Sharpe Ratio (Bailey & López de Prado, 2014)

Probar `n_trials` configuraciones infla el mejor Sharpe **por construcción**, aunque
todas sean ruido. El listón bajo la hipótesis nula es:

```
E[max SR] ≈ σ_SR · [ (1−γ)·Z(1 − 1/N) + γ·Z(1 − 1/(N·e)) ]
```

con `γ` = constante de Euler-Mascheroni (0.5772), `Z` la inversa de la normal, `N` el
número de pruebas y `σ_SR` la desviación de los Sharpe de la rejilla.

El **DSR** es el PSR calculado contra ese benchmark, ajustado por asimetría y curtosis:

```
PSR(SR*) = Φ( (SR − SR*)·√(n−1) / √(1 − γ₃·SR + (γ₄−1)/4·SR²) )
```

- **DSR > 0.95** ⇒ el Sharpe sobrevive al ajuste por pruebas múltiples.
- `n_trials` debe incluir **todo** lo evaluado, también lo que probaste a mano y
  descartaste. El sesgo de selección no distingue rejilla formal de prueba y error.
- Cola izquierda gorda (`γ₃ < 0`, `γ₄` alto) ⇒ hace falta mucho más track record para
  el mismo Sharpe. Vender volatilidad puntúa mal aquí, y debe.

## 11. Minimum Track Record Length

Cuántas observaciones necesitas para que el Sharpe sea significativo al nivel dado.
Si `MinTRL > len(returns)`, **no tienes evidencia todavía** — cualquier otra
conclusión es prematura, por bonita que sea la curva.

## 12. PBO por CSCV (Bailey, Borwein, López de Prado & Zhu, 2017)

Probability of Backtest Overfitting. Con una matriz `T × N` de retornos por
configuración: se parte el eje temporal en `S` bloques, y para cada combinación de
`S/2` bloques como IS se mira el **rango OOS** de la configuración ganadora IS.

```
PBO = P( logit(rango OOS de la ganadora IS) <= 0 )
    = P( la ganadora IS queda por debajo de la mediana OOS )
```

- **PBO < 0.5** ⇒ el proceso de selección aporta algo.
- **PBO > 0.5** ⇒ seleccionar por IS es **peor que elegir al azar**: tu procedimiento
  de optimización está entrenado sobre el ruido.
- Repórtalo siempre junto al DSR: miden cosas distintas (DSR ajusta la métrica; PBO
  audita el *procedimiento de selección*).

---

## 13. Orden de aplicación recomendado

```
t1 (triple barrera o cierre de trade)
   ↓
pesos por unicidad  →  entrenamiento / optimización
   ↓
PurgedKFold  (mínimo)  o  CPCV  (preferido)   ← purga + embargo aquí
   ↓
distribución de Sharpe OOS
   ↓
DSR (n_trials declarado)  +  PBO por CSCV
   ↓
Monte Carlo de trades  +  sensibilidad a costes
   ↓
checklist y veredicto
```
