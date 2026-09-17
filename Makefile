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
GCP_PROJECT ?= $(or $(shell sed -n 's/^GCP_PROJECT=//p' .env 2>/dev/null | head -1),tu-proyecto-gcp)
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

.PHONY: baseline
baseline: ## Churn rate observado por periodo (el numero a batir)
	$(RUN) churn baseline

.PHONY: seasonality
seasonality: ## Cuentas estacionales y ciclo anual de altas y bajas
	$(RUN) churn seasonality

.PHONY: info
info: ## Metadatos y metricas del modelo entrenado
	$(RUN) churn info

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

# ------------------------------------------------------------------- GCP ---
# El despliegue normal es automatico (.github/workflows/deploy.yml). Estos targets son
# para la puesta a punto inicial y para operar a mano, y llaman al mismo
# deploy/cloudrun.sh que el workflow: hacen exactamente lo mismo que CI.

GCP_ENV = PROJECT_ID=$(GCP_PROJECT) REGION=$(GCP_REGION) BUCKET=$(GCP_BUCKET) PROJECT_NUMBER=$$(gcloud projects describe $(GCP_PROJECT) --format='value(projectNumber)')
CLOUDRUN = $(GCP_ENV) bash deploy/cloudrun.sh

.PHONY: gcp-bootstrap
gcp-bootstrap: ## Puesta a punto inicial de GCP (una vez): bucket, registry, permisos, WIF
	PROJECT_ID=$(GCP_PROJECT) REGION=$(GCP_REGION) BUCKET=$(GCP_BUCKET) \
	GITHUB_REPO=$(GITHUB_REPO) bash deploy/bootstrap.sh

.PHONY: gcp-upload-data
gcp-upload-data: ## Sube un snapshot comprimido al bucket. FILE=data/account_stats_AAAAMM.csv
	@test -n "$(FILE)" || { echo "Uso: make gcp-upload-data FILE=data/account_stats_202609.csv"; exit 1; }
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
gcp-cloudshell-upload: ## [Cloud Shell] Sube un snapshot al bucket. FILE=~/account_stats_AAAAMM.csv
	@test -n "$(FILE)" || { echo "Uso: make gcp-cloudshell-upload FILE=~/account_stats_202609.csv"; exit 1; }
	$(CLOUDSHELL) upload "$(FILE)"

.PHONY: gcp-cloudshell-train
gcp-cloudshell-train: ## [Cloud Shell] Entrena y guarda el modelo en gs://BUCKET/models/cloudshell/. RUN_ID opcional
	$(CLOUDSHELL) train $(RUN_ID)

.PHONY: gcp-cloudshell-score
gcp-cloudshell-score: ## [Cloud Shell] Predicciones del ultimo mes con un modelo del bucket. RUN_ID opcional (por defecto el ultimo)
	$(CLOUDSHELL) score $(RUN_ID)

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
