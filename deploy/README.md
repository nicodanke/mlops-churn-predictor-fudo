# Despliegue en GCP

La arquitectura son tres piezas, cada una en la superficie de GCP que le corresponde:

| Pieza | Servicio | Por qué |
|---|---|---|
| Pipeline batch (`prepare` → `train` → `score`) | **Cloud Run Job** + **Cloud Scheduler** | Corre una vez al mes, tarda minutos y después se apaga. Pagar por un servicio siempre encendido no tendría sentido. |
| API de lectura | **Cloud Run Service** | Escala a cero entre consultas del equipo de CX y no carga XGBoost, así que arranca en frío rápido. |
| Dashboard | **Cloud Run Service** (nginx) | Estático; también podría ir a Cloud Storage + CDN. |
| Datos, modelo y predicciones | **Cloud Storage** | El pipeline escribe, la API lee. Un bucket alcanza. |

El código no distingue local de GCP: `pandas` y `pyarrow` leen `gs://` de forma nativa
vía `gcsfs`, así que la única diferencia es qué rutas trae el YAML de configuración.

## Puesta a punto inicial

```bash
export PROJECT_ID=tu-proyecto
export REGION=southamerica-east1
export BUCKET=gs://${PROJECT_ID}-churn

gcloud config set project $PROJECT_ID

gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com cloudscheduler.googleapis.com

gcloud artifacts repositories create churn \
  --repository-format=docker --location=$REGION

gsutil mb -l $REGION $BUCKET
gsutil cp data/account_stats_since_2024.csv $BUCKET/raw/
```

## Build y deploy

```bash
make gcp-build        GCP_PROJECT=$PROJECT_ID GCP_REGION=$REGION
make gcp-deploy-job   GCP_PROJECT=$PROJECT_ID GCP_REGION=$REGION
make gcp-deploy-api   GCP_PROJECT=$PROJECT_ID GCP_REGION=$REGION
make gcp-deploy-web   GCP_PROJECT=$PROJECT_ID GCP_REGION=$REGION
```

Después de desplegar la API, apuntá el dashboard a su URL:

```bash
API_URL=$(gcloud run services describe churn-api --region=$REGION --format='value(status.url)')

gcloud run services update churn-web --region=$REGION \
  --set-env-vars=CHURN_API_URL=$API_URL

# Y restringí el CORS de la API al dominio del dashboard
WEB_URL=$(gcloud run services describe churn-web --region=$REGION --format='value(status.url)')
gcloud run services update churn-api --region=$REGION \
  --set-env-vars=CHURN_API_CORS_ORIGINS=$WEB_URL
```

## Montar el bucket en el job

El pipeline escribe el modelo y las predicciones; la API los lee. Con el volumen de GCS
montado, ninguno de los dos necesita saber que está en la nube:

```bash
gcloud run jobs update churn-pipeline --region=$REGION \
  --set-env-vars=CHURN_CONFIG=/app/deploy/config.gcp.yaml \
  --add-volume=name=churn,type=cloud-storage,bucket=${PROJECT_ID}-churn \
  --add-volume-mount=volume=churn,mount-path=/app/outputs

gcloud run services update churn-api --region=$REGION \
  --add-volume=name=churn,type=cloud-storage,bucket=${PROJECT_ID}-churn,readonly=true \
  --add-volume-mount=volume=churn,mount-path=/app/outputs
```

En `deploy/config.gcp.yaml` hay que reemplazar `CHURN_BUCKET` por el nombre real del
bucket antes de construir la imagen (o montar el archivo como secreto/volumen).

## Cadencia mensual

El canvas define que las predicciones salen dentro de los 5 días del cierre de mes:

```bash
gcloud scheduler jobs create http churn-mensual \
  --location=$REGION \
  --schedule="0 6 5 * *" \
  --time-zone="America/Argentina/Buenos_Aires" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/churn-pipeline:run" \
  --http-method=POST \
  --oauth-service-account-email=$(gcloud iam service-accounts list --format='value(email)' --filter='displayName:Compute Engine default')
```

Para reentrenar sólo cuando hace falta, el job puede ejecutar `churn score` en vez de
`run-all`, y dejar `churn train` para una cadencia trimestral o disparada por deriva.

## Memoria del job

El job necesita **4 GiB**: el pico está en el feature engineering sobre las 707k filas
del panel. Con menos muere por OOM y Cloud Run lo reporta como un fallo de la tarea sin
más detalle. Ya viene configurado así en `make gcp-deploy-job`.

Si el panel crece de forma significativa (más países, más histórico), la etapa a mirar es
`_add_temporal_features`: procesa las métricas de a bloques de `LAG_CHUNK_SIZE` columnas
justamente para acotar ese pico, y bajar ese número cambia memoria por tiempo.

## Notas de costo

- El job usa 4 GiB / 2 vCPU y tarda menos de un minuto: son centavos por corrida.
- API y dashboard con `--min-instances=0` no cuestan nada mientras nadie los use.
- El grueso del gasto termina siendo el almacenamiento del CSV en GCS.
