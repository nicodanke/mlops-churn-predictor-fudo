#!/usr/bin/env bash
# ============================================================================
#  Entrenamiento, scoring y dashboard desde Cloud Shell, como en las clases 4 y 5.
#
#  Cloud Shell ya trae gcloud autenticado, asi que no hacen falta cuentas de servicio ni
#  claves: el pipeline lee y escribe el bucket con las credenciales de la sesion. El
#  modelo queda versionado en el bucket, no en Cloud Shell, que se puede cerrar tranquilo.
#
#    setup             instala Poetry y las dependencias, y crea el bucket si falta
#    upload ARCHIVO    sube un snapshot a gs://BUCKET/raw/ (los .csv, comprimidos)
#    train [RUN_ID]    baja los snapshots del bucket y entrena. El modelo queda en
#                      gs://BUCKET/models/cloudshell/RUN_ID (por defecto, fecha y hora UTC)
#    models            lista los modelos entrenados desde Cloud Shell
#    score [RUN_ID]    predicciones del ultimo mes con ese modelo (por defecto, el ultimo)
#    serve [PUERTO]    API + dashboard en PUERTO (8080), para la Vista previa en la Web
#
#  Variables opcionales: PROJECT_ID (por defecto el de `gcloud config`), BUCKET (por
#  defecto PROJECT_ID-churn-fudo) y REGION.
# ============================================================================
set -euo pipefail

PROJECT_ID=${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}
if [[ -z "$PROJECT_ID" || "$PROJECT_ID" == "(unset)" ]]; then
  echo "No hay proyecto configurado. Fijarlo con: gcloud config set project TU_PROYECTO" >&2
  exit 1
fi
BUCKET=${BUCKET:-${PROJECT_ID}-churn-fudo}
REGION=${REGION:-us-central1}
MODELS="gs://${BUCKET}/models/cloudshell"
# Copia local exacta de gs://BUCKET/raw/. Es la que lee config/cloudshell.yaml, en un
# directorio aparte para no mezclarse con los CSV que haya en data/.
SNAPSHOTS=data/bucket
# Lo que genera `score` y lee `serve`. Coincide con scoring.output_dir de
# config/cloudshell.yaml.
WORKDIR=outputs/cloudshell
PREDICTIONS=$WORKDIR/predictions
MODEL_LOCAL=$WORKDIR/model

POETRY_VERSION=2.4.1
# Poetry vive en su propio entorno: el pip del sistema no deja instalar paquetes sueltos
# (PEP 668).
POETRY_HOME=${POETRY_HOME:-$HOME/.local/share/churn-poetry}
POETRY="$POETRY_HOME/bin/poetry"
# El entorno del proyecto queda en .venv, dentro del repo, sin escribir poetry.toml.
export POETRY_VIRTUALENVS_IN_PROJECT=true

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

paso() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

en_cloud_shell() { [[ "${CLOUD_SHELL:-}" == true ]]; }

# RAM total en GB. /proc/meminfo en Linux (Cloud Shell), sysctl en macOS; vacio si no se
# puede saber, y entonces no se avisa nada.
memoria_gb() {
  if [[ -r /proc/meminfo ]]; then
    awk '/MemTotal/ {printf "%.1f", $2 / 1024 / 1024}' /proc/meminfo
  else
    sysctl -n hw.memsize 2>/dev/null | awk '{printf "%.1f", $1 / 1024 / 1024 / 1024}' || true
  fi
}

# Dependencias instaladas y credenciales para que Python lea y escriba el bucket.
requiere_entorno() {
  if [[ ! -x .venv/bin/churn ]]; then
    echo "Faltan las dependencias. Correr primero: make gcp-cloudshell-setup" >&2
    exit 1
  fi
  # gcloud tiene su propio login, pero el pipeline usa las librerias de Python, que leen
  # las Application Default Credentials. Cloud Shell ya las trae; en una computadora hay
  # que crearlas. Sin este chequeo el error aparece recien al tocar el bucket, despues de
  # varios minutos de proceso.
  if ! .venv/bin/python -c 'import google.auth; google.auth.default()' >/dev/null 2>&1; then
    echo "Faltan las credenciales de Python para usar el bucket. Crearlas con:" >&2
    echo "    gcloud auth application-default login" >&2
    exit 1
  fi
}

# Deja en data/bucket/ un espejo de gs://BUCKET/raw/, validando antes que no haya
# snapshots vacios de una subida que fallo.
bajar_snapshots() {
  local listado
  listado=$(gcloud storage ls -l "gs://${BUCKET}/raw/" 2>/dev/null || true)
  if ! grep -q 'gs://' <<<"$listado"; then
    echo "No hay snapshots en gs://${BUCKET}/raw/. Subirlos con:" >&2
    echo "    make gcp-cloudshell-upload FILE=data/account_stats_since_2024.csv" >&2
    exit 1
  fi
  # Columnas de `ls -l`: tamaño, fecha, URL.
  local vacios
  vacios=$(awk '$1 == 0 && $3 ~ /^gs:/ {print "    " $3}' <<<"$listado")
  if [[ -n "$vacios" ]]; then
    echo "Hay snapshots vacios en el bucket, de una subida que fallo:" >&2
    echo "$vacios" >&2
    echo "Volver a subirlos con: make gcp-cloudshell-upload FILE=data/account_stats_since_2024.csv" >&2
    exit 1
  fi

  # Baja solo lo que cambio y borra lo que ya no esta en raw/, asi se trabaja exactamente
  # con lo que hay en el bucket.
  paso "Bajando los snapshots de gs://${BUCKET}/raw/"
  mkdir -p "$SNAPSHOTS"
  gcloud storage rsync --delete-unmatched-destination-objects "gs://${BUCKET}/raw/" "$SNAPSHOTS/"
}

cmd_setup() {
  paso "Python"
  if ! python3 -c 'import sys; sys.exit(not (3, 11) <= sys.version_info[:2] < (3, 13))'; then
    echo "El proyecto necesita Python 3.11 o 3.12 y aca hay $(python3 --version)." >&2
    exit 1
  fi
  python3 --version

  paso "Poetry ${POETRY_VERSION}"
  if [[ ! -x "$POETRY" ]]; then
    python3 -m venv "$POETRY_HOME"
    "$POETRY_HOME/bin/pip" install -q "poetry==${POETRY_VERSION}"
  fi

  # Las mismas versiones que la imagen del pipeline (poetry.lock), mas la API para
  # `serve`. Sin cache: el home de Cloud Shell tiene 5 GB y XGBoost y SHAP ya ocupan una
  # buena parte.
  paso "Dependencias"
  "$POETRY" --no-cache install --only main --extras "gcp api" --no-interaction

  paso "Bucket gs://${BUCKET}"
  if gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
    echo "  ya existe"
  else
    gcloud storage buckets create "gs://${BUCKET}" \
      --project="$PROJECT_ID" \
      --location="$REGION" \
      --uniform-bucket-level-access \
      --public-access-prevention
  fi
}

cmd_upload() {
  local archivo=${1:?"Uso: bash deploy/cloudshell.sh upload data/account_stats_since_2024.csv"}

  # `gcloud storage cp` sube lo que le llegue: si el archivo no existe, en el bucket queda
  # un objeto vacio que recien explota al entrenar.
  if [[ ! -s "$archivo" ]]; then
    echo "No existe o esta vacio: $archivo" >&2
    exit 1
  fi

  if [[ "$archivo" != *.csv ]]; then
    gcloud storage cp "$archivo" "gs://${BUCKET}/raw/"
    return
  fi

  # El CSV pesa ~160 MB y comprimido una fraccion; el loader lee .csv.gz directo. Se
  # comprime a un archivo y no por un pipe, para no subir nada si gzip falla a mitad.
  local comprimido
  comprimido=$(mktemp)
  trap "rm -f '$comprimido'" EXIT
  paso "Comprimiendo $archivo"
  gzip -c "$archivo" >"$comprimido"
  gcloud storage cp "$comprimido" "gs://${BUCKET}/raw/$(basename "$archivo").gz"
}

cmd_train() {
  local run_id=${1:-$(date -u +%Y%m%d-%H%M%S)}
  local model_dir="${MODELS}/${run_id}"

  requiere_entorno

  # El feature engineering llega a ~4 GB. Con menos memoria el proceso muere con un
  # "Killed" y nada mas: mejor avisarlo antes de esperar varios minutos.
  local mem_gb
  mem_gb=$(memoria_gb)
  if [[ -n "$mem_gb" ]] && awk -v m="$mem_gb" 'BEGIN {exit !(m < 4)}'; then
    echo "Aviso: esta maquina tiene ${mem_gb} GB de RAM y el pipeline necesita ~4 GB." >&2
    echo "Si termina en 'Killed', entrenar en local con: make train" >&2
  fi

  bajar_snapshots

  paso "Entrenando -> ${model_dir}"
  GOOGLE_CLOUD_PROJECT="$PROJECT_ID" CHURN_CONFIG=config/cloudshell.yaml \
    .venv/bin/churn train --model-dir "$model_dir"

  echo ""
  echo "  Modelo en ${model_dir}"
  echo "  Metricas:  gcloud storage cat ${model_dir}/metadata.json"
  echo "  Scorear:   make gcp-cloudshell-score RUN_ID=${run_id}"
}

cmd_score() {
  local model_dir
  if [[ -n "${1:-}" ]]; then
    model_dir="${MODELS}/$1"
  else
    # Los RUN_ID por defecto son fecha y hora, asi que el ultimo por nombre es el mas nuevo.
    model_dir=$(gcloud storage ls "${MODELS}/" 2>/dev/null | sort | tail -1 | sed 's:/$::')
  fi
  if [[ -z "$model_dir" ]] || ! gcloud storage ls "${model_dir}/metadata.json" >/dev/null 2>&1; then
    echo "No hay un modelo en ${model_dir:-${MODELS}/}. Entrenar con: make gcp-cloudshell-train" >&2
    exit 1
  fi

  requiere_entorno
  bajar_snapshots
  mkdir -p "$WORKDIR"

  # config/pricing.yaml no esta en git: tiene los precios reales. Se usa la del bucket si
  # alguien la subio, si no la local, y como ultimo recurso la de ejemplo.
  paso "Lista de precios"
  local pricing=""
  if gcloud storage cp "gs://${BUCKET}/config/pricing.yaml" "$WORKDIR/pricing.yaml" >/dev/null 2>&1; then
    echo "  gs://${BUCKET}/config/pricing.yaml"
    pricing="$WORKDIR/pricing.yaml"
  elif [[ -f config/pricing.yaml ]]; then
    echo "  config/pricing.yaml (local)"
  else
    echo "  No hay precios cargados: se usa config/pricing.example.yaml y el revenue en"
    echo "  riesgo es ilustrativo. Para usar los reales:"
    echo "      gcloud storage cp config/pricing.yaml gs://${BUCKET}/config/pricing.yaml"
    pricing=config/pricing.example.yaml
  fi

  paso "Scoreando con ${model_dir}"
  GOOGLE_CLOUD_PROJECT="$PROJECT_ID" CHURN_CONFIG=config/cloudshell.yaml \
    .venv/bin/churn score --model-dir "$model_dir" ${pricing:+--pricing "$pricing"}

  # La API lee los metadatos del modelo de disco (/api/v1/model y /health).
  mkdir -p "$MODEL_LOCAL"
  gcloud storage cp "${model_dir}/metadata.json" "$MODEL_LOCAL/metadata.json" >/dev/null 2>&1
  echo "$model_dir" >"$MODEL_LOCAL/origen.txt"

  echo ""
  echo "  Predicciones en ${PREDICTIONS}"
  echo "  Dashboard:     make gcp-cloudshell-serve"
}

cmd_serve() {
  local puerto=${1:-8080}

  if [[ ! -x .venv/bin/uvicorn ]]; then
    echo "Faltan las dependencias de la API. Correr: make gcp-cloudshell-setup" >&2
    exit 1
  fi
  if ! ls "$PREDICTIONS"/*/predictions.parquet >/dev/null 2>&1; then
    echo "No hay predicciones en ${PREDICTIONS}. Generarlas con: make gcp-cloudshell-score" >&2
    exit 1
  fi

  # En Cloud Shell el puerto solo se ve por la Vista previa en la Web, que ya exige la
  # cuenta de Google duena de la sesion. En una computadora se escucha solo en localhost,
  # para no publicar datos de cuentas reales en la red.
  local host=127.0.0.1
  if en_cloud_shell; then
    host=0.0.0.0
  fi

  paso "Dashboard y API en el puerto ${puerto} (Ctrl+C para cortar)"
  if en_cloud_shell; then
    echo "  Abrir con el boton 'Vista previa en la Web', arriba a la derecha."
    if [[ "$puerto" != 8080 ]]; then
      echo "  Elegir 'Cambiar puerto' y poner ${puerto}."
    fi
    echo "  Documentacion de la API: agregar /docs a la URL de la vista previa."
  else
    echo "  Dashboard: http://127.0.0.1:${puerto}"
    echo "  API:       http://127.0.0.1:${puerto}/docs"
  fi
  if [[ -f "$MODEL_LOCAL/origen.txt" ]]; then
    echo "  Modelo:    $(cat "$MODEL_LOCAL/origen.txt")"
  fi

  # Un solo proceso: la API sirve el dashboard en / y sus datos en /api/v1, desde el mismo
  # origen, asi que la vista previa no necesita dos puertos ni CORS.
  cd api
  CHURN_API_PREDICTIONS_DIR="$ROOT/$PREDICTIONS" \
    CHURN_API_MODEL_DIR="$ROOT/$MODEL_LOCAL" \
    CHURN_API_WEB_DIR="$ROOT/web" \
    exec ../.venv/bin/uvicorn app.main:app --host "$host" --port "$puerto"
}

case "${1:-}" in
  setup) cmd_setup ;;
  upload) shift; cmd_upload "$@" ;;
  train) shift; cmd_train "$@" ;;
  models) gcloud storage ls "${MODELS}/" ;;
  score) shift; cmd_score "$@" ;;
  serve) shift; cmd_serve "$@" ;;
  *)
    sed -n '3,19p' "$0" >&2
    exit 1
    ;;
esac
