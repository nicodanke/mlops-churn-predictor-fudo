# 3. La API

> Qué endpoints hay, qué devuelve cada uno y por qué está hecha tan simple.

---

## Qué es y qué no es

Una API **FastAPI de solo lectura**. Diez endpoints, todos `GET`. No entrena, no scorea, no
escribe nada: lee los archivos que dejó el batch y los sirve como JSON.

Esa restricción es a propósito y es la decisión de arquitectura central del proyecto:

| Porque la API no ejecuta el modelo… | …se consigue |
|---|---|
| no necesita XGBoost ni SHAP | imagen de Docker chica, arranca en < 1 segundo |
| no hace cálculo pesado | escala a cero en Cloud Run: cuesta centavos |
| no depende del pipeline en runtime | si el batch falla, el dashboard sigue mostrando el último mes bueno |
| no tiene estado propio | se puede reemplazar, escalar o revertir sin migrar nada |

```
outputs/predictions/202608/predictions.parquet ──┐
outputs/predictions/202608/metadata.json ────────┼──► PredictionStore ──► API ──► JSON
outputs/eda/stats.json ──────────────────────────┤        (pandas)
models/metadata.json ────────────────────────────┘
```

Código: [`api/app/`](../api/app/) · Documentación interactiva autogenerada en `/docs`.

## Cómo lee los datos

No hay base de datos. El parquet de un mes son ~33.000 filas: entra cómodo en memoria y las
consultas se resuelven con pandas.

`PredictionStore` ([`api/app/store.py`](../api/app/store.py)) indexa las carpetas de
`outputs/predictions/`, carga el batch pedido y lo **cachea en memoria**. El caché se
invalida solo si cambia la fecha de modificación del archivo — así, cuando el job mensual
escribe un batch nuevo, la API lo toma sin reiniciarse.

Al arrancar precarga el mes más reciente, para que el primer request no pague la lectura.
Si no hay predicciones todavía, **arranca igual** y responde 404 hasta que corra el batch.

> Cuando el volumen lo pida, `store.py` es el único archivo que hay que cambiar para
> apuntar a BigQuery o Firestore. El resto de la API no sabe de dónde salen los datos.

## El parámetro `?periodo=`

Casi todos los endpoints aceptan `?periodo=YYYYMM`. Si no se manda, se usa el más reciente.
Es lo que permite que el dashboard tenga un selector de mes:

```bash
GET /api/v1/summary              # el último batch
GET /api/v1/summary?periodo=202603   # un mes anterior
GET /api/v1/summary?periodo=209901   # → 404, no hay predicciones para ese período
```

---

## Los endpoints

### Meta

#### `GET /health`

Para el healthcheck de Cloud Run y de `docker compose`. Es el único que no vive bajo
`/api/v1`, y el único que no depende de que haya predicciones.

```json
{
  "status": "ok",
  "predictions_loaded": true,
  "periodos_disponibles": [202608, 202603],
  "model_loaded": true,
  "eda_loaded": true
}
```

`status` es `"degraded"` si no hay ningún batch escrito.

#### `GET /api/v1/me`

Con qué cuenta de Google se inició sesión. **Es informativo, no controla nada**: el control
de acceso lo hace Identity-Aware Proxy antes de que el request llegue al contenedor. La API
solo lee el header `x-goog-authenticated-user-email` que IAP inyecta.

```json
{ "authenticated": true, "email": "ana@fu.do" }
```

En local no hay IAP y devuelve `authenticated: false`. El dashboard lo usa solo para
mostrar el mail arriba a la derecha.

#### `GET /api/v1/periodos`

Los meses que tienen predicciones, del más nuevo al más viejo. Es lo primero que pide el
dashboard, para llenar el selector.

```json
[202608, 202603]
```

#### `GET /api/v1/model`

Metadatos y métricas offline del modelo que generó las predicciones. Sale de
`models/metadata.json`.

```json
{
  "trained_at": "2026-09-22T21:07:12+00:00",
  "n_features": 284,
  "training_periods": [202502, "…", 202601],
  "churn_baseline": 0.0342,
  "decision_threshold": 0.3237,
  "metrics": { "val": { "pr_auc": 0.659, "…": "…" },
               "test": { "pr_auc": 0.6674, "roc_auc": 0.955, "brier": 0.019,
                         "top_k": [ { "fraction": 0.05, "precision": 0.5389,
                                      "recall": 0.6688, "lift": 13.38 } ] } }
}
```

→ 404 si no hay ningún modelo entrenado.

#### `GET /api/v1/eda`

La estadística descriptiva de la base, tal como la dejó `churn eda`. Es **contexto, no
predicción**: responde contra qué base se están leyendo las probabilidades.

Cuatro secciones:

| Sección | Qué trae |
|---|---|
| `panel` | filas, cuentas, períodos y rango del panel |
| `base` | salud de la base: serie de altas y bajas, crecimiento, churn rate, retención anual, vida media, países, cuentas estacionales |
| `adopcion` | qué parte del producto usa una cuenta, sobre el último mes |
| `churn` | en qué se diferencia una cuenta que se va de una que se queda: señales, estado de cobranza, antigüedad |

Es el payload más grande de la API y alimenta toda la pestaña **Uso de la base** del
dashboard. Las tablas de cada sección van como listas de objetos sin tipar campo por campo
(ver el comentario en [`schemas.py`](../api/app/schemas.py)): son listas largas que
cambian cada vez que se agrega una funcionalidad al catálogo, y fijarlas obligaría a tocar
dos archivos por cada cambio del reporte.

### Predicciones

#### `GET /api/v1/summary`

La cabecera del dashboard: cuántas cuentas y cuánta plata hay en riesgo, por categoría.
Sale del `metadata.json` del batch, ya precalculado.

```json
{
  "periodo": 202608,
  "periodo_prediccion": 202609,
  "generated_at": "2026-09-22T21:21:22+00:00",
  "n_accounts": 33517,
  "currency": "USD",
  "pricing_loaded": true,
  "decision_threshold": 0.3237,
  "risk_bands": { "alto": 40.0, "medio": 15.0 },
  "summary": [
    { "risk_category": "alto", "cuentas": 612, "prob_promedio": 74.0,
      "revenue_mensual": 59524.0, "revenue_en_riesgo": 42823.0 },
    { "risk_category": "medio", "cuentas": 415, "…": "…" },
    { "risk_category": "bajo", "cuentas": 55, "…": "…" },
    { "risk_category": "no_churn", "cuentas": 32435, "…": "…" }
  ]
}
```

`pricing_loaded: false` es lo que dispara el cartel de aviso en el dashboard: significa que
la lista de precios está sin cargar y el revenue en riesgo se está mostrando en 0.

#### `GET /api/v1/accounts`

El listado paginado de cuentas. Es el endpoint que hace el trabajo: todos los filtros del
dashboard son parámetros de acá.

| Parámetro | Default | Qué hace |
|---|---|---|
| `risk` | todas | categoría; se repite para varias: `?risk=alto&risk=medio` |
| `search` | — | busca por nombre o por ID de cuenta |
| `pais` | — | filtra por país |
| `min_probability` | — | probabilidad mínima, 0 a 1 |
| `seasonal` | todas | `true` solo estacionales, `false` las excluye |
| `sort_by` | `revenue_at_risk` | columna de ordenamiento |
| `ascending` | `false` | dirección |
| `page` | 1 | página |
| `size` | 50 | filas por página (techo: 200) |

```json
{
  "total": 1082,
  "page": 1,
  "size": 25,
  "pages": 44,
  "items": [
    {
      "id": 123456,
      "nombre": "Restaurante de Ejemplo SRL",
      "plan": "pro-kds-dv-tbl-cl",
      "plan_descripcion": "Plan Pro + Kitchen Display System, Delivery, Mesas (Chile)",
      "pais": "Chile",
      "estado": "BLOCKING_ALERT",
      "periodo": 202608,
      "periodo_prediccion": 202609,
      "churn_probability": 0.9563,
      "monthly_revenue": 148.0,
      "revenue_at_risk": 141.53,
      "risk_category": "alto",
      "will_churn": 1,
      "diagnostico": "Riesgo alto de baja: probabilidad estimada de 96%. …",
      "precio_incompleto": false,
      "pausas_historicas": 0,
      "mes_de_pausa_habitual": null,
      "posible_estacional": false
    }
  ]
}
```

Ordenado por `revenue_at_risk` descendente por defecto: la primera página **es** la lista
de llamados del mes.

#### `GET /api/v1/accounts/{account_id}`

El detalle de una cuenta. Devuelve todo lo de arriba **más** dos campos:

- `top_features`: las 8 features que más pesaron en *esta* cuenta, con su valor, su aporte
  SHAP, la dirección y el percentil contra el resto de la base. Es lo que dibuja el mapa de
  calor del panel lateral.
- `revenue_desglose`: el desglose del precio por plan y módulo, como JSON serializado.

```json
{
  "id": 123456,
  "…": "todos los campos de /accounts",
  "top_features": [
    { "feature": "adiciones_totales",
      "label": "Adiciones totales del mes",
      "group": "Operacion diaria",
      "value": 0.0,
      "shap_value": 1.0372,
      "direction": "aumenta",
      "percentile": 3.3 },
    { "feature": "estado_severity",
      "label": "Severidad del estado de cobranza",
      "group": "Cobranza",
      "value": 3,
      "shap_value": 0.7251,
      "direction": "aumenta",
      "percentile": 90.7 }
  ],
  "revenue_desglose": "[{\"kind\": \"plan\", \"code\": \"pro\", \"price\": 95.0}, …]"
}
```

→ 404 si la cuenta no está en ese batch (se dio de baja, o es de otro mes).

### Explicabilidad

#### `GET /api/v1/importance`

La importancia global de las features **en ese lote**, medida como `|SHAP|` promedio.
Responde "¿qué está mirando el modelo este mes?". Viene precalculada en el `metadata.json`.

```json
[
  { "feature": "estado_severity", "label": "Severidad del estado de cobranza",
    "group": "Cobranza", "mean_abs_shap": 0.8224 },
  { "feature": "adiciones_totales", "label": "Adiciones totales del mes",
    "group": "Operacion diaria", "mean_abs_shap": 0.7192 }
]
```

Ojo con la diferencia: esto es importancia **global** (sobre todo el lote), no la de una
cuenta. La de una cuenta está en `top_features` del detalle.

#### `GET /api/v1/distribution`

Histograma de las probabilidades de churn del lote: cómo se reparte el riesgo en la base.
Acepta `?bins=` (entre 5 y 100, default 20). Es el único endpoint que calcula algo en el
momento — un `pd.cut` sobre la columna de probabilidades.

```json
{
  "threshold": 0.3237,
  "bins": [
    { "desde": -0.001, "hasta": 0.05, "cuentas": 29766 },
    { "desde": 0.05, "hasta": 0.1, "cuentas": 1826 },
    { "desde": 0.1, "hasta": 0.15, "cuentas": 103 }
  ]
}
```

`threshold` viene incluido para que el dashboard pueda dibujar la línea del umbral de
decisión sobre el histograma.

---

## Dos detalles de implementación

**El orden de las rutas importa.** El dashboard estático se monta en `/` **al final** del
archivo, después de declarar todos los endpoints, para que las rutas de la API tengan
prioridad sobre los archivos estáticos. Si se montara antes, `StaticFiles` se comería
`/api/v1/...`.

**Limpieza de tipos.** pandas y numpy devuelven `int64`, `float32` y `NaN`, que pydantic no
acepta. La función `_clean()` los normaliza a tipos de Python y convierte `NaN` en `null`
antes de armar la respuesta.

## Autenticación

**No está en la API.** En GCP el servicio va detrás de **Identity-Aware Proxy**: hay que
iniciar sesión con una cuenta de Google que esté en la lista de acceso, y el request llega
al contenedor solo si pasó ese control. Si llegó, ya está autorizado.

Esa es también la razón por la que **la misma app sirve el dashboard**: la sesión de IAP es
una cookie del dominio del servicio, y un front en otro dominio no podría mandarla.

## Observabilidad

Un middleware emite **una línea de log por request**, con método, ruta, estado y latencia.
El formato se elige solo (`log_format: auto`): JSON dentro de Cloud Run —donde Cloud Logging
lo parsea a campos consultables— y texto legible en local.

```
2026-09-26 18:30:04 INFO app.observability GET /api/v1/summary 200
```

Eso es lo que permite filtrar por campo en la nube:

```bash
make gcp-run-logs-api      # tabla: método, ruta, estado, latencia y revisión
make gcp-run-logs-errors   # solo errores
gcloud logging read 'jsonPayload.event="request" AND jsonPayload.latency_ms>500' --limit=10
```

Código: [`api/app/observability.py`](../api/app/observability.py)

## Configuración

Todo por variables de entorno con prefijo `CHURN_API_` (12-factor, listo para Cloud Run):

| Variable | Default | Para qué |
|---|---|---|
| `CHURN_API_PREDICTIONS_DIR` | `outputs/predictions` | de dónde lee los batches. Acepta `gs://…` |
| `CHURN_API_MODEL_DIR` | `models` | de dónde lee el `metadata.json` del modelo |
| `CHURN_API_EDA_DIR` | `outputs/eda` | de dónde lee `stats.json` |
| `CHURN_API_WEB_DIR` | `web` | dashboard a servir en `/`. Si no existe, no lo sirve |
| `CHURN_API_CORS_ORIGINS` | `*` | orígenes permitidos. En Cloud Run va vacío: mismo origen |
| `CHURN_API_MAX_PAGE_SIZE` | `200` | techo de `?size=` |
| `CHURN_API_LOG_FORMAT` | `auto` | `auto` \| `json` \| `text` |

Código: [`api/app/settings.py`](../api/app/settings.py)

## Cómo levantarla

```bash
make api          # local, sin Docker, en :8000
make up           # con Docker: API en :8000 y dashboard en :8080
```

Y para probar a mano:

```bash
curl localhost:8000/health
curl 'localhost:8000/api/v1/accounts?risk=alto&size=3' | jq '.items[].nombre'
open localhost:8000/docs     # Swagger autogenerado
```

---

**Anterior:** [2. El scoring](02-scoring.md) · **Siguiente:** [4. El dashboard](04-web.md)
