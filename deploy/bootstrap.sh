#!/usr/bin/env bash
# ============================================================================
#  Puesta a punto de GCP, una sola vez por proyecto.
#
#  Crea todo lo que el workflow de despliegue da por existente: bucket con los datos,
#  repositorio de imagenes, cuentas de servicio con permisos minimos, la federacion de
#  identidad para que GitHub Actions entre sin claves, la cuenta de servicio de IAP y el
#  scheduler del scoring mensual. Es idempotente: volver a correrlo solo completa lo que
#  falte.
#
#  Todo lo que crea lleva el prefijo churn, para poder convivir con otros sistemas en el
#  mismo proyecto sin pisarlos.
#
#      make gcp-bootstrap GCP_PROJECT=mi-proyecto
#
#  Variables: PROJECT_ID y GITHUB_REPO (usuario/repositorio) obligatorias. Opcionales:
#  REGION, BUCKET, BILLING_ACCOUNT (crea una alerta de presupuesto) y
#  VERTEX_MODEL_REGISTRY=true (permite a CI registrar versiones en Vertex AI).
# ============================================================================
set -euo pipefail

: "${PROJECT_ID:?definir PROJECT_ID}"
: "${GITHUB_REPO:?definir GITHUB_REPO como usuario/repositorio}"
REGION=${REGION:-us-central1}
# Sin "-fudo" el nombre choca con el bucket del lab de la clase 4 (PROYECTO-churn), y el
# pipeline leeria el CSV de Telco que ese lab deja en raw/.
BUCKET=${BUCKET:-${PROJECT_ID}-churn-fudo}
BUDGET_USD=${BUDGET_USD:-10}
REPO=churn
POOL=churn-github

ROOT=$(cd "$(dirname "$0")/.." && pwd)

paso() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
sa_email() { echo "churn-$1@${PROJECT_ID}.iam.gserviceaccount.com"; }
member() { echo "serviceAccount:$(sa_email "$1")"; }

# Antes de crear nada: IAP directo sobre Cloud Run necesita un gcloud reciente.
if ! gcloud iap web add-iam-policy-binding --help 2>/dev/null | grep -q "cloud-run"; then
  echo "Esta version de gcloud no soporta IAP en Cloud Run. Actualizar con:" >&2
  echo "    gcloud components update" >&2
  exit 1
fi

gcloud config set project "$PROJECT_ID" >/dev/null
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

# ------------------------------------------------------------------------------
paso "Habilitando APIs"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com \
  storage.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  iap.googleapis.com \
  aiplatform.googleapis.com

# ------------------------------------------------------------------------------
paso "Bucket gs://${BUCKET}"
if ! gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" \
    --location="$REGION" \
    --uniform-bucket-level-access \
    --public-access-prevention
fi

# Los candidatos que no se promovieron no sirven despues de un tiempo. Los campeones se
# archivan aparte en models/releases/, que no vence.
lifecycle=$(mktemp)
cat >"$lifecycle" <<'JSON'
{"rule": [{"action": {"type": "Delete"},
           "condition": {"age": 180, "matchesPrefix": ["models/candidates/"]}}]}
JSON
gcloud storage buckets update "gs://${BUCKET}" --lifecycle-file="$lifecycle" >/dev/null

paso "Subiendo datos (comprimidos: el CSV pesa ~160 MB y gzip lo baja a una fraccion)"
shopt -s nullglob
for csv in "$ROOT"/data/*.csv; do
  destino="gs://${BUCKET}/raw/$(basename "$csv").gz"
  if gcloud storage objects describe "$destino" >/dev/null 2>&1; then
    echo "  ya existe $destino"
  else
    echo "  $csv -> $destino"
    gzip -c "$csv" | gcloud storage cp - "$destino"
  fi
done

# La lista de precios real no esta en git; si no existe local se sube la de ejemplo.
if ! gcloud storage objects describe "gs://${BUCKET}/config/pricing.yaml" >/dev/null 2>&1; then
  pricing="$ROOT/config/pricing.yaml"
  [[ -f "$pricing" ]] || pricing="$ROOT/config/pricing.example.yaml"
  echo "  $pricing -> gs://${BUCKET}/config/pricing.yaml"
  gcloud storage cp "$pricing" "gs://${BUCKET}/config/pricing.yaml"
fi

# ------------------------------------------------------------------------------
paso "Artifact Registry"
if ! gcloud artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$REPO" \
    --repository-format=docker \
    --location="$REGION" \
    --description="Imagenes del predictor de churn"
fi

# Solo los primeros 0.5 GB son gratis y la imagen del pipeline es pesada (XGBoost). Se
# conservan las 3 ultimas de cada imagen y las que fueron campeonas (para el rollback).
# La politica es del repositorio churn: no toca otros repositorios del proyecto.
policy=$(mktemp)
cat >"$policy" <<'JSON'
[
  {"name": "conservar-releases", "action": {"type": "Keep"},
   "condition": {"tagState": "tagged", "tagPrefixes": ["champion", "release-"]}},
  {"name": "conservar-recientes", "action": {"type": "Keep"},
   "mostRecentVersions": {"keepCount": 3}},
  {"name": "borrar-viejas", "action": {"type": "Delete"},
   "condition": {"tagState": "any", "olderThan": "7d"}}
]
JSON
gcloud artifacts repositories set-cleanup-policies "$REPO" \
  --location="$REGION" --policy="$policy" --no-dry-run >/dev/null

# ------------------------------------------------------------------------------
paso "Cuentas de servicio"
creadas=0
crear_sa() {
  if ! gcloud iam service-accounts describe "$(sa_email "$1")" >/dev/null 2>&1; then
    gcloud iam service-accounts create "churn-$1" --display-name="$2"
    creadas=1
  fi
}
crear_sa deployer "GitHub Actions: build y deploy"
crear_sa pipeline "Cloud Run Jobs: entrenamiento y scoring"
crear_sa app "Cloud Run: API y dashboard"
crear_sa scheduler "Cloud Scheduler: scoring mensual"
# IAM tarda unos segundos en ver una cuenta recien creada.
if [[ $creadas == 1 ]]; then sleep 15; fi

paso "Permisos (minimos por pieza)"
bucket_role() {
  gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
    --member="$(member "$1")" --role="$2" >/dev/null
}
project_role() {
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="$(member "$1")" --role="$2" --condition=None >/dev/null
}

bucket_role pipeline roles/storage.objectUser   # lee datos, escribe modelos y predicciones
bucket_role app roles/storage.objectViewer      # solo lee predicciones y el campeon
bucket_role deployer roles/storage.objectViewer # lee decision.json para decidir en CI

project_role deployer roles/run.admin
if [[ "${VERTEX_MODEL_REGISTRY:-}" == "true" ]]; then
  project_role deployer roles/aiplatform.user   # registro de versiones en Vertex AI
fi
gcloud artifacts repositories add-iam-policy-binding "$REPO" --location="$REGION" \
  --member="$(member deployer)" --role=roles/artifactregistry.writer >/dev/null

# El deployer despliega recursos que corren como estas cuentas.
for runtime in pipeline app; do
  gcloud iam service-accounts add-iam-policy-binding "$(sa_email "$runtime")" \
    --member="$(member deployer)" --role=roles/iam.serviceAccountUser >/dev/null
done

# ------------------------------------------------------------------------------
paso "Workload Identity Federation: GitHub Actions entra sin claves JSON"
if ! gcloud iam workload-identity-pools describe "$POOL" --location=global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$POOL" \
    --location=global --display-name="Churn: GitHub Actions"
fi
if ! gcloud iam workload-identity-pools providers describe github \
  --location=global --workload-identity-pool="$POOL" >/dev/null 2>&1; then
  # Solo este repositorio y solo la rama main pueden asumir la identidad del deployer.
  gcloud iam workload-identity-pools providers create-oidc github \
    --location=global \
    --workload-identity-pool="$POOL" \
    --display-name="GitHub OIDC" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition="assertion.repository == '${GITHUB_REPO}' && assertion.ref == 'refs/heads/main'"
fi
gcloud iam service-accounts add-iam-policy-binding "$(sa_email deployer)" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GITHUB_REPO}" \
  >/dev/null

# ------------------------------------------------------------------------------
paso "Identity-Aware Proxy"
# Cuenta de servicio de IAP: es la que invoca a la app en nombre de quien inicio sesion.
gcloud services identity create --service=iap.googleapis.com --project="$PROJECT_ID" \
  >/dev/null 2>&1 ||
  gcloud beta services identity create --service=iap.googleapis.com --project="$PROJECT_ID" \
    --quiet >/dev/null

organizaciones=$(gcloud projects get-ancestors "$PROJECT_ID" --format='value(type)' |
  grep -c organization || true)
if [[ "$organizaciones" == "0" ]]; then
  echo "  El proyecto no pertenece a una organizacion: despues del primer despliegue hace"
  echo "  falta un cliente OAuth propio (make gcp-iap-oauth) o nadie va a poder entrar."
else
  echo "  El proyecto pertenece a una organizacion: sin mas configuracion solo pueden entrar"
  echo "  cuentas de esa organizacion. Para cuentas externas: make gcp-iap-oauth."
fi

# ------------------------------------------------------------------------------
paso "Cloud Scheduler: scoring el dia 5 de cada mes"
# El job churn-score se crea en la primera promocion; el scheduler puede existir antes.
if ! gcloud scheduler jobs describe churn-score-mensual --location="$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs create http churn-score-mensual \
    --location="$REGION" \
    --schedule="0 6 5 * *" \
    --time-zone="America/Argentina/Buenos_Aires" \
    --uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/churn-score:run" \
    --http-method=POST \
    --oauth-service-account-email="$(sa_email scheduler)"
fi

# ------------------------------------------------------------------------------
if [[ -n "${BILLING_ACCOUNT:-}" ]]; then
  paso "Alerta de presupuesto (USD ${BUDGET_USD})"
  gcloud services enable billingbudgets.googleapis.com
  if ! gcloud billing budgets list --billing-account="$BILLING_ACCOUNT" \
    --format='value(displayName)' | grep -qx churn-fudo; then
    gcloud billing budgets create \
      --billing-account="$BILLING_ACCOUNT" \
      --display-name=churn-fudo \
      --budget-amount="${BUDGET_USD}USD" \
      --filter-projects="projects/${PROJECT_ID}" \
      --threshold-rule=percent=0.5 \
      --threshold-rule=percent=0.9 \
      --threshold-rule=percent=1.0
  fi
fi

# ------------------------------------------------------------------------------
paso "Listo. Siguientes pasos"
cat <<EOF
1. Cargar estas variables en GitHub (Settings > Secrets and variables > Actions >
   Variables):

   GCP_PROJECT_ID       ${PROJECT_ID}
   GCP_PROJECT_NUMBER   ${PROJECT_NUMBER}
   GCP_REGION           ${REGION}
   GCP_BUCKET           ${BUCKET}
   GCP_WIF_PROVIDER     projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/github
   GCP_DEPLOYER_SA      $(sa_email deployer)

2. Commit y push a main: despliega todo.

3. Si tienen que entrar cuentas de fuera de la organizacion, cliente OAuth propio:

   IAP_OAUTH_CLIENT_ID=... IAP_OAUTH_CLIENT_SECRET=... make gcp-iap-oauth GCP_PROJECT=${PROJECT_ID}

4. Dar acceso a la app (nadie entra hasta este paso):

   make gcp-grant GCP_PROJECT=${PROJECT_ID} MEMBER=user:tu-email@dominio
EOF
