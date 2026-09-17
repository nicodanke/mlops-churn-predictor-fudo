#!/usr/bin/env bash
# ============================================================================
#  Frenar y limpiar lo que se creo en GCP.
#
#  Regla de oro de la nube: lo que queda prendido, cuesta. Este script muestra que hay
#  creado y lo da de baja en el orden que corresponde.
#
#    listar        que existe hoy y que puede estar costando. No borra nada
#    frenar        da de baja el dashboard publico (churn-demo). El resto queda
#    borrar        lo anterior + imagenes, repositorio y cuenta de servicio
#    borrar-todo   lo anterior + el bucket, con los datos y los modelos entrenados
#
#  Solo toca lo que crean el camino de Cloud Shell y el de la clase 6. Lo que crea
#  deploy/bootstrap.sh (jobs, scheduler, IAP, federacion con GitHub) se lista pero no se
#  borra: ahi vive el sistema de produccion.
#
#  Variables: PROJECT_ID (por defecto el de `gcloud config`), BUCKET, REGION y
#  CONFIRMAR=si para no preguntar.
# ============================================================================
set -euo pipefail

PROJECT_ID=${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}
if [[ -z "$PROJECT_ID" || "$PROJECT_ID" == "(unset)" ]]; then
  echo "No hay proyecto configurado. Fijarlo con: gcloud config set project TU_PROYECTO" >&2
  exit 1
fi
BUCKET=${BUCKET:-${PROJECT_ID}-churn-fudo}
REGION=${REGION:-us-central1}
SERVICIO=${SERVICIO:-churn-demo}
SA="churn-demo@${PROJECT_ID}.iam.gserviceaccount.com"
REPO=${REPO:-churn}

paso() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
gc() { gcloud "$@" --project="$PROJECT_ID"; }

# Pregunta salvo que ya vengan confirmadas. `esperado` es lo que hay que escribir: para lo
# que no tiene vuelta atras se pide el nombre del recurso, no un "si" automatico.
confirmar() {
  local mensaje=$1 esperado=${2:-si}
  if [[ "${CONFIRMAR:-}" == si ]]; then
    return 0
  fi
  local respuesta
  read -r -p "$mensaje [escribir '${esperado}']: " respuesta
  if [[ "$respuesta" != "$esperado" ]]; then
    echo "Cancelado."
    exit 1
  fi
}

existe_servicio() { gc run services describe "$SERVICIO" --region="$REGION" >/dev/null 2>&1; }

# Sin esto, una sesion vencida hace que todo conteste "no existe" y el reporte diga que no
# hay nada creado, que es justo la conclusion opuesta a la correcta.
requiere_auth() {
  local error
  if ! error=$(gcloud storage ls "gs://${BUCKET}" 2>&1 >/dev/null); then
    if grep -qi "auth\|credential\|reautenticacion\|Reauthentication" <<<"$error"; then
      echo "La sesion de gcloud no sirve para consultar el proyecto:" >&2
      echo "$error" | head -3 | sed 's/^/  /' >&2
      echo "  Renovarla con: gcloud auth login" >&2
      exit 1
    fi
  fi
}

cmd_listar() {
  requiere_auth

  paso "Cloud Run"
  if existe_servicio; then
    local url
    url=$(gc run services describe "$SERVICIO" --region="$REGION" --format='value(status.url)')
    echo "  ${SERVICIO}: ${url}"
    echo "  (escala a cero: sin visitas casi no cuesta, pero la URL esta online)"
  else
    echo "  no hay servicio ${SERVICIO}"
  fi
  gc run jobs list --region="$REGION" --format='value(name)' 2>/dev/null |
    sed 's/^/  job (lo crea bootstrap.sh): /' || true

  paso "Artifact Registry"
  # Es lo que mas cuesta si se acumula: cada imagen del pipeline pesa cientos de MB.
  if gc artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1; then
    gc artifacts repositories describe "$REPO" --location="$REGION" \
      --format='value[separator=" | "](name.basename(), sizeBytes.size(), createTime.date("%Y-%m-%d"))' |
      sed 's/^/  /'
  else
    echo "  no hay repositorio ${REPO}"
  fi

  paso "Cloud Storage"
  for bucket in "gs://${BUCKET}" "gs://${PROJECT_ID}_cloudbuild"; do
    if gcloud storage buckets describe "$bucket" >/dev/null 2>&1; then
      echo "  ${bucket}: $(gcloud storage du -s "$bucket" 2>/dev/null | awk '{print $1 " bytes"}')"
      gcloud storage ls "${bucket}/" 2>/dev/null | sed 's/^/      /'
    else
      echo "  ${bucket}: no existe"
    fi
  done

  paso "Cuentas de servicio"
  local cuentas
  cuentas=$(gc iam service-accounts list --filter="email:churn-" --format='value(email)' 2>/dev/null || true)
  if [[ -n "$cuentas" ]]; then
    sed 's/^/  /' <<<"$cuentas"
  else
    echo "  ninguna"
  fi

  echo ""
  echo "  Frenar el dashboard:  make gcp-stop"
  echo "  Borrar lo demas:      make gcp-teardown        (bucket aparte: TODO=1)"
}

cmd_frenar() {
  if ! existe_servicio; then
    echo "No hay servicio ${SERVICIO} en ${REGION}: no hay nada que frenar."
    return 0
  fi
  confirmar "Se da de baja ${SERVICIO} y su URL deja de responder. Seguir?"
  paso "Dando de baja ${SERVICIO}"
  gc run services delete "$SERVICIO" --region="$REGION" --quiet
  echo "  Listo. Los datos, los modelos y la imagen quedan; se puede volver a desplegar con:"
  echo "      make gcp-run-deploy"
}

cmd_borrar() {
  confirmar "Se borran el servicio, las imagenes, el repositorio y la cuenta de servicio. Seguir?"
  CONFIRMAR=si cmd_frenar

  paso "Repositorio de imagenes ${REPO}"
  if gc artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1; then
    gc artifacts repositories delete "$REPO" --location="$REGION" --quiet
  else
    echo "  no existe"
  fi

  paso "Cuenta de servicio ${SA}"
  if gc iam service-accounts describe "$SA" >/dev/null 2>&1; then
    gc iam service-accounts delete "$SA" --quiet
  else
    echo "  no existe"
  fi

  paso "Predicciones publicadas (gs://${BUCKET}/demo/)"
  gcloud storage rm -r "gs://${BUCKET}/demo/" 2>/dev/null || echo "  no habia nada publicado"

  echo ""
  echo "  Quedan el bucket con los datos y los modelos. Para borrarlo tambien:"
  echo "      make gcp-teardown TODO=1"
}

cmd_borrar_todo() {
  echo ""
  echo "Esto borra gs://${BUCKET} entero: el snapshot, todos los modelos entrenados y las"
  echo "predicciones. No se puede deshacer, y volver a tenerlo implica subir el CSV y"
  echo "reentrenar."
  confirmar "Para confirmar, escribir el nombre del bucket" "$BUCKET"
  CONFIRMAR=si cmd_borrar

  paso "Bucket gs://${BUCKET}"
  gcloud storage rm -r "gs://${BUCKET}" 2>/dev/null || echo "  ya no existe"

  # Lo crea `gcloud builds submit` para subir el codigo; sin builds no hace falta.
  paso "Bucket de Cloud Build"
  gcloud storage rm -r "gs://${PROJECT_ID}_cloudbuild" 2>/dev/null || echo "  no existe"

  echo ""
  echo "  Listo. No queda nada de este proyecto salvo lo que haya creado bootstrap.sh."
}

case "${1:-}" in
  listar) cmd_listar ;;
  frenar) cmd_frenar ;;
  borrar) cmd_borrar ;;
  borrar-todo) cmd_borrar_todo ;;
  *)
    sed -n '3,18p' "$0" >&2
    exit 1
    ;;
esac
