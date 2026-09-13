"""API FastAPI que sirve los resultados del scoring batch al dashboard de CX.

Deliberadamente es solo lectura: el modelo corre en un job batch mensual aparte y esta
capa se limita a exponer lo que quedo escrito. Eso la hace liviana (arranca en menos de
un segundo, no carga XGBoost) y barata de hostear en Cloud Run escalando a cero.

La autenticacion no vive aca: en Cloud Run el servicio esta detras de Identity-Aware
Proxy, que exige iniciar sesion con Google y estar en la lista de acceso antes de que el
request llegue al contenedor. Por eso esta misma app sirve tambien el dashboard: la
sesion de IAP es una cookie del dominio del servicio, y un front en otro dominio no
podria mandarla.
"""

from __future__ import annotations

import json
import logging
import math
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from app.schemas import (
    AccountDetail,
    AccountPrediction,
    BatchSummary,
    GlobalImportanceRow,
    HealthResponse,
    ModelInfo,
    PagedAccounts,
)
from app.settings import settings
from app.store import PredictionStore, query_accounts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

store = PredictionStore(settings.predictions_dir)


@asynccontextmanager
async def lifespan(app: FastAPI):
    periods = store.available_periods()
    if periods:
        store.load(periods[0])  # precarga el batch mas reciente
        logger.info("API lista. Periodos disponibles: %s", periods)
    else:
        logger.warning(
            "No hay predicciones en %s. La API arranca igual pero responde 404 "
            "hasta que corra el batch.",
            settings.predictions_dir,
        )
    yield


app = FastAPI(
    title=settings.title,
    version=settings.version,
    lifespan=lifespan,
    description=(
        "Predicciones mensuales de churn por cuenta, con probabilidad calibrada, "
        "riesgo economico y las features que explican cada prediccion."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

API = "/api/v1"

# Header que agrega IAP con la cuenta autenticada, como "accounts.google.com:ana@fu.do".
IAP_USER_HEADER = "x-goog-authenticated-user-email"


def get_batch(
    periodo: int | None = Query(None, description="Periodo YYYYMM. Por defecto el ultimo."),
):
    """Dependencia que resuelve el batch pedido y devuelve (dataframe, metadata)."""
    try:
        return store.load(periodo), store.metadata(periodo)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health() -> HealthResponse:
    periods = store.available_periods()
    model_ok = (Path(settings.model_dir) / "metadata.json").exists()
    return HealthResponse(
        status="ok" if periods else "degraded",
        predictions_loaded=bool(periods),
        periodos_disponibles=periods,
        model_loaded=model_ok,
    )


@app.get(f"{API}/me", tags=["meta"])
def me(request: Request) -> dict[str, Any]:
    """Cuenta con la que se inicio sesion en el dashboard.

    Es informativo: el control de acceso lo hace IAP antes de que el request llegue aca,
    no este endpoint. En local no hay IAP y devuelve `authenticated: false`.
    """
    raw = request.headers.get(IAP_USER_HEADER, "")
    email = raw.split(":", 1)[-1] if raw else None
    return {"authenticated": bool(email), "email": email}


@app.get(f"{API}/periodos", response_model=list[int], tags=["meta"])
def periodos() -> list[int]:
    """Periodos con predicciones disponibles, del mas reciente al mas viejo."""
    return store.available_periods()


@app.get(f"{API}/model", response_model=ModelInfo, tags=["meta"])
def model_info() -> ModelInfo:
    """Metadatos y metricas offline del modelo que genero las predicciones."""
    path = Path(settings.model_dir) / "metadata.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="No hay un modelo entrenado disponible.")
    with open(path) as fh:
        meta = json.load(fh)
    return ModelInfo(
        trained_at=meta.get("trained_at", ""),
        n_features=meta.get("n_features", 0),
        training_periods=meta.get("training_periods", []),
        churn_baseline=meta.get("churn_baseline", 0.0),
        decision_threshold=meta.get("decision_threshold", 0.5),
        metrics=meta.get("metrics", {}),
    )


@app.get(f"{API}/summary", response_model=BatchSummary, tags=["predicciones"])
def summary(batch=Depends(get_batch)) -> BatchSummary:
    """Cabecera del dashboard: cuentas y revenue en riesgo por categoria."""
    _, meta = batch
    return BatchSummary(
        periodo=meta["periodo_snapshot"],
        periodo_prediccion=meta["periodo_prediccion"],
        generated_at=meta["generated_at"],
        n_accounts=meta["n_accounts"],
        currency=meta.get("currency", "USD"),
        pricing_loaded=meta.get("pricing_loaded", False),
        decision_threshold=meta.get("decision_threshold", 0.5),
        risk_bands=meta.get("risk_bands", {}),
        summary=meta.get("summary", []),
    )


@app.get(f"{API}/accounts", response_model=PagedAccounts, tags=["predicciones"])
def accounts(
    batch=Depends(get_batch),
    risk: list[str] | None = Query(
        None, description="Filtrar por categoria: alto, medio, bajo, no_churn."
    ),
    search: str | None = Query(None, description="Busqueda por nombre o por ID de cuenta."),
    pais: str | None = Query(None),
    min_probability: float | None = Query(None, ge=0.0, le=1.0),
    seasonal: bool | None = Query(
        None, description="Filtrar cuentas con patron de pausas estacionales."
    ),
    sort_by: str = Query("revenue_at_risk", description="Columna de ordenamiento."),
    ascending: bool = Query(False),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1),
) -> PagedAccounts:
    """Listado paginado de cuentas del batch, ordenado por revenue en riesgo."""
    df, _ = batch
    size = min(size, settings.max_page_size)

    rows, total = query_accounts(
        df,
        risk=risk,
        search=search,
        pais=pais,
        min_probability=min_probability,
        seasonal=seasonal,
        sort_by=sort_by,
        ascending=ascending,
        page=page,
        size=size,
    )
    return PagedAccounts(
        total=total,
        page=page,
        size=size,
        pages=max(1, math.ceil(total / size)),
        items=[AccountPrediction(**_clean(r)) for r in rows.to_dict(orient="records")],
    )


@app.get(f"{API}/accounts/{{account_id}}", response_model=AccountDetail, tags=["predicciones"])
def account_detail(account_id: int, batch=Depends(get_batch)) -> AccountDetail:
    """Detalle de una cuenta con las features que explican su prediccion."""
    df, _ = batch
    rows = df[df["id"] == account_id]
    if rows.empty:
        raise HTTPException(
            status_code=404, detail=f"La cuenta {account_id} no esta en este batch."
        )
    return AccountDetail(**_clean(rows.iloc[0].to_dict()))


@app.get(f"{API}/importance", response_model=list[GlobalImportanceRow], tags=["explicabilidad"])
def importance(batch=Depends(get_batch)) -> list[GlobalImportanceRow]:
    """Importancia global de las features en el batch, medida como |SHAP| promedio."""
    _, meta = batch
    return [GlobalImportanceRow(**row) for row in meta.get("global_importance", [])]


@app.get(f"{API}/distribution", tags=["explicabilidad"])
def distribution(batch=Depends(get_batch), bins: int = Query(20, ge=5, le=100)) -> dict[str, Any]:
    """Histograma de probabilidades de churn: como se reparte el riesgo en la base."""
    df, meta = batch
    counts, edges = pd.cut(df["churn_probability"], bins=bins, retbins=True)
    hist = counts.value_counts().sort_index()
    return {
        "threshold": meta.get("decision_threshold", 0.5),
        "bins": [
            {
                "desde": round(float(edges[i]), 4),
                "hasta": round(float(edges[i + 1]), 4),
                "cuentas": int(hist.iloc[i]),
            }
            for i in range(len(hist))
        ],
    }


def _clean(record: dict[str, Any]) -> dict[str, Any]:
    """Normaliza tipos de numpy/pandas y NaN para que pydantic los acepte."""
    out: dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(value, float) and math.isnan(value):
            out[key] = None
        elif hasattr(value, "item") and not isinstance(value, list | dict):
            out[key] = value.item()
        else:
            out[key] = value
    return out


# ----------------------------------------------------------------- dashboard ---
# Va al final: las rutas de la API, definidas arriba, tienen prioridad sobre los
# archivos estaticos montados en "/".
WEB_DIR = Path(settings.web_dir)

if (WEB_DIR / "index.html").exists():

    @app.get("/config.js", include_in_schema=False)
    def dashboard_config() -> Response:
        """El dashboard le habla a la API que lo sirvio: mismo origen, sin CORS."""
        return Response(
            "window.CHURN_API_URL = window.location.origin;\n",
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="dashboard")
