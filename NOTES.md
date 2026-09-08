# Observaciones de la simulación de Backtest (Faltantes para Despliegue de Capital)

En base a la evaluación cuantitativa realizada bajo marcos de auto-restricción estrictos en la estrategia **Turtle Mean Reversion CME Bitcoin**, se determinan los siguientes criterios superados y fallados, junto con los faltantes indispensables antes de pasar a producción o comprometer capital real:

## Checklist de Viabilidad Cuantitativa 
El sistema de parámetros fijos (`MeanReversion_IBKR_CME_Bitcoin.ipynb`) se evalúa contra un checklist riguroso de robustez operacional y estadística, obteniendo **8 de 12 criterios aprobados**:

*   **[✓] Muestra:** Se dispone de un histórico detallado con cadenas de contratos del CME.
*   **[✗] Folds Out-Of-Sample (OOS):** Solo se miden 6 folds en el walk-forward. El test estadístico t-test carece de potencia con menos de 8.
*   **[✓] Número de trades cerrados en el sistema fijo:** Muestra robusta de transacciones en la serie.
*   **[✓] Ejecutabilidad de señales:** El sizing por N de la posición no arroja contratos fraccionarios menores a la unidad mínima en la mayoría de los casos.
*   **[✓] Consistencia OOS:** Fijo positivo en más del 60% de los folds.
*   **[✓] Retorno medio positivo OOS:** Esperanza matemática positiva en los datos fuera de muestra evaluados.
*   **[✓] Supera costes pesimistas:** Sobrevive tras penalizar la simulación con comisiones del CME y el slippage calibrado.
*   **[✓] Sin sesgo de concentración:** Los 3 mejores trades no explican más del 50% de la ganancia total acumulada.
*   **[✗] Apuestas independientes:** El número efectivo de apuestas independientes es extraordinariamente bajo (`n_eff = 1.00`).
*   **[✓] Desempeño vs Buy & Hold:** Supera al retorno ajustado por riesgo de mantener la moneda directamente.
*   **[✓] Baja probabilidad de quiebra:** Probabilidad por método de Monte Carlo de tener un Drawdown (DD) ≥ 50% es menor al 20%.
*   **[✗] Deflated Sharpe Ratio (DSR):** Sharpe ratio modificado `DSR = 0.374`, muy por debajo del umbral de aceptación comercial de `> 0.95`.

---

## Faltantes y Deficiencias Críticas para Despliegue de Capital

### 1. El Filtro Deflated Sharpe Ratio (DSR)
*   **Problema:** La configuración elegida tiene un ratio Sharpe en Backtesting que **no supera lo que cabría esperar por puro azar** tras probar distintas combinaciones de la rejilla de parámetros.
*   **Implicación:** La aparente rentabilidad es altamente susceptible a ser un mero ajuste de curvas (curve fitting). Con un DSR de `0.374`, la probabilidad de que el rendimiento histórico se deba puramente a suerte en el histórico de prueba es muy alta.
*   **Acción:** Se requiere ampliar el horizonte de la muestra o simplificar drásticamente las reglas para reducir el sesgo de selección en rejilla.

### 2. Número de Apuestas Independientes (`n_eff`)
*   **Problema:** Operar un solo contrato (BTC o MBT) sobre el mismo activo en un marco temporal de 30 minutos reduce el número efectivo de apuestas independientes a `1.00`.
*   **Implicación:** Con `n_eff = 1`, la diversificación estadística es nula. Un drawdown del 50% es el escenario central estadísticamente hablando, no un evento extremo de cola.
*   **Acción:** La estrategia por sí misma no es operable de forma aislada. Debe incorporarse a un portafolio diversificado multiactivo (como se describe en `Turtle_WalkForward_Diversified.ipynb`) para aumentar los grados de libertad operacionales.

### 3. Falta de Ajuste del Roll en la Serie
*   **Problema:** La serie continua de futuros de Bitcoin no está ajustada por el salto del roll (es decir, no es back-adjusted).
*   **Implicación:** La serie continua introduce gaps o saltos artificiales cuando cambia el contrato de primer vencimiento o "front month", distorsionando temporalmente el cálculo de la volatilidad (N) y la ubicación de los canales de Donchian.
*   **Acción:** Implementar un motor de datos que realice un roll ajustado de manera retrospectiva sobre los niveles históricos de precios cerrados.

### 4. Modelo de Margen y Liquidación Simplista
*   **Problema:** El backtest utiliza un límite de apalancamiento bruto como aproximación simplificada en lugar de simular dinámicamente los requisitos de margen inicial y de mantenimiento del CME.
*   **Implicación:** La volatilidad extrema de Bitcoin puede disparar variaciones drásticas en los requisitos de margen del bróker (maintenance margin call), forzando liquidaciones forzosas intradía que el motor no predice en teoría.
*   **Acción:** Desarrollar o acoplar una simulación precisa que calcule los niveles de margen del CME en base al precio de marca y la volatilidad corriente intrabarra.

### 5. Supuestos Intrabarra en la Prelación de Salidas
*   **Problema:** Cuando el mínimo de una barra rompe el Stop Loss (1N) y el máximo de la misma barra alcanza el canal Donchian de salida (Take Profit), el backtest adopta un supuesto pesimista (asume que primero tocó el Stop y luego el TP).
*   **Implicación:** Aunque es una simplificación conservadora que protege el backtest contra sesgos a favor, sesga la distribución de los resultados y omite la verdadera dinámica intrabarra.
*   **Acción:** Utilizar datos intrabarra de mayor resolución (por ejemplo, velas de 1 minuto o ticks) para reconstruir con precisión milimétrica la trayectoria de precios (path-dependent execution) para operaciones rápida.

### 6. Ausencia de Modelo de Microestructura y Ejecución Real
*   **Problema:** No se modelan los rechazos de órdenes, ejecuciones parciales, latencias del socket del API, ni colas del libro de órdenes (FIFO).
*   **Implicación:** El split de comisiones y slippage es estático, asumiendo ejecuciones perfectas sobre el precio teórico de disparo.
*   **Acción:** Validar la degradación de la esperanza matemática mediante simulaciones concurrentes en cuentas Paper que comparen el precio de disparo teórico vs el precio de fill real del bróker en vivo.

### 7. Fallo Silencioso del Bucle de Eventos en Vivo (`trade()`)
*   **Problema:** El bucle `trade()` puede morir en silencio y quedarse ahí. En `trading.py:33-68`, el único `try/except` cubría `events.get(False)` — todo el despacho real (`strategy.calculate_signals`, `portfolio.execute_signal`, `execution.execute_order`, `portfolio.update_fill`) quedaba sin proteger. Cualquier excepción ahí (un `KeyError`, un `AttributeError`) mataba `trade_thread` de raíz, sin traceback visible en el notebook (hilo daemon). Esto explicó una sesión en la que no se lanzó ninguna orden en una hora pese a que ya deberían haberse cerrado varias barras de 30 min.
*   **Desacople relacionado:** Hay además un desacople entre `strategy.open_symbols` (que bloquea nuevas entradas) y `portfolio.positions`, alimentado por el mismo fallo — el motor podía perder de vista una posición real sin que el estado local lo reflejara. Es secundario frente al punto anterior, pero de la misma familia de causa raíz (estado en memoria que no sobrevive a un hilo muerto o a un reinicio del kernel).
*   **Importante — seguridad primero:** el stop del bracket que protege una posición abierta vive en IBKR, no en el estado de Python — si se colocó, sigue descansando en TWS aunque el motor haya perdido la pista. Antes de intervenir sobre cualquier síntoma de este tipo, verificar en TWS/Trader Workstation que la orden de stop sigue activa ("Open Orders") para esa posición. Si no está, la posición queda desprotegida de inmediato.
*   **Estado:** Mitigado el 2026-09-08 — `trading.py` ahora envuelve todo el despacho en `try/except` (se registra y se descarta el evento sin matar el hilo) y expone un pulso (`monitor.heartbeat()`) más un contador de errores (`monitor.record_error()`); `portfolio.py` gana `reconcile_from_broker()` para no arrancar ciego si ya hay una posición viva en la cuenta; `streaming.py` gana reintento de suscripción tras fallos consecutivos de bombeo. Pendiente de verificación en una sesión en vivo prolongada (varias horas, con al menos un cierre de barra y, si es posible, una señal real).
