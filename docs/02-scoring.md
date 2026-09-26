# 2. El proceso de scoring

> Cómo se pasa del CSV del mes a las predicciones que ve el equipo de CX.

---

## Qué es el scoring

**Entrenar** y **scorear** son dos cosas distintas que conviene no mezclar:

| | Entrenar | Scorear |
|---|---|---|
| Cuándo | cuando hay datos nuevos o cambia el código | todos los meses |
| Qué usa | los meses **con etiqueta confirmada** | el snapshot **más reciente** |
| Qué produce | un modelo (`churn_model.joblib`) | un lote de predicciones (`predictions.parquet`) |
| Cuánto tarda | minutos | menos de un minuto |

El scoring es el job que agarra el modelo ya entrenado, lo aplica a las cuentas activas de
este mes, y **deja el resultado escrito en disco**. Eso es todo: no hay servidor de
inferencia, no hay predicción en vivo. Corre una vez al mes y se apaga.

Código: [`src/churn/scoring/batch.py`](../src/churn/scoring/batch.py)

## El pipeline completo

```
  ┌─ prepare ─────────────────────────────────────────────┐
  │  1. Lee los CSV de data/ y los apila en un panel      │
  │  2. Infiere la etiqueta de churn (ventana de 3 meses) │
  │  3. Construye las 296 features                        │
  │  → cachea en outputs/interim/features.parquet         │
  └───────────────────────────────────────────────────────┘
                          │
                          ▼
  ┌─ train ───────────────────────────────────────────────┐
  │  Split temporal → XGBoost → calibración → evaluación  │
  │  → models/churn_model.joblib + metadata.json          │
  └───────────────────────────────────────────────────────┘
                          │
                          ▼
  ┌─ score ───────────────────────────────────────────────┐  ← esto es lo que corre
  │  El paso que se detalla abajo                         │     todos los meses
  │  → outputs/predictions/YYYYMM/                        │
  └───────────────────────────────────────────────────────┘
                          │
                          ▼
  ┌─ eda ─────────────────────────────────────────────────┐
  │  Estadística descriptiva de la base                   │
  │  → outputs/eda/stats.json                             │
  └───────────────────────────────────────────────────────┘
```

`make all` corre los tres primeros pasos en orden. El cuarto no hace falta pedirlo:
`score` deja el reporte de `eda` de paso, para que las dos pestañas del dashboard hablen
siempre del mismo mes (se puede saltear con `churn score --no-eda`).

## Los seis pasos del scoring

Todo pasa dentro de `score_period()`. Se scorea un período (por defecto el más reciente del
panel) y se predice la baja del mes **siguiente**: sobre el snapshot de 202608 se predice
quién no va a estar en 202609.

### 1. Elegir las cuentas

Se filtran las filas del período pedido: las 33.517 cuentas activas del mes. Nótese que acá
**no** se descarta nada — ni las cuentas nuevas, ni las ambiguas, ni las estacionales. Esos
filtros eran para entrenar; en producción hay que darle un número a todas.

### 2. Predecir la probabilidad

```python
X = batch[artifact.feature_names]     # las columnas exactas del contrato, en orden
probabilities = artifact.predict_proba(X)   # XGBoost + calibrador isotónico
```

Sale un número entre 0 y 1 por cuenta, ya calibrado.

### 3. Calcular el revenue de cada cuenta

Acá aparece algo importante: **el revenue no entra al modelo**. El modelo solo estima
probabilidad; la plata se usa después, para priorizar.

El revenue sale de decodificar el `plan code` que tiene contratado la cuenta. Un plan code
es una cadena con el plan base, los módulos y el mercado:

```
pro-kds-dv-tbl-cl
 │   │   │   │   └── mercado: Chile
 │   │   │   └────── módulo: Mesas
 │   │   └────────── módulo: Delivery
 │   └────────────── módulo: Kitchen Display System
 └────────────────── plan base: Pro

revenue = plans.pro.cl + modules.kds.cl + modules.dv.cl + modules.tbl.cl
        = 95 + 20 + 18 + 15 = 148 USD/mes
```

Los precios se cargan **a mano** en `config/pricing.yaml` (7 planes × 8 módulos × 7
mercados = 82 valores). No están en git.

```bash
make pricing-template   # genera el YAML con todos los planes y módulos vistos en los datos
make pricing-check      # muestra qué falta completar y a cuántas cuentas afecta
```

Si el archivo está vacío o incompleto, el sistema **no se rompe**: el revenue sale 0, queda
un `WARNING` en el log y el dashboard muestra un cartel de aviso. Para ver el sistema
funcionando sin la lista real hay precios inventados en `config/pricing.example.yaml`:

```bash
make score-demo
```

Código: [`src/churn/pricing/`](../src/churn/pricing/)

### 4. Convertir probabilidad en categoría de riesgo

Este es el paso que traduce el modelo a una decisión operativa. Hay dos cortes encadenados:

```
                    ¿prob >= umbral de decisión (0.324)?
                              │
                 ┌── no ──────┴────── sí ──┐
                 ▼                          ▼
             no_churn            revenue_at_risk = prob × revenue mensual
                                            │
                              ┌─────────────┼─────────────┐
                              ▼             ▼             ▼
                        >= 40 USD/mes  >= 15 USD/mes    resto
                            alto          medio          bajo
```

El **umbral de decisión** no está puesto a dedo: se calibra automáticamente sobre
validación buscando el punto de mejor F1 (dio 0.324). Se puede fijar a mano en
`risk.no_churn_threshold`.

Los **cortes de plata** (40 y 15 USD/mes) sí son una decisión de negocio, y viven en
`risk.revenue_at_risk_bands` de [`config/model.yaml`](../config/model.yaml).

La lógica de las dos etapas es deliberada: una cuenta puede tener 95 % de probabilidad de
baja y caer en `bajo` porque paga 12 USD/mes. No es que no se vaya — es que hay 612 cuentas
más caras a las que llamar primero. Así se reparte el resultado de un mes real:

| Categoría | Cuentas | Prob. promedio | Revenue en riesgo |
|---|---|---|---|
| `alto` | 612 | 74 % | 42.823 USD/mes |
| `medio` | 415 | 56 % | 11.612 USD/mes |
| `bajo` | 55 | 41 % | 631 USD/mes |
| `no_churn` | 32.435 | 1.5 % | — |

Código: [`src/churn/scoring/risk.py`](../src/churn/scoring/risk.py)

### 5. Explicar cada predicción

Una probabilidad sin explicación es inútil para quien tiene que levantar el teléfono. Por
eso cada predicción viene con las **8 features que más pesaron en esa cuenta**, calculadas
con **SHAP** (`TreeExplainer`).

Cada una trae cuatro datos:

| Campo | Qué es |
|---|---|
| `label` | el nombre legible (*"Adiciones totales del mes"*, no `adiciones_totales`) |
| `value` | el valor de esa cuenta en esa feature |
| `shap_value` + `direction` | cuánto aportó al riesgo y para qué lado (`aumenta` / `reduce`) |
| `percentile` | dónde está esa cuenta respecto del resto de la base **de ese mes** |

El percentil es el que hace la explicación interpretable: saber que la cuenta tuvo 0
adiciones no dice nada si no se sabe que eso la pone en el percentil 3 de toda la base.
Es lo que alimenta el mapa de calor del dashboard.

De paso se guarda la **importancia global** del lote (el `|SHAP|` promedio de cada feature),
que es lo que responde "¿qué está mirando el modelo este mes?".

### 6. Escribir un texto listo para leer

Con esas contribuciones se arma un diagnóstico en castellano, usando plantillas:

> Riesgo alto de baja: probabilidad estimada de 96%. Representa 148 USD/mes, con 142
> USD/mes en riesgo. Lo que más empuja el riesgo: adiciones totales del mes (muy por debajo
> del resto de la base, percentil 3); severidad del estado de cobranza (muy por encima del
> resto de la base, percentil 91); cambio en el estado de cobranza vs mes anterior (muy por
> encima del resto de la base, percentil 92).

Es **determinista, gratis y no puede alucinar**: el texto se arma con las contribuciones
SHAP, así que nunca puede nombrar una causa que el modelo no usó.

Hay un modo alternativo que, **solo para las cuentas de riesgo alto**, le pasa esas mismas
contribuciones a Claude para que redacte un párrafo más natural y sugiera una acción:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
make score-llm
```

El insumo es el mismo en los dos casos. El LLM redacta; no decide ni interpreta el modelo.

Código: [`src/churn/explain/`](../src/churn/explain/)

### Extra: el aviso de estacionalidad

Cada predicción lleva además tres campos de contexto: `pausas_historicas`,
`mes_de_pausa_habitual` y `posible_estacional`. Sirven para que CX lea la predicción con
criterio: una cuenta que ya pausó dos veces y vuelve a caer en mayo probablemente sea un
negocio de temporada, no una baja que haya que salir a rescatar.

> 🔍 Este cálculo mira **solo hasta el mes que se scorea**, nunca el futuro. Es la versión
> causal de la detección de estacionalidad: el filtro que se usa para limpiar el
> entrenamiento mira el panel entero, y eso en producción no existe.

## Qué queda escrito

```
outputs/predictions/
├── latest.json              puntero al último período, para que la API no adivine
├── 202608/
│   ├── predictions.parquet  ← la fuente de verdad. Es lo que lee la API
│   ├── predictions.json     mismo contenido, para inspeccionar a mano
│   └── metadata.json        el resumen del lote
└── 202603/
    └── ...                  los meses anteriores quedan, no se pisan
```

Una fila por cuenta, con 20 columnas:

```json
{
  "id": 123456,
  "periodo": 202608,
  "periodo_prediccion": 202609,
  "nombre": "Restaurante de Ejemplo SRL",
  "plan": "pro-kds-dv-tbl-cl",
  "plan_descripcion": "Plan Pro + Kitchen Display System, Delivery, Mesas (Chile)",
  "pais": "Chile",
  "estado": "BLOCKING_ALERT",
  "churn_probability": 0.956311,
  "monthly_revenue": 148.0,
  "revenue_at_risk": 141.53,
  "risk_category": "alto",
  "will_churn": 1,
  "revenue_desglose": "[{\"kind\": \"plan\", \"code\": \"pro\", \"price\": 95.0}, ...]",
  "precio_incompleto": false,
  "pausas_historicas": 0,
  "mes_de_pausa_habitual": null,
  "posible_estacional": false,
  "top_features": [ { "label": "Adiciones totales del mes", "value": 0.0,
                      "shap_value": 1.037, "direction": "aumenta",
                      "percentile": 3.3 }, "… 7 más" ],
  "diagnostico": "Riesgo alto de baja: probabilidad estimada de 96%. …"
}
```

Y el `metadata.json`, que es el resumen del lote —lo que la API sirve en `/summary`,
`/importance` y para la cabecera del dashboard:

```json
{
  "periodo_snapshot": 202608,
  "periodo_prediccion": 202609,
  "generated_at": "2026-09-22T21:21:22+00:00",
  "n_accounts": 33517,
  "model_trained_at": "…",
  "model_metrics": { "val": {…}, "test": {…} },
  "decision_threshold": 0.3237,
  "risk_bands": { "alto": 40.0, "medio": 15.0 },
  "currency": "USD",
  "pricing_loaded": true,
  "narrative_mode": "rules",
  "summary": [ { "risk_category": "alto", "cuentas": 612, … } ],
  "global_importance": [ { "feature": "estado_severity", "mean_abs_shap": 0.822, … } ]
}
```

Nada de esto se sobreescribe: cada mes crea su carpeta. Por eso el dashboard tiene un
selector de período y se puede volver a un mes anterior sin recalcular nada.

## Cómo se corre

```bash
make score                      # el último período, con config/pricing.yaml
make score-demo                 # con los precios de ejemplo
make score-llm                  # diagnósticos redactados por Claude (cuentas de riesgo alto)

# O con el CLI, para más control:
churn score --periodo 202603    # un mes puntual
churn score --no-eda            # sin regenerar la estadística de la base
churn score --pricing config/pricing.example.yaml
```

En GCP el equivalente es un **Cloud Run Job** disparado por Cloud Scheduler una vez al mes,
fijado a la imagen del modelo campeón. Ver [5. El deploy](05-deploy.md).

## Monitoreo: ¿sigue sirviendo el modelo?

El mundo se mueve y el modelo no. La forma de detectarlo es comparar los datos del mes que
se scorea contra los meses con los que se entrenó, feature por feature, con **PSI**
(Population Stability Index):

```bash
make drift
```

| PSI | Lectura |
|---|---|
| < 0.10 | ruido, no pasa nada |
| 0.10 – 0.25 | conviene mirarlo |
| > 0.25 | el modelo está viendo otro mundo: reentrenar |

Código: [`src/churn/monitoring/drift.py`](../src/churn/monitoring/drift.py)

---

**Anterior:** [1. El modelo](01-modelo.md) · **Siguiente:** [3. La API](03-api.md)
