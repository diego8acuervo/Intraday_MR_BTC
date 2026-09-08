---
name: maximum_adverse_excursion_analysis
description: Guía metodológica y cuantitativa para calcular, evaluar e implementar Maximum Adverse Excursion (MAE) en el diseño de stops, perfilado de drawdowns intra-trade y optimización de estrategias sistemáticas de trading.
---

# Maximum Adverse Excursion (MAE)

> **Implementación en este repo:** `qsCrypto/portfolio/mae.py` (`Excursion`,
> `excursions_from_ohlc`, `mae_star`, `win_prob_curve`, `edge_ratio`,
> `resolvability`). El §3 de este documento es pseudocódigo ilustrativo en Polars
> y **no ejecuta**; usa el módulo, que está cubierto por
> `qsCrypto/portfolio/mae_test.py`.

El **Maximum Adverse Excursion (MAE)** mide la pérdida máxima no realizada (peor *drawdown* intrabarra o intrate trade) experimentada por una posición desde su punto de entrada hasta su cierre. Introducido por John Sweeney, permite calibrar *stop-loss* basados en datos empíricos en lugar de reglas heurísticas arbitrarias.

---

### 1. Lógica Matemática y Estructura de Datos

Para cada trade $i \in \{1, \dots, N\}$ con precio de entrada $P_{\text{entry}, i}$ y horizonte temporal $t \in [t_{\text{entry}}, t_{\text{exit}}]$:

* **Posiciones Largas (Long):**

$$\text{MAE}_i = P_{\text{entry}, i} - \min_{t} (L_{i, t})$$


* **Posiciones Cortas (Short):**

$$\text{MAE}_i = \max_{t} (H_{i, t}) - P_{\text{entry}, i}$$



Donde $L_{i, t}$ y $H_{i, t}$ representan los mínimos y máximos alcanzados durante la vida del trade. En la práctica cuantitativa se expresa en **unidades de volatilidad** (múltiplos de ATR) o **retornos porcentuales** para normalizar series no estacionarias.

---

### 2. Flujo de Trabajo en Análisis Cuantitativo

* **Separación de Poblaciones:** Divide los trades cerrados en dos subconjuntos: ganadores ($\mathcal{W}$) y perdedores ($\mathcal{L}$).
* **Generación del Scatter Plot MAE vs. PnL:** Grafica el MAE (eje X) frente al resultado final del trade (eje Y).
* **Identificación del Umbral Crítico ($MAE^*$):** Determina el percentil alto de MAE en trades ganadores (por ejemplo, percentil 95 de $\mathcal{W}$). Un trade ganador rara vez supera cierta excursión negativa antes de recuperarse.
* **Calibración de Stop-Loss:** Establece el stop protector en $MAE^* + \epsilon$. Cualquier trade que supere este nivel tiene una probabilidad condicional mínima de terminar en ganancia:

$$P(\text{Trade} \in \mathcal{W} \mid \text{MAE} > MAE^*) \to 0$$


* **Optimización de Función Objetivo:** Compara la curva de capital recalculada evaluando métricas ajustadas por riesgo:
* Sharpe / Sortino Ratio
* Calmar Ratio y Max Drawdown global
* Expectativa matemática por trade: $E = (P_w \times W) - (P_l \times L)$



---

### 3. Implementación Vectorizada (Python / Polars)

```python
import polars as pl

def compute_trade_mae(trades_df: pl.DataFrame, ohlc_df: pl.DataFrame) -> pl.DataFrame:
    """
    Calcula MAE normalizado por ATR para cada trade ejecutado.
    trades_df: [trade_id, side, entry_time, exit_time, entry_price, pnl]
    ohlc_df: [timestamp, high, low, atr]
    """
    # Join temporal para mapear rango intrabarra del trade
    joined = ohlc_df.join(
        trades_df,
        left_on="timestamp",
        right_on="entry_time",
        how="inner"
    )
    
    # Evaluación vectorizada según dirección
    mae_expr = pl.when(pl.col("side") == "LONG")\
        .then((pl.col("entry_price") - pl.col("min_low_during_trade")) / pl.col("entry_atr"))\
        .otherwise((pl.col("max_high_during_trade") - pl.col("entry_price")) / pl.col("entry_atr"))
        
    return trades_df.with_columns(mae_expr.alias("mae_atr"))

```

---

### 4. Directrices para el Diagnóstico del Modelo

* **Stops Demasiado Ajustados:** Si $MAE^*$ recorta el *win rate* de la estrategia en más de un 5-10%, el stop está sofocando el ruido inherente del activo (*noise whipsaw*).
* **Asimetría de Riesgo:** Evalúa el ratio **MFE / MAE** (*Maximum Favorable Excursion* vs. *MAE*) para auditar si las entradas tienen *edge* direccional inmediato o sufren de ineficiencias de *timing*.
* **Mitigación de Overfitting:** Aplica validación cruzada combinatoria o *Purged Walk-Forward* sobre la calibración del umbral de MAE para evitar sobreajustar el stop al ruido de la muestra.