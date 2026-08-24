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

# Configuracion de despliegue en GCP. Sobreescribir por entorno o en .env.
GCP_PROJECT ?= tu-proyecto-gcp
GCP_REGION  ?= southamerica-east1
GCP_REPO    ?= churn
IMAGE_BASE  := $(GCP_REGION)-docker.pkg.dev/$(GCP_PROJECT)/$(GCP_REPO)
TAG         ?= latest

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

.PHONY: gcp-build
gcp-build: ## Construye y sube las imagenes a Artifact Registry
	gcloud builds submit --config=deploy/cloudbuild.yaml \
		--substitutions=_REGION=$(GCP_REGION),_REPO=$(GCP_REPO),_TAG=$(TAG) .

.PHONY: gcp-deploy-api
gcp-deploy-api: ## Despliega la API en Cloud Run
	gcloud run deploy churn-api \
		--image=$(IMAGE_BASE)/api:$(TAG) \
		--region=$(GCP_REGION) --platform=managed --allow-unauthenticated \
		--memory=1Gi --cpu=1 --min-instances=0 --max-instances=4

.PHONY: gcp-deploy-web
gcp-deploy-web: ## Despliega el dashboard en Cloud Run
	gcloud run deploy churn-web \
		--image=$(IMAGE_BASE)/web:$(TAG) \
		--region=$(GCP_REGION) --platform=managed --allow-unauthenticated \
		--memory=256Mi --min-instances=0 --max-instances=2

.PHONY: gcp-deploy-job
gcp-deploy-job: ## Crea/actualiza el Cloud Run Job del pipeline batch
	gcloud run jobs deploy churn-pipeline \
		--image=$(IMAGE_BASE)/pipeline:$(TAG) \
		--region=$(GCP_REGION) --memory=4Gi --cpu=2 --task-timeout=3600 \
		--args=run-all

.PHONY: gcp-run-job
gcp-run-job: ## Ejecuta el job batch en GCP a demanda
	gcloud run jobs execute churn-pipeline --region=$(GCP_REGION) --wait
