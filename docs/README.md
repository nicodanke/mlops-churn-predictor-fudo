# Documentación del proyecto

Cinco documentos, uno por pieza del sistema. Están pensados para leerse en orden, pero
cada uno se entiende solo. La idea es que alguien que nunca vio el repo pueda entender
qué hace cada parte sin leer código.

| # | Documento | Responde a |
|---|---|---|
| 1 | [El modelo](01-modelo.md) | ¿Qué predice, con qué datos y por qué se eligió así? |
| 2 | [El scoring](02-scoring.md) | ¿Cómo se pasa del CSV del mes a las predicciones escritas en disco? |
| 3 | [La API](03-api.md) | ¿Qué endpoints hay y qué devuelve cada uno? |
| 4 | [El dashboard](04-web.md) | ¿Con qué está hecha la web y cómo le pide los datos a la API? |
| 5 | [El deploy en GCP](05-deploy.md) | ¿Cómo se sube a la nube y qué comando de `make` hace cada cosa? |

El [`README.md`](../README.md) de la raíz es la versión larga y con los números de
respaldo (métricas, experimentos, decisiones descartadas). Estos documentos son la
versión corta.

---

## El sistema en un minuto

Fudo es un SaaS para restaurantes. Cada mes, algunas cuentas se dan de baja. Este
proyecto intenta adivinar **cuáles se van a ir el mes que viene**, para que el equipo de
Customer Experience las llame antes.

El recorrido completo, de punta a punta:

```
  data/account-stats-YYYYMM.csv          un CSV por mes, con 82 métricas por cuenta
             │
             ▼
  ┌──────────────────────────┐
  │  1. PIPELINE (batch)     │           corre una vez por mes, tarda minutos
  │  prepare → train → score │
  └──────────┬───────────────┘
             │  escribe archivos
             ▼
  outputs/predictions/YYYYMM/             una fila por cuenta: probabilidad, plata en
      predictions.parquet                 riesgo, categoría y por qué
      metadata.json
  outputs/eda/stats.json
             │  lee
             ▼
  ┌──────────────────────────┐
  │  2. API (FastAPI)        │           solo lectura, no ejecuta el modelo
  └──────────┬───────────────┘
             │  HTTP / JSON
             ▼
  ┌──────────────────────────┐
  │  3. DASHBOARD (web)      │           HTML + CSS + JS, sin frameworks
  └──────────────────────────┘
```

La decisión de diseño más importante está en esa separación: **el modelo corre en batch y
la API solo lee lo que el batch dejó escrito**. Nunca se predice "en vivo".

Eso tiene tres consecuencias prácticas:

- la API no carga XGBoost ni SHAP, así que arranca en menos de un segundo y su imagen de
  Docker es chica;
- se puede hostear en Cloud Run escalando a cero (cuesta centavos);
- si el pipeline se rompe, el dashboard sigue mostrando el último mes bueno.

## Vocabulario

Estas palabras aparecen en todos los documentos.

| Término | Qué significa acá |
|---|---|
| **cuenta** | un restaurante cliente de Fudo. La unidad que se predice. |
| **período** | un mes, escrito `YYYYMM` (`202608` = agosto de 2026). |
| **snapshot** | la foto de todas las cuentas activas de un mes: un CSV. |
| **churn / baja** | la cuenta dejó de ser cliente. |
| **panel** | todos los snapshots apilados: una fila por (cuenta, mes). |
| **feature** | una columna que el modelo usa para predecir. |
| **etiqueta / label** | lo que se quiere predecir: ¿esta cuenta se fue? (1) ¿o se quedó? (0) |
| **batch** | el lote de predicciones de un mes, ya calculado y escrito. |
| **revenue en riesgo** | `probabilidad de baja × facturación mensual de la cuenta`. |
| **artefacto** | el archivo con el modelo entrenado, sus features y sus métricas. |
