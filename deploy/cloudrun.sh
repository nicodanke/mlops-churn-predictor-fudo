#!/usr/bin/env bash
# ============================================================================
#  Despliegue y operacion de las piezas en Cloud Run.
#
#  Es la unica definicion de como se configura cada recurso (memoria, cuenta de
#  servicio, volumen del bucket, autenticacion). La usan el workflow de GitHub Actions
#  y los targets `gcp-*` del Makefile, asi que lo que se corre a mano es lo mismo que
#  despliega CI.
#
#    train-job IMAGE           crea/actualiza el job de entrenamiento
#    score-job IMAGE           crea/actualiza el job de scoring mensual
#    app IMAGE                 despliega API + dashboard detras de Identity-Aware Proxy
#    iap-oauth                 aplica un cliente OAuth propio a IAP (cuentas externas);
#                              lee IAP_OAUTH_CLIENT_ID e IAP_OAUTH_CLIENT_SECRET
#    grant MIEMBRO             da acceso a la app (user:EMAIL, group:EMAIL, domain:DOMINIO)
#    revoke MIEMBRO            quita el acceso
#    access                    lista quien tiene acceso
#    retrain VERSION [--force-promote]
#                              entrena el candidato VERSION y decide contra el campeon
#    decision VERSION [md]     muestra la decision (json por defecto)
#    release VERSION [--force] promueve VERSION, fija el scoring a su imagen y scorea
#    score                     corre el scoring con el campeon actual
#    vertex-register VERSION   registra la release en Vertex AI Model Registry
#    url                       URL de la app
#
#  Variables: PROJECT_ID, PROJECT_NUMBER, REGION, BUCKET (nombre, sin gs://).
# ============================================================================
set -euo pipefail

: "${PROJECT_ID:?definir PROJECT_ID}"
: "${PROJECT_NUMBER:?definir PROJECT_NUMBER}"
: "${REGION:?definir REGION}"
: "${BUCKET:?definir BUCKET}"
REGISTRY=${REGISTRY:-${REGION}-docker.pkg.dev/${PROJECT_ID}/churn}

APP=churn-app
IAP_ROLE=roles/iap.httpsResourceAccessor

# Dentro de los contenedores el bucket es un directorio mas.
MOUNT=/mnt/gcs
CANDIDATES=$MOUNT/models/candidates
CHAMPION=$MOUNT/models/champion
RELEASES=$MOUNT/models/releases

sa() { echo "churn-$1@${PROJECT_ID}.iam.gserviceaccount.com"; }

# URL deterministica de Cloud Run: se conoce antes de que el servicio exista.
run_url() { echo "https://$1-${PROJECT_NUMBER}.${REGION}.run.app"; }

# El bucket montado con Cloud Storage FUSE. implicit-dirs hace visibles las "carpetas"
# que aparecen al subir archivos con `gcloud storage cp` (GCS no tiene directorios).
# Se limpia y se vuelve a agregar en cada deploy para que el comando sea idempotente.
volume_flags() {
  local extra=${1:-}
  echo --clear-volumes \
    "--add-volume=name=gcs,type=cloud-storage,bucket=${BUCKET}${extra},mount-options=implicit-dirs" \
    --clear-volume-mounts \
    "--add-volume-mount=volume=gcs,mount-path=${MOUNT}"
}

# IAP directo sobre Cloud Run (sin load balancer) necesita un gcloud reciente.
require_iap_support() {
  if ! gcloud iap web add-iam-policy-binding --help 2>/dev/null | grep -q "cloud-run"; then
    echo "Esta version de gcloud no soporta IAP en Cloud Run." >&2
    echo "Actualizar con: gcloud components update" >&2
    exit 1
  fi
}

deploy_job() {
  local name=$1 image=$2
  shift 2
  # 8 GiB: el pico del feature engineering son ~4 GiB, y en Cloud Run el disco del
  # contenedor (donde queda el cache de features) tambien ocupa memoria.
  # shellcheck disable=SC2046
  gcloud run jobs deploy "$name" \
    --image="$image" \
    --region="$REGION" \
    --service-account="$(sa pipeline)" \
    --cpu=2 --memory=8Gi \
    --task-timeout=3600 --max-retries=0 \
    --set-env-vars=CHURN_CONFIG=/app/config/gcp.yaml \
    $(volume_flags) \
    "$@"
}

execute() {
  local job=$1 args=${2:-}
  if [[ -n "$args" ]]; then
    gcloud run jobs execute "$job" --region="$REGION" --wait --args="$args"
  else
    gcloud run jobs execute "$job" --region="$REGION" --wait
  fi
}

cmd_train_job() {
  deploy_job churn-train "${1:?uso: train-job IMAGE}"
}

cmd_score_job() {
  deploy_job churn-score "${1:?uso: score-job IMAGE}" \
    --args="score,--model-dir,${CHAMPION},--pricing,${MOUNT}/config/pricing.yaml"
  # Cloud Scheduler dispara este job el dia 5 de cada mes (ver bootstrap.sh).
  gcloud run jobs add-iam-policy-binding churn-score --region="$REGION" \
    --member="serviceAccount:$(sa scheduler)" --role=roles/run.invoker >/dev/null
}

cmd_retrain() {
  local version=${1:?uso: retrain VERSION [--force-promote]}
  local args="retrain,--candidate-dir,${CANDIDATES}/${version},--champion-dir,${CHAMPION},--version,${version}"
  if [[ "${2:-}" == "--force-promote" ]]; then
    args+=",--force-promote"
  fi
  execute churn-train "$args"
}

cmd_decision() {
  local version=${1:?uso: decision VERSION [md]}
  local file=decision.json
  if [[ "${2:-}" == "md" ]]; then
    file=decision.md
  fi
  gcloud storage cat "gs://${BUCKET}/models/candidates/${version}/${file}"
}

cmd_release() {
  local version=${1:?uso: release VERSION [--force]}
  local source="${CANDIDATES}/${version}"
  # Rollback a una version cuyo candidato ya borro el ciclo de vida del bucket.
  if ! gcloud storage objects describe "gs://${BUCKET}/models/candidates/${version}/decision.json" \
    >/dev/null 2>&1; then
    source="${RELEASES}/${version}"
  fi

  local args="promote,--candidate-dir,${source},--champion-dir,${CHAMPION}"
  if [[ "${2:-}" == "--force" ]]; then
    args+=",--force"
  fi
  execute churn-train "$args"

  # El scoring corre con el codigo del campeon, no con el ultimo commit: un candidato
  # que perdio puede traer otro feature engineering, y ese modelo no sabria leerlo.
  # Los tags evitan que la politica de limpieza de Artifact Registry borre la imagen.
  local image="${REGISTRY}/pipeline:${version}"
  gcloud artifacts docker tags add "$image" "${REGISTRY}/pipeline:champion" --quiet
  gcloud artifacts docker tags add "$image" "${REGISTRY}/pipeline:release-${version:0:12}" --quiet
  cmd_score_job "$image"
  cmd_score
}

cmd_score() {
  execute churn-score
}

cmd_app() {
  local image=${1:?uso: app IMAGE}
  require_iap_support

  # Sin acceso publico. Todo request pasa por Identity-Aware Proxy, que exige iniciar
  # sesion con Google y estar en la lista de acceso (ver `grant`) antes de llegar al
  # contenedor. Cubre el dashboard, la API y /docs por igual.
  # CORS vacio: el dashboard se sirve desde este mismo servicio.
  # shellcheck disable=SC2046
  gcloud run deploy "$APP" \
    --image="$image" \
    --region="$REGION" \
    --service-account="$(sa app)" \
    --execution-environment=gen2 \
    --cpu=1 --memory=1Gi \
    --min-instances=0 --max-instances=2 \
    --no-allow-unauthenticated \
    --iap \
    --set-env-vars="CHURN_API_PREDICTIONS_DIR=${MOUNT}/predictions,CHURN_API_MODEL_DIR=${CHAMPION},CHURN_API_CORS_ORIGINS=" \
    $(volume_flags ",readonly=true")

  # Por las dudas de que alguna vez se haya desplegado publico.
  gcloud run services remove-iam-policy-binding "$APP" --region="$REGION" \
    --member=allUsers --role=roles/run.invoker >/dev/null 2>&1 || true

  # IAP le reenvia al servicio los requests ya autenticados, con su propia identidad.
  gcloud run services add-iam-policy-binding "$APP" --region="$REGION" \
    --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com" \
    --role=roles/run.invoker >/dev/null
}

cmd_iap_oauth() {
  : "${IAP_OAUTH_CLIENT_ID:?definir IAP_OAUTH_CLIENT_ID}"
  : "${IAP_OAUTH_CLIENT_SECRET:?definir IAP_OAUTH_CLIENT_SECRET}"
  require_iap_support

  # El secreto solo pasa por este archivo temporal: no queda en el repo ni en GitHub.
  local settings
  settings=$(mktemp)
  # shellcheck disable=SC2064
  trap "rm -f '${settings}'" EXIT
  cat >"$settings" <<EOF
accessSettings:
  oauthSettings:
    clientId: ${IAP_OAUTH_CLIENT_ID}
    clientSecret: ${IAP_OAUTH_CLIENT_SECRET}
EOF

  # Solo para este servicio y no para todo el proyecto: otros servicios de Cloud Run que
  # usen IAP en el mismo proyecto siguen con su propia configuracion.
  gcloud iap settings set "$settings" \
    --project="$PROJECT_ID" --resource-type=cloud-run --region="$REGION" --service="$APP" \
    >/dev/null
  echo "IAP de ${APP} usa el cliente OAuth ${IAP_OAUTH_CLIENT_ID}"
}

# Acepta un email suelto (se asume usuario) o el prefijo explicito de IAM. Nunca deja
# abrir el acceso a cualquiera: el dashboard expone datos de cuentas reales.
normalize_member() {
  local member=$1
  case "$member" in
    allUsers | allAuthenticatedUsers)
      echo "No se permite '$member': el acceso tiene que ser a personas, grupos o dominios." >&2
      return 1
      ;;
    user:* | group:* | domain:*) echo "$member" ;;
    *@*) echo "user:$member" ;;
    *)
      echo "Miembro invalido '$member'. Usar user:EMAIL, group:EMAIL o domain:DOMINIO." >&2
      return 1
      ;;
  esac
}

cmd_grant() {
  local member
  member=$(normalize_member "${1:?uso: grant user:EMAIL | group:EMAIL | domain:DOMINIO}")
  require_iap_support
  gcloud iap web add-iam-policy-binding \
    --resource-type=cloud-run --service="$APP" --region="$REGION" \
    --member="$member" --role="$IAP_ROLE" >/dev/null
  echo "Acceso otorgado a ${member}"
}

cmd_revoke() {
  local member
  member=$(normalize_member "${1:?uso: revoke user:EMAIL | group:EMAIL | domain:DOMINIO}")
  require_iap_support
  gcloud iap web remove-iam-policy-binding \
    --resource-type=cloud-run --service="$APP" --region="$REGION" \
    --member="$member" --role="$IAP_ROLE" >/dev/null
  echo "Acceso quitado a ${member}"
}

cmd_access() {
  require_iap_support
  gcloud iap web get-iam-policy \
    --resource-type=cloud-run --service="$APP" --region="$REGION" \
    --flatten="bindings[].members" \
    --filter="bindings.role=${IAP_ROLE}" \
    --format="value(bindings.members)"
}

cmd_vertex_register() {
  local version=${1:?uso: vertex-register VERSION}
  local parent
  parent=$(gcloud ai models list --region="$REGION" --filter="displayName=churn-fudo" \
    --format="value(name)" 2>/dev/null | head -n1 || true)

  # El registry de Vertex es solo catalogo: el modelo no se sirve desde un endpoint (la
  # API lee predicciones batch), asi que no hay costo. La imagen declarada es la del
  # pipeline, que es el unico entorno que sabe cargar el artefacto.
  local parent_flag=()
  if [[ -n "$parent" ]]; then
    parent_flag=(--parent-model="$parent")
  fi
  gcloud ai models upload \
    --region="$REGION" \
    --display-name=churn-fudo \
    --description="Clasificador de churn de cuentas Fudo (XGBoost calibrado)" \
    --artifact-uri="gs://${BUCKET}/models/releases/${version}" \
    --container-image-uri="${REGISTRY}/pipeline:${version}" \
    --version-aliases=champion \
    --labels="git_sha=${version:0:12}" \
    ${parent_flag[@]+"${parent_flag[@]}"}
}

cmd_url() {
  echo "App (pide iniciar sesion con Google)  $(run_url "$APP")"
  echo "Documentacion de la API               $(run_url "$APP")/docs"
}

command=${1:-}
shift || true
case "$command" in
  train-job) cmd_train_job "$@" ;;
  score-job) cmd_score_job "$@" ;;
  app) cmd_app "$@" ;;
  iap-oauth) cmd_iap_oauth ;;
  grant) cmd_grant "$@" ;;
  revoke) cmd_revoke "$@" ;;
  access) cmd_access ;;
  retrain) cmd_retrain "$@" ;;
  decision) cmd_decision "$@" ;;
  release) cmd_release "$@" ;;
  score) cmd_score ;;
  vertex-register) cmd_vertex_register "$@" ;;
  url) cmd_url ;;
  *)
    sed -n '3,/^# =====/p' "$0"
    exit 1
    ;;
esac
