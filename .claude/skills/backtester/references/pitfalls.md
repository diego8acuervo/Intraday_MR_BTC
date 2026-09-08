# Catálogo de trampas medidas

Cada entrada es un error que ya se coló en un backtest real, con la magnitud del daño.
No son hipótesis: son cifras observadas. Úsalo como lista de sospechosos cuando un
resultado parezca demasiado bueno, o cuando algo reviente sin motivo aparente.

---

## Datos y calendario

**Calendario equivocado por clase de activo.** Aplicar un calendario 24/7 a un
instrumento que cotiza días hábiles rellena fines de semana con barras planas y
**diluye el ATR**. Si el sizing es `riesgo·equity / N`, un N diluido **infla la
posición ~26% de media**. Cripto → 24/7; commodities, FX, equity, tipos → weekday.

**Mark-to-market que pierde posiciones el fin de semana.** Con un índice de fechas que
es la **unión** de calendarios, `t not in data[s].index` es cierto cada sábado para
todo símbolo TradFi. Sin forward-fill del último cierre conocido, esas posiciones
desaparecen de la suma y la curva dibuja un **serrucho semanal** falso, más visible
cuanto más nocional TradFi hay abierto.

**Cesta equiponderada con `mean(axis=1)`.** Un activo vivo con el mercado cerrado no
"falta": su retorno es **0**. `skipna` divide por los que tienen dato, no por los
vivos, así que el fin de semana la cesta pasa a ser "solo cripto al 100%". Medido:
**CAGR 22.85% → 44.99%**. Construye una máscara de vida (primera↔última barra por
símbolo) y divide por el número de vivos.

**`fillna(0.0)` en la primera fila de retornos.** Sin él, `cumprod` propaga el NaN a
**toda** la serie y el retorno total sale `nan%`.

**Inner join antes de calcular retornos.** El activo más joven trunca a todos; con
calendarios heterogéneos la intersección puede quedar **vacía** (un contrato que dejó
de cotizar en 2023 y otro con 241 barras recientes no comparten ni un día). Calcula el
retorno sobre el índice **propio** de cada símbolo y alinea después. Y si alineas
primero, el lunes de todo símbolo TradFi sale NaN (su fila del domingo no existe) y
pierdes un día de cada cinco.

**Reparación de high/low en históricos antiguos.** Los futuros de Yahoo anteriores a
~2012 traen el `close` como *settlement* de otra fuente que el rango intradía, sin
reconciliar: el cierre cae fuera de `[low, high]` en cientos de barras. Un
`check_price_validity` estricto descartaba **47 de 64 instrumentos** por unas barras de
hace 20 años — justo la profundidad que se buscaba. Forzar `high = max(OHLC)` y
`low = min(OHLC)` sin tocar `open`/`close` recupera 54/54.

**Precio negativo ≠ dato ausente.** El WTI liquidó en −37.63 el 2020-04-20. Es el dato
correcto. Distingue el **cero** (centinela de dato ausente → error) del **negativo**
(evento real en materia prima física → warning).

**Contrato muerto con precio congelado.** `std = 0` ⇒ correlación indefinida ⇒ columna
entera de NaN ⇒ `np.linalg.eigvalsh` revienta con *"eigenvalues did not converge"*.
LAPACK no valida su entrada: un NaN dentro no da un error de dominio legible, sino
no-convergencia. Es la causa silenciosa; la del calendario es la ruidosa.

---

## Motor de backtest

**Funding cobrado a quien no lo paga.** El funding es de **perpetuos**. Los futuros
listados y los índices cash llevan su carry embebido en el precio forward y depositan
margen: no financian el nocional. Cobrárselo es un coste inventado que escala con el
**nocional**, mientras el sizing Turtle escala con el **riesgo** — así que en
instrumentos de baja volatilidad (N/precio ≈ 0.15% ⇒ 6.8x de nocional por unidad)
drena la cuenta. Medido: **9.783 USD de funding sobre 10.000 de capital**, equity
mínimo −2.064 y Max DD −112.8%. Con la corrección: −80.7%, y la curva nunca cruza cero.

**Sizing por riesgo sin tope de nocional.** `riesgo·equity/N` fija el riesgo, no el
nocional: cuanto menor es `N/precio`, más nocional compra ese 1%. Sin tope de
apalancamiento bruto, el libro llegó a **187.682 USD de nocional sobre 10.000 de
cuenta (18.8x; p90 38x)**.

**Prelación intradía optimista.** Si en la misma vela se tocan stop y salida y no hay
dato de menor frecuencia, asume que se ejecutó el **stop**. Equivocarse a favor del
sistema es exactamente cómo se construyen backtests mentirosos.

**Devolver solo `(curva, trades)`.** Los trades son los **cerrados**; las posiciones
vivas el último día no están en ninguna de las dos salidas. Sin el libro abierto no
sabes qué tiene puesto el sistema hoy. Devuélvelo, marca su P&L como **no realizado**
(está al último cierre, sin comisión de salida) y señala los símbolos `stale`.

---

## Estadística

**Matriz de correlación par a par no es PSD.** Con `min_periods`, cada celda ve una
muestra distinta, así que la matriz puede tener autovalores negativos. Descartar los
negativos con `ev[ev>0]` **quita masa de la traza e infla el % explicado por PC1**.
Proyecta a la correlación válida más cercana: clip de autovalores a 0,
rediagonalización a 1.

**`T/N < 10`.** Matriz de correlación mal condicionada ⇒ el número efectivo de apuestas
sale **sesgado al alza**. Avísalo junto al número, o el número miente.

**Poda por `dropna(how="any")`.** Se lleva por delante a los símbolos sanos que solo
chocan con el intruso. Poda **iterativamente** el que más pares sin estimar tenga.

**Monte Carlo permutando el orden de los trades.** La suma de P&L es invariante: el
equity final sale **idéntico** en todas las simulaciones. Solo el remuestreo **con
reemplazo** genera una distribución real.

**Optimizar Sharpe a secas.** Selecciona sistemáticamente el parámetro **más
apalancado** de la rejilla. Penaliza por drawdown: `Sharpe_IS − λ·|MaxDD_IS|`.

**Contar solo la rejilla formal como `n_trials`.** El sesgo de selección no distingue
entre rejilla y prueba y error manual. Todo lo que miraste cuenta.

**Confundir "no significativo" con "poco potente".** Con 16 folds OOS y un sistema que
captura 2–3 tendencias al año, un p-valor alto no dice "no funciona": dice **"no
tienes muestra"**. Ambas conclusiones desaconsejan desplegar capital, pero por motivos
distintos y con remedios distintos.
