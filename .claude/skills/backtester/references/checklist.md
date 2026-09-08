# Checklist de revisión de un backtest

Para auditar un backtest ajeno (o el propio de hace tres meses). Recorre en orden;
para cada punto exige el **número**, no la afirmación.

---

## A. Datos

- [ ] ¿Cuál es la **fecha de composición** del universo? ¿Es la de hoy? → survivorship.
- [ ] ¿Están los activos **deslistados, muertos o fusionados**? ¿Con qué precio final?
- [ ] Inventario con fecha de listado por símbolo. ¿Cuántos tienen <2 años? Para
      trend-following, esa es muestra insuficiente.
- [ ] ¿Qué **calendario** se aplica por clase de activo? ¿Coincide con el mercado real?
- [ ] ¿Hay `bfill`, `interpolate` o relleno sintético en precios? → leakage.
- [ ] ¿Los datos fundamentales/macro son **point-in-time** o la serie revisada de hoy?
- [ ] ¿Qué símbolos se descartaron y **con qué motivo explícito**? (un descarte
      silencioso reaparece como bug cinco pasos después)
- [ ] ¿Hay barras con `close` fuera de `[low, high]`? ¿Precios cero vs. negativos?

## B. Features y señales

- [ ] ¿**Todas** las features de decisión llevan `shift(1)` o equivalente?
- [ ] ¿Se ha ejecutado el **test de truncamiento** (`assert_no_lookahead`)? ¿Está en el
      código, ejecutándose, o es un comentario?
- [ ] ¿Alguna normalización (`mean`, `std`, `min/max`, scaler) usa la muestra completa?
- [ ] ¿El scaler/encoder se ajusta **dentro** de cada fold, solo con train?
- [ ] ¿El universo se filtró con información futura (liquidez media total, market cap
      actual, "los que llegaron a X")?

## C. Motor

- [ ] Fees, slippage y funding/carry **explícitos y por instrumento**. ¿El funding se
      cobra solo a quien lo paga de verdad?
- [ ] ¿Slippage al alza en estrategias de ruptura? (se llenan mal por definición)
- [ ] ¿Prelación intradía **pesimista** cuando falta granularidad?
- [ ] ¿Hay tope de **apalancamiento bruto**? ¿Cuál fue el nocional máximo y el p90?
- [ ] ¿El mark-to-market usa el último cierre conocido por símbolo (`ffill`)?
- [ ] ¿Se devuelve el **libro abierto**? ¿Su P&L está marcado como no realizado?
- [ ] ¿Se respetan límites de unidades por mercado / grupo correlacionado / dirección?

## D. Validación

- [ ] ¿Existe un **baseline sin optimizar**? ¿Se compara fold a fold en la misma ventana?
- [ ] ¿Hay **purga** y **embargo** entre train y test? ¿Con qué `t1`?
- [ ] ¿Las ventanas OOS **no se solapan**?
- [ ] ¿Cuántas configuraciones se evaluaron (`n_trials`), incluidas las manuales?
- [ ] ¿Los parámetros ganadores son **estables** entre folds? (moda y su frecuencia)
- [ ] ¿Se usó CPCV para obtener una **distribución** de Sharpe OOS, o un único camino?
- [ ] ¿Cuántos folds hay? ¿Es una muestra o son 16 observaciones de un sistema que
      genera 2–3 eventos relevantes al año?

## E. Estadística

- [ ] **Deflated Sharpe** con `n_trials` declarado. ¿DSR > 0.95?
- [ ] **PBO** por CSCV. ¿< 0.5?
- [ ] **MinTRL**: ¿la muestra alcanza la longitud mínima requerida?
- [ ] **Monte Carlo** con reemplazo: percentiles de equity final, distribución de
      Max DD, probabilidad de ruina.
- [ ] **Concentración de P&L**: ¿top-3 < 50%? ¿Qué queda sin el mejor trade?
- [ ] **Apuestas efectivas** (`n_eff`): ¿cuántos mercados independientes hay de verdad?
- [ ] **Sensibilidad a costes**: ¿sobrevive el escenario pesimista?

## F. Interpretación

- [ ] ¿El resultado se compara contra el **buy & hold** del activo dominante y contra
      un benchmark declarado?
- [ ] ¿El veredicto es **explícito**? ("NO despliegues capital" cuando toca)
- [ ] Si el resultado es malo: ¿se identifica qué es **estructural** (universo poco
      diversificado, muestra corta, la optimización contradice la premisa) frente a lo
      que es ajustable? Proponer más optimización sobre un problema estructural es
      malgastar el tiempo.
- [ ] ¿Se declara qué **no** se modeló? (impacto de mercado, límites de borrow,
      halts, financiación del margen, fiscalidad, capacidad)

---

## Umbrales por defecto del veredicto

Adáptalos a la estrategia, pero decláralos **antes** de mirar los resultados:

| Criterio | Umbral |
|---|---|
| WFO/CPCV vs. baseline fijo | gana en >60% de folds |
| t-test de la diferencia | p < 0.05 |
| Estabilidad de parámetros | moda >50% de folds |
| Deflated Sharpe | > 0.95 |
| PBO | < 0.5 |
| Escenario pesimista de costes | CAGR > 0 |
| Concentración top-3 | < 50% del P&L |
| Apuestas efectivas | n_eff >= 8 |
| Sharpe OOS | > Sharpe del B&H dominante |
| Prob. Monte Carlo de ruina (DD>=50%) | < 20% |
