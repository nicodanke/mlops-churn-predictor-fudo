# ============================================================================
#  Predictor de churn Fudo
#
#  Todo corre igual en local (Poetry) o en Docker. Los targets `docker-*` usan la
#  misma imagen que despues se despliega en GCP, asi que lo que funciona aca
#  funciona alla.
#
#      make help              lista los comandos disponibles
#      make setup             instala dependencias con Poetry
#      make all               pipeline completo: features -> modelo -> predicciones
#      make up                levanta API + dashboard en Docker
# ============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

POETRY      ?= poetry
RUN         := $(POETRY) run
COMPOSE     ?= docker compose
DC_RUN      := $(COMPOSE) --profile jobs run --rm pipeline

# Configuracion de despliegue en GCP. Sobreescribir por linea de comando o en .env.
# us-central1 por costo: es tier 1 de Cloud Run y entra en el free tier de Cloud Storage.
# El proyecto sale del .env si esta, y si no del que tenga configurado gcloud, que es el
# caso de Cloud Shell (`gcloud config set project`). El placeholder del final hace que los
# targets corten con un mensaje claro en vez de fallar contra un proyecto inexistente.
# `$(or ...)` corta en el primero que no este vacio, asi que el `gcloud` solo se ejecuta
# cuando hace falta.
GCP_PROJECT ?= $(or $(shell sed -n 's/^GCP_PROJECT=//p' .env 2>/dev/null | head -1),$(shell gcloud config get-value project 2>/dev/null | grep -v '(unset)'),tu-proyecto-gcp)
GCP_REGION  ?= $(or $(shell sed -n 's/^GCP_REGION=//p' .env 2>/dev/null | head -1),us-central1)
GCP_REPO    ?= churn
# Sin "-fudo" choca con el bucket que crea el lab de la clase 4.
GCP_BUCKET  ?= $(GCP_PROJECT)-churn-fudo
GITHUB_REPO ?= nicodanke/mlops-churn-predictor-fudo
IMAGE_BASE  := $(GCP_REGION)-docker.pkg.dev/$(GCP_PROJECT)/$(GCP_REPO)
# Las imagenes se etiquetan con el SHA del commit que las construyo.
TAG         ?= $(shell git rev-parse HEAD 2>/dev/null)

.PHONY: help
help: ## Muestra esta ayuda
	@echo ""
	@echo "  Predictor de churn Fudo"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'
	@echo ""

# ---------------------------------------------------------------- entorno --

.PHONY: setup
setup: ## Instala las dependencias del proyecto con Poetry
	$(POETRY) install --all-extras

.PHONY: lock
lock: ## Regenera poetry.lock
	$(POETRY) lock

# ---------------------------------------------------------------- pipeline --

.PHONY: prepare
prepare: ## Lee el snapshot, infiere el churn y construye las features
	$(RUN) churn prepare

.PHONY: prepare-force
prepare-force: ## Recalcula las features ignorando el cache
	$(RUN) churn prepare --force

.PHONY: train
train: ## Entrena el modelo y reporta las metricas offline
	$(RUN) churn train

.PHONY: score
score: ## Genera el batch de predicciones del ultimo periodo
	$(RUN) churn score

.PHONY: score-demo
score-demo: ## Igual que score, pero con la lista de precios de ejemplo
	$(RUN) churn score --pricing config/pricing.example.yaml

.PHONY: score-llm
score-llm: ## Scoring con diagnosticos redactados por Claude (requiere ANTHROPIC_API_KEY)
	$(RUN) churn score --narrative llm

.PHONY: all
all: ## Pipeline completo: prepare -> train -> score
	$(RUN) churn run-all

.PHONY: eda
eda: ## Estadistica de la base: salud, adopcion y señales de churn -> outputs/eda/stats.json
	$(RUN) churn eda

.PHONY: baseline
baseline: ## Churn rate observado por periodo (el numero a batir)
	$(RUN) churn baseline

.PHONY: seasonality
seasonality: ## Cuentas estacionales y ciclo anual de altas y bajas
	$(RUN) churn seasonality

.PHONY: info
info: ## Metadatos y metricas del modelo entrenado
	$(RUN) churn info

.PHONY: drift
drift: ## Compara los datos del ultimo mes con los del entrenamiento (PSI)
	$(RUN) churn drift

# ----------------------------------------------------------------- precios --

.PHONY: pricing-template
pricing-template: ## Genera/actualiza config/pricing.yaml con todos los planes y modulos
	$(RUN) churn pricing-template

.PHONY: pricing-check
pricing-check: ## Muestra que planes todavia no tienen precio cargado
	$(RUN) churn pricing-check

# ------------------------------------------------------------- aplicacion --

.PHONY: api
api: ## Levanta la API en local (sin Docker) en :8000
	cd api && $(RUN) uvicorn app.main:app --reload --port 8000

.PHONY: web
web: ## Sirve el dashboard en local (sin Docker) en :8080
	cd web && $(RUN) python -m http.server 8080

# ----------------------------------------------------------------- docker --

.PHONY: build
build: ## Construye las tres imagenes
	$(COMPOSE) --profile jobs build

# Puertos publicados. Se pueden fijar de tres formas, de mayor a menor prioridad:
#   make up API_PORT=8001     solo para esa corrida
#   API_PORT=8001 en .env     permanente y no se versiona (util si el 8000 ya
#                             lo ocupa otro proyecto de forma estable)
#   el default de aca abajo
API_PORT ?= $(or $(shell sed -n 's/^API_PORT=//p' .env 2>/dev/null | head -1),8000)
WEB_PORT ?= $(or $(shell sed -n 's/^WEB_PORT=//p' .env 2>/dev/null | head -1),8080)
# Memoria del contenedor del pipeline. Con menos de 4g muere por OOM al construir
# las features (exit 137). Ver "Requisitos" en el README.
PIPELINE_MEM ?= 4g
export API_PORT WEB_PORT PIPELINE_MEM

.PHONY: up
up: check-ports ## Levanta API (:8000) y dashboard (:8080). Puertos: API_PORT / WEB_PORT
	$(COMPOSE) up -d api web
	@echo ""
	@echo "  API        http://localhost:$(API_PORT)/docs"
	@echo "  Dashboard  http://localhost:$(WEB_PORT)"
	@echo ""

# Docker falla con un mensaje cripitico cuando el puerto esta tomado ("Bind for
# 0.0.0.0:8000 failed") y deja los contenedores a medio crear. Mejor avisar antes
# y decir exactamente que hacer.
.PHONY: check-ports
check-ports:
	@command -v lsof >/dev/null 2>&1 || exit 0; \
	for p in $(API_PORT) $(WEB_PORT); do \
	  holder=$$(lsof -nP -iTCP:$$p -sTCP:LISTEN 2>/dev/null | tail -n +2 | head -1); \
	  if [ -n "$$holder" ] && ! echo "$$holder" | grep -q churnpredictor; then \
	    echo ""; \
	    echo "  El puerto $$p ya esta en uso por:"; \
	    echo "$$holder" | awk '{print "    " $$1 " (pid " $$2 ")"}'; \
	    docker ps --format '    contenedor {{.Names}} -> {{.Ports}}' 2>/dev/null \
	      | grep ":$$p->" || true; \
	    echo ""; \
	    echo "  Para esta corrida:"; \
	    echo "      make up API_PORT=8001 WEB_PORT=8081"; \
	    echo ""; \
	    echo "  Para no tener que repetirlo (el .env no se versiona):"; \
	    echo "      printf 'API_PORT=8001\\nWEB_PORT=8081\\n' >> .env"; \
	    echo ""; \
	    exit 1; \
	  fi; \
	done

.PHONY: down
down: ## Baja el stack
	$(COMPOSE) down

.PHONY: logs
logs: ## Sigue los logs de API y dashboard
	$(COMPOSE) logs -f api web

.PHONY: docker-prepare
docker-prepare: ## prepare dentro del contenedor del pipeline
	$(DC_RUN) prepare

.PHONY: docker-train
docker-train: ## train dentro del contenedor del pipeline
	$(DC_RUN) train

.PHONY: docker-score
docker-score: ## score dentro del contenedor del pipeline
	$(DC_RUN) score

.PHONY: docker-eda
docker-eda: ## Estadistica de la base dentro del contenedor del pipeline
	$(DC_RUN) churn eda

.PHONY: docker-drift
docker-drift: ## Reporte de drift dentro del contenedor del pipeline
	$(DC_RUN) drift

.PHONY: docker-seasonality
docker-seasonality: ## Reporte de estacionalidad dentro del contenedor
	$(DC_RUN) seasonality

.PHONY: docker-all
docker-all: ## Pipeline completo dentro del contenedor
	$(DC_RUN) run-all

.PHONY: docker-shell
docker-shell: ## Shell dentro del contenedor del pipeline
	$(COMPOSE) --profile jobs run --rm --entrypoint bash pipeline

# -------------------------------------------------------------- calidad ---

.PHONY: test
test: ## Corre los tests
	$(RUN) pytest -q

.PHONY: lint
lint: ## Chequea estilo con ruff
	$(RUN) ruff check src api tests

.PHONY: fmt
fmt: ## Formatea y arregla lo que ruff pueda arreglar solo
	$(RUN) ruff check --fix src api tests
	$(RUN) ruff format src api tests

.PHONY: clean
clean: ## Borra caches y artefactos intermedios (no toca data/ ni models/)
	rm -rf outputs/interim .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

# --------------------------------------------------- GCP con Docker local ---
# El mismo camino que `gcp-build` + `gcp-deploy-app`, pero construyendo con Docker
# en esta maquina en vez de con Cloud Build en la nube. Es el recorrido de la clase 6
# —construir, publicar, desplegar— con un comando por paso.
#
#   make gcp-docker-auth      una vez por maquina: habilita el push al registry
#   make gcp-docker-build     construye las tres imagenes, etiquetadas con el SHA
#   make gcp-docker-push      las publica en Artifact Registry
#   make gcp-docker-deploy    despliega la API con esa imagen
#   make gcp-docker-release   los tres pasos anteriores, en orden
#
# Se etiqueta con el SHA del commit y no con :latest, igual que Cloud Build: dos
# imagenes distintas no pueden quedar bajo el mismo nombre.

# Sin GCP_PROJECT la variable cae al placeholder `tu-proyecto-gcp`, y el build igual
# funciona: deja tres imagenes con un nombre que no existe en ningun registry y recien
# falla el push, con un NOT_FOUND que se lee como un problema de permisos. Cortamos
# antes y decimos que arreglar.
.PHONY: require-gcp-project
require-gcp-project:
	@test "$(GCP_PROJECT)" != "tu-proyecto-gcp" || { \
	  echo "GCP_PROJECT no esta configurado (se usaria el placeholder 'tu-proyecto-gcp')."; \
	  echo "  con gcloud:  gcloud config set project mi-proyecto"; \
	  echo "  en .env:     GCP_PROJECT=mi-proyecto"; \
	  echo "  o al vuelo:  make <target> GCP_PROJECT=mi-proyecto"; \
	  exit 1; }

.PHONY: gcp-docker-auth
gcp-docker-auth: ## [Docker] Autoriza a Docker a publicar en Artifact Registry (una vez por maquina)
	gcloud auth configure-docker $(GCP_REGION)-docker.pkg.dev

.PHONY: gcp-docker-build
gcp-docker-build: require-gcp-project ## [Docker] Construye las tres imagenes localmente, etiquetadas con el SHA
	@test -n "$(TAG)" || { echo "Sin commits todavia: no hay SHA para etiquetar."; exit 1; }
	docker build -f docker/Dockerfile.pipeline -t $(IMAGE_BASE)/pipeline:$(TAG) .
	docker build -f docker/Dockerfile.api      -t $(IMAGE_BASE)/api:$(TAG)      .
	docker build -f docker/Dockerfile.web      -t $(IMAGE_BASE)/web:$(TAG)      .

.PHONY: gcp-docker-push
gcp-docker-push: require-gcp-project ## [Docker] Publica las tres imagenes en Artifact Registry
	@# El registry lo crea `make gcp-bootstrap`. Sin el, el push falla con NOT_FOUND.
	docker push $(IMAGE_BASE)/pipeline:$(TAG)
	docker push $(IMAGE_BASE)/api:$(TAG)
	docker push $(IMAGE_BASE)/web:$(TAG)

.PHONY: gcp-docker-deploy
gcp-docker-deploy: require-gcp-project ## [Docker] Despliega la API con la imagen TAG (mismo camino que CI)
	@# Llama al mismo cloudrun.sh que usa el workflow: memoria, cuenta de servicio,
	@# volumen del bucket e IAP quedan definidos en un solo lugar.
	$(CLOUDRUN) app $(IMAGE_BASE)/api:$(TAG)

.PHONY: gcp-docker-release
gcp-docker-release: gcp-docker-build gcp-docker-push gcp-docker-deploy ## [Docker] build + push + deploy, en orden
	@echo ""
	@echo "  Desplegado $(IMAGE_BASE)/api:$(TAG)"
	@echo "  URL:  make gcp-url    (pide iniciar sesion: la app esta detras de IAP)"
	@echo ""

# -------------------------------------------- Cloud Run a mano (clase 6) ---
# El recorrido de la clase 6 con un comando por paso: construir la imagen, publicarla en
# el registry y correrla en Cloud Run. A diferencia de `gcp-docker-*`, no necesita
# `make gcp-bootstrap` ni GitHub Actions.
#
#   make gcp-run-repo      crea el repositorio de imagenes (una vez por proyecto)
#   make gcp-run-build     construye la imagen de la API, que sirve tambien el dashboard
#   make gcp-run-local     la corre en esta maquina, para probarla antes de subirla
#   make gcp-run-push      la publica en Artifact Registry
#   make gcp-run-data      sube las predicciones al bucket, que el servicio lee montado
#   make gcp-run-sa        crea la cuenta de servicio del servicio (una vez por proyecto)
#   make gcp-run-deploy    despliega esa imagen en Cloud Run
#   make gcp-run-release   build + push + data + deploy, en orden
#   make gcp-run-url       imprime la URL
#   make gcp-run-logs      ultimos logs del servicio
#   make gcp-run-delete    da de baja el servicio
#
# OJO: el servicio queda PUBLICO, sin login, como en la clase. Es el mismo servicio que
# publica `make gcp-cloudshell-publish` de una sola vez. Para que pida iniciar sesion,
# el camino es `make gcp-deploy-app` (IAP) y `make gcp-grant`.

GCP_RUN_SERVICE ?= churn-demo
GCP_RUN_SA      := $(GCP_RUN_SERVICE)@$(GCP_PROJECT).iam.gserviceaccount.com
GCP_RUN_IMAGE   := $(IMAGE_BASE)/api:$(TAG)
# Carpeta del bucket que lee el servicio, montada en /mnt/gcs.
GCP_RUN_PREFIX  ?= demo
# Lo que dejan `make gcp-cloudshell-score` y `make gcp-cloudshell-train` en esta maquina.
GCP_RUN_DATA    ?= outputs/cloudshell

.PHONY: gcp-run-repo
gcp-run-repo: require-gcp-project ## [Clase 6] Habilita las APIs y crea el repositorio de imagenes (una vez)
	@# Un proyecto nuevo nace con casi todo apagado. Habilitar una API es gratis.
	gcloud services enable run.googleapis.com artifactregistry.googleapis.com --project=$(GCP_PROJECT)
	gcloud artifacts repositories describe $(GCP_REPO) --project=$(GCP_PROJECT) --location=$(GCP_REGION) >/dev/null 2>&1 \
	  || gcloud artifacts repositories create $(GCP_REPO) --project=$(GCP_PROJECT) --location=$(GCP_REGION) \
	       --repository-format=docker --description="Imagenes del predictor de churn"

.PHONY: gcp-run-build
gcp-run-build: require-gcp-project ## [Clase 6] Construye la imagen de la API + dashboard
	@test -n "$(TAG)" || { echo "Sin commits todavia: no hay SHA para etiquetar. Usar TAG=latest."; exit 1; }
	@# --platform linux/amd64: Cloud Run no corre imagenes arm64, y en una Mac con chip M
	@# el build por defecto sale arm64. El servicio arrancaria roto, no al construirse.
	docker build --platform linux/amd64 -f docker/Dockerfile.api -t $(GCP_RUN_IMAGE) .

.PHONY: gcp-run-local
gcp-run-local: ## [Clase 6] Corre la imagen en esta maquina en :$(API_PORT), antes de subirla
	@test -d "$(GCP_RUN_DATA)/predictions" || { echo "No hay predicciones en $(GCP_RUN_DATA)/predictions. Generarlas con: make gcp-cloudshell-score"; exit 1; }
	@# El reporte de EDA puede no existir todavia. Se crea vacio en vez de omitir el
	@# volumen: docker crearia el directorio igual, pero como root y fuera del .gitignore.
	@mkdir -p "$(GCP_RUN_DATA)/eda"
	docker run --rm -p $(API_PORT):8000 \
	  -v "$(PWD)/$(GCP_RUN_DATA)/predictions:/app/predictions:ro" \
	  -v "$(PWD)/$(GCP_RUN_DATA)/model:/app/model:ro" \
	  -v "$(PWD)/$(GCP_RUN_DATA)/eda:/app/eda:ro" \
	  -e CHURN_API_PREDICTIONS_DIR=/app/predictions \
	  -e CHURN_API_MODEL_DIR=/app/model \
	  -e CHURN_API_EDA_DIR=/app/eda \
	  $(GCP_RUN_IMAGE)

.PHONY: gcp-run-push
gcp-run-push: require-gcp-project ## [Clase 6] Publica la imagen en Artifact Registry
	@# Si falla con "denied" o NOT_FOUND: falta `make gcp-docker-auth` o `make gcp-run-repo`.
	docker push $(GCP_RUN_IMAGE)

.PHONY: gcp-run-data
gcp-run-data: require-gcp-project ## [Clase 6] Sube al bucket las predicciones que sirve el servicio
	@test -d "$(GCP_RUN_DATA)/predictions" || { echo "No hay predicciones en $(GCP_RUN_DATA)/predictions. Generarlas con: make gcp-cloudshell-score"; exit 1; }
	gcloud storage rsync --recursive --delete-unmatched-destination-objects \
	  "$(GCP_RUN_DATA)/predictions/" gs://$(GCP_BUCKET)/$(GCP_RUN_PREFIX)/predictions/
	gcloud storage rsync --delete-unmatched-destination-objects \
	  "$(GCP_RUN_DATA)/model/" gs://$(GCP_BUCKET)/$(GCP_RUN_PREFIX)/model/
	@# El reporte de EDA es opcional: si todavia no se genero, el dashboard muestra la
	@# pestaña de riesgo igual y la de uso avisa que falta correr `churn eda`.
	@test -d "$(GCP_RUN_DATA)/eda" && gcloud storage rsync --delete-unmatched-destination-objects \
	  "$(GCP_RUN_DATA)/eda/" gs://$(GCP_BUCKET)/$(GCP_RUN_PREFIX)/eda/ \
	  || echo "  (sin outputs/cloudshell/eda: la pestaña 'Uso de la base' queda vacia)"

.PHONY: gcp-run-sa
gcp-run-sa: require-gcp-project ## [Clase 6] Cuenta de servicio con solo lectura del bucket (una vez)
	@# Sin esto el servicio usaria la cuenta por defecto de Compute, que en muchos
	@# proyectos es Editor de todo. Esta solo puede leer el bucket.
	gcloud iam service-accounts describe $(GCP_RUN_SA) --project=$(GCP_PROJECT) >/dev/null 2>&1 \
	  || { gcloud iam service-accounts create $(GCP_RUN_SERVICE) --project=$(GCP_PROJECT) \
	         --display-name="Churn: dashboard publico"; sleep 15; }
	gcloud storage buckets add-iam-policy-binding gs://$(GCP_BUCKET) \
	  --member=serviceAccount:$(GCP_RUN_SA) --role=roles/storage.objectViewer >/dev/null

.PHONY: gcp-run-deploy
gcp-run-deploy: require-gcp-project ## [Clase 6] Despliega la imagen en Cloud Run con URL PUBLICA (sin login)
	@# El bucket se monta de solo lectura en /mnt/gcs, asi que actualizar las predicciones
	@# no obliga a reconstruir la imagen. gen2 es el entorno que permite montarlo.
	gcloud run deploy $(GCP_RUN_SERVICE) \
	  --project=$(GCP_PROJECT) --region=$(GCP_REGION) \
	  --image=$(GCP_RUN_IMAGE) \
	  --service-account=$(GCP_RUN_SA) \
	  --execution-environment=gen2 \
	  --cpu=1 --memory=1Gi --min-instances=0 --max-instances=2 \
	  --allow-unauthenticated \
	  --set-env-vars=CHURN_API_PREDICTIONS_DIR=/mnt/gcs/$(GCP_RUN_PREFIX)/predictions,CHURN_API_MODEL_DIR=/mnt/gcs/$(GCP_RUN_PREFIX)/model,CHURN_API_EDA_DIR=/mnt/gcs/$(GCP_RUN_PREFIX)/eda,CHURN_API_CORS_ORIGINS= \
	  --clear-volumes \
	  --add-volume=name=gcs,type=cloud-storage,bucket=$(GCP_BUCKET),readonly=true,mount-options=implicit-dirs \
	  --clear-volume-mounts \
	  --add-volume-mount=volume=gcs,mount-path=/mnt/gcs
	@echo ""
	@echo "  URL: $$(gcloud run services describe $(GCP_RUN_SERVICE) --project=$(GCP_PROJECT) --region=$(GCP_REGION) --format='value(status.url)')"
	@echo "  Sin login: cualquiera con ese link ve datos de cuentas reales."
	@echo "  Darlo de baja: make gcp-run-delete"
	@echo ""

.PHONY: gcp-run-release
gcp-run-release: gcp-run-build gcp-run-push gcp-run-data gcp-run-deploy ## [Clase 6] build + push + data + deploy, en orden

.PHONY: gcp-run-url
gcp-run-url: ## [Clase 6] URL del servicio
	@gcloud run services describe $(GCP_RUN_SERVICE) --project=$(GCP_PROJECT) --region=$(GCP_REGION) --format='value(status.url)'

.PHONY: gcp-run-logs
gcp-run-logs: ## [Clase 6] Ultimos logs del servicio
	gcloud run services logs read $(GCP_RUN_SERVICE) --project=$(GCP_PROJECT) --region=$(GCP_REGION) --limit=50

.PHONY: gcp-run-revisions
gcp-run-revisions: ## [Clase 7] Lista las revisiones del servicio (cada deploy crea una)
	gcloud run revisions list --service=$(GCP_RUN_SERVICE) --project=$(GCP_PROJECT) --region=$(GCP_REGION)

.PHONY: gcp-run-rollback
gcp-run-rollback: ## [Clase 7] Manda el 100% del trafico a una revision anterior. REVISION=churn-demo-00002-abc
	@# Rollback sin reconstruir ni volver a desplegar: solo se mueve el trafico. El nombre
	@# de la revision sale de `make gcp-run-revisions`.
	@test -n "$(REVISION)" || { echo "Uso: make gcp-run-rollback REVISION=<nombre>   (ver: make gcp-run-revisions)"; exit 1; }
	gcloud run services update-traffic $(GCP_RUN_SERVICE) --project=$(GCP_PROJECT) --region=$(GCP_REGION) \
	  --to-revisions $(REVISION)=100

.PHONY: gcp-run-logs-api
gcp-run-logs-api: ## [Clase 7] Requests del servicio en Cloud Logging, con latencia y estado
	@# La API emite una linea JSON por request, asi que Cloud Logging la parsea a campos y
	@# se puede pedir la tabla por columna en vez de leer texto.
	gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="$(GCP_RUN_SERVICE)" AND jsonPayload.event="request"' \
	  --project=$(GCP_PROJECT) --limit=20 \
	  --format='table(timestamp.date("%H:%M:%S"), jsonPayload.method, jsonPayload.path, jsonPayload.status, jsonPayload.latency_ms, jsonPayload.revision)'

.PHONY: gcp-run-logs-errors
gcp-run-logs-errors: ## [Clase 7] Solo los errores del servicio en Cloud Logging
	gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="$(GCP_RUN_SERVICE)" AND severity>=ERROR' \
	  --project=$(GCP_PROJECT) --limit=20 \
	  --format='table(timestamp.date("%H:%M:%S"), severity, jsonPayload.path, jsonPayload.status, jsonPayload.message)'

.PHONY: gcp-run-delete
gcp-run-delete: ## [Clase 6] Da de baja el servicio
	gcloud run services delete $(GCP_RUN_SERVICE) --project=$(GCP_PROJECT) --region=$(GCP_REGION)
	@echo "Las predicciones siguen en gs://$(GCP_BUCKET)/$(GCP_RUN_PREFIX)/"

# ------------------------------------------------------------------- GCP ---
# El despliegue normal es automatico (.github/workflows/deploy.yml). Estos targets son
# para la puesta a punto inicial y para operar a mano, y llaman al mismo
# deploy/cloudrun.sh que el workflow: hacen exactamente lo mismo que CI.

GCP_ENV = PROJECT_ID=$(GCP_PROJECT) REGION=$(GCP_REGION) BUCKET=$(GCP_BUCKET) PROJECT_NUMBER=$$(gcloud projects describe $(GCP_PROJECT) --format='value(projectNumber)')
CLOUDRUN = $(GCP_ENV) bash deploy/cloudrun.sh

# Frenar y limpiar. Ver deploy/teardown.sh y el runbook en deploy/runbook.md.
TEARDOWN = PROJECT_ID=$(GCP_PROJECT) BUCKET=$(GCP_BUCKET) REGION=$(GCP_REGION) bash deploy/teardown.sh

.PHONY: gcp-resources
gcp-resources: ## Lista que hay creado en GCP y que puede estar costando
	@$(TEARDOWN) listar

.PHONY: gcp-stop
gcp-stop: ## Da de baja el dashboard publico. Los datos y los modelos quedan
	$(TEARDOWN) frenar

.PHONY: gcp-teardown
gcp-teardown: ## Borra servicio, imagenes y cuenta de servicio. TODO=1 borra tambien el bucket
	$(TEARDOWN) $(if $(TODO),borrar-todo,borrar)

.PHONY: gcp-bootstrap
gcp-bootstrap: ## Puesta a punto inicial de GCP (una vez): bucket, registry, permisos, WIF
	PROJECT_ID=$(GCP_PROJECT) REGION=$(GCP_REGION) BUCKET=$(GCP_BUCKET) \
	GITHUB_REPO=$(GITHUB_REPO) bash deploy/bootstrap.sh

.PHONY: gcp-upload-data
gcp-upload-data: ## Sube un snapshot comprimido al bucket. FILE=data/account-stats-AAAAMM.csv
	@test -n "$(FILE)" || { echo "Uso: make gcp-upload-data FILE=data/account-stats-202609.csv"; exit 1; }
	@# Sin este chequeo, un FILE inexistente subia un objeto vacio al bucket.
	@test -s "$(FILE)" || { echo "No existe o esta vacio: $(FILE)"; exit 1; }
	set -o pipefail; gzip -c "$(FILE)" | gcloud storage cp - gs://$(GCP_BUCKET)/raw/$$(basename "$(FILE)").gz

# Entrenamiento desde Cloud Shell, como en el lab de la clase 4 (ver deploy/cloudshell.sh).
# Se corren dentro de Cloud Shell y toman el proyecto de `gcloud config`.
CLOUDSHELL = bash deploy/cloudshell.sh

.PHONY: gcp-cloudshell-setup
gcp-cloudshell-setup: ## [Cloud Shell] Instala las dependencias y crea el bucket
	$(CLOUDSHELL) setup

.PHONY: gcp-cloudshell-upload
gcp-cloudshell-upload: ## [Cloud Shell] Sube snapshots al bucket. FILE="data/account-stats-*.csv"
	@test -n "$(FILE)" || { echo 'Uso: make gcp-cloudshell-upload FILE="data/account-stats-*.csv"'; exit 1; }
	@# Sin comillas a proposito: asi FILE puede traer un patron y subir los 20 meses de
	@# una. Subirlos de a uno es donde se saltea alguno, y un mes faltante no da error.
	$(CLOUDSHELL) upload $(FILE)

.PHONY: gcp-cloudshell-train
gcp-cloudshell-train: ## [Cloud Shell] Entrena y guarda el modelo en gs://BUCKET/models/cloudshell/. RUN_ID opcional
	$(CLOUDSHELL) train $(RUN_ID)

.PHONY: gcp-cloudshell-score
gcp-cloudshell-score: ## [Cloud Shell] Predicciones del ultimo mes con un modelo del bucket. RUN_ID opcional (por defecto el ultimo)
	$(CLOUDSHELL) score $(RUN_ID)

.PHONY: gcp-cloudshell-drift
gcp-cloudshell-drift: ## [Cloud Shell] Drift del ultimo mes contra los datos de entrenamiento. RUN_ID opcional
	$(CLOUDSHELL) drift $(RUN_ID)

.PHONY: gcp-cloudshell-serve
gcp-cloudshell-serve: ## [Cloud Shell] API + dashboard para abrir con la Vista previa en la Web. PORT=8080
	$(CLOUDSHELL) serve $(PORT)

.PHONY: gcp-cloudshell-publish
gcp-cloudshell-publish: ## [Cloud Shell] Publica API + dashboard en Cloud Run con una URL PUBLICA (sin login)
	$(CLOUDSHELL) publish

.PHONY: gcp-cloudshell-url
gcp-cloudshell-url: ## [Cloud Shell] URL del dashboard publicado
	@$(CLOUDSHELL) url

.PHONY: gcp-cloudshell-unpublish
gcp-cloudshell-unpublish: ## [Cloud Shell] Da de baja el dashboard publicado
	$(CLOUDSHELL) unpublish

.PHONY: gcp-cloudshell-models
gcp-cloudshell-models: ## [Cloud Shell] Lista los modelos entrenados desde Cloud Shell
	$(CLOUDSHELL) models

.PHONY: gcp-build
gcp-build: ## Construye las imagenes con Cloud Build (alternativa manual a GitHub Actions)
	gcloud builds submit --project=$(GCP_PROJECT) --config=deploy/cloudbuild.yaml \
		--substitutions=_REGION=$(GCP_REGION),_REPO=$(GCP_REPO),_TAG=$(TAG) .

.PHONY: gcp-retrain
gcp-retrain: ## Entrena un candidato con la imagen TAG y lo compara contra el campeon
	$(CLOUDRUN) train-job $(IMAGE_BASE)/pipeline:$(TAG)
	$(CLOUDRUN) retrain $(TAG)
	$(CLOUDRUN) decision $(TAG) md

.PHONY: gcp-release
gcp-release: ## Promueve la version TAG y regenera predicciones. FORCE=1 para rollback
	$(CLOUDRUN) release $(TAG) $(if $(FORCE),--force)

.PHONY: gcp-score
gcp-score: ## Corre el scoring con el campeon actual, a demanda
	$(CLOUDRUN) score

.PHONY: gcp-deploy-app
gcp-deploy-app: ## Despliega API + dashboard detras de IAP con la imagen TAG
	$(CLOUDRUN) app $(IMAGE_BASE)/api:$(TAG)

.PHONY: gcp-iap-oauth
gcp-iap-oauth: ## Cliente OAuth propio para IAP (cuentas de fuera de la organizacion)
	@$(CLOUDRUN) iap-oauth

.PHONY: gcp-grant
gcp-grant: ## Da acceso a la app. MEMBER=user:ana@fu.do | group:cx@fu.do | domain:fu.do
	@test -n "$(MEMBER)" || { echo "Uso: make gcp-grant MEMBER=user:ana@fu.do"; exit 1; }
	@$(CLOUDRUN) grant "$(MEMBER)"

.PHONY: gcp-revoke
gcp-revoke: ## Quita el acceso a la app. MEMBER=user:ana@fu.do
	@test -n "$(MEMBER)" || { echo "Uso: make gcp-revoke MEMBER=user:ana@fu.do"; exit 1; }
	@$(CLOUDRUN) revoke "$(MEMBER)"

.PHONY: gcp-access
gcp-access: ## Lista quien tiene acceso a la app
	@$(CLOUDRUN) access

.PHONY: gcp-registry
gcp-registry: ## Historial de versiones promovidas a produccion
	gcloud storage cat gs://$(GCP_BUCKET)/models/registry.json

.PHONY: gcp-url
gcp-url: ## URL de la app (pide iniciar sesion con Google)
	@$(CLOUDRUN) url
