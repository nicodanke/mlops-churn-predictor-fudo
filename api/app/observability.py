"""Logs estructurados, pensados para Cloud Logging.

Cloud Run manda a Cloud Logging todo lo que la app escribe por salida estandar. Si la
linea es un JSON valido, Logging la parsea a `jsonPayload` y cada campo se vuelve
filtrable y graficable; si es texto plano queda en `textPayload` y lo unico que se puede
hacer es buscar texto. Por eso en la nube se emite JSON y en local, texto legible.

    gcloud logging read 'jsonPayload.event="request" AND jsonPayload.status>=500'

Convenciones de Cloud Logging que se respetan:

    severity                       nivel del log; es lo que permite filtrar por errores
    logging.googleapis.com/trace   ata entre si todas las lineas de un mismo request

No se loguea nada que identifique a una cuenta ni a una persona: se registra la ruta del
endpoint (`/api/v1/accounts/{account_id}`) y no la concreta, asi que el id de la cuenta no
queda en los logs. Tampoco los nombres, ni el email de la sesion de IAP.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger(__name__)

# Cloud Run expone el servicio y la revision como variables de entorno. Sirven para saber
# que version emitio cada linea, que es lo que se mira despues de un rollback.
SERVICE = os.getenv("K_SERVICE")
REVISION = os.getenv("K_REVISION")
# El id de proyecto no lo inyecta Cloud Run; sin el no se puede armar el nombre completo
# del trace, asi que en ese caso se guarda solo el id crudo.
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT")

TRACE_HEADER = "x-cloud-trace-context"
TRACE_FIELD = "logging.googleapis.com/trace"

# Parametros de la query string que se pueden loguear: describen el pedido, no a nadie.
QUERY_PARAMS_LOGUEABLES = ("periodo", "risk", "page", "size")


class JsonFormatter(logging.Formatter):
    """Una linea JSON por evento, con los campos que Cloud Logging entiende."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        # Lo que agrega log_event(), que es lo que despues se consulta por campo.
        payload.update(getattr(record, "structured", None) or {})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def usa_json(modo: str) -> bool:
    """En la nube JSON, en local texto. `K_SERVICE` solo existe dentro de Cloud Run."""
    if modo == "json":
        return True
    if modo == "text":
        return False
    return bool(SERVICE)


def setup_logging(modo: str = "auto") -> None:
    handler = logging.StreamHandler()
    if usa_json(modo):
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

    # uvicorn ya escribe su propia linea por request, en texto plano. Con el middleware
    # esa linea seria un duplicado peor: sin latencia y sin campos.
    logging.getLogger("uvicorn.access").disabled = True

    # uvicorn se configura sus propios handlers y no propaga al root, asi que sus lineas
    # de arranque saldrian en texto aunque todo lo demas sea JSON. Se las deja colgando
    # del root para que pasen por el mismo formateador.
    for nombre in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(nombre)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


def log_event(nivel: int, mensaje: str, **campos: Any) -> None:
    """Emite un evento con campos consultables. En texto plano se ve solo el mensaje."""
    logger.log(nivel, mensaje, extra={"structured": campos})


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Una linea por request, con latencia y resultado.

    Es la base del monitoreo sin infraestructura extra: con esto alcanza para ver
    latencias, tasa de error y que revision las produjo.
    """

    async def dispatch(self, request: Request, call_next):
        inicio = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log_event(
                logging.ERROR,
                f"{request.method} {request.url.path} fallo",
                **_campos(request, status=500, inicio=inicio),
            )
            raise

        # /health lo llama el chequeo de Cloud Run todo el tiempo: en INFO seria ruido.
        nivel = logging.DEBUG if request.url.path == "/health" else logging.INFO
        if response.status_code >= 500:
            nivel = logging.ERROR
        elif response.status_code >= 400:
            nivel = logging.WARNING

        campos = _campos(request, status=response.status_code, inicio=inicio)
        log_event(nivel, f"{request.method} {campos['path']} {response.status_code}", **campos)
        return response


def _campos(request: Request, status: int, inicio: float) -> dict[str, Any]:
    campos: dict[str, Any] = {
        "event": "request",
        "method": request.method,
        # La ruta del endpoint, no la concreta: sin ids de cuentas y sin cardinalidad alta.
        "path": _ruta(request),
        "status": status,
        "latency_ms": round((time.perf_counter() - inicio) * 1000, 2),
    }
    if REVISION:
        campos["revision"] = REVISION
    campos.update(_query(request))
    campos.update(_trace(request))
    return campos


def _ruta(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path_format", None) or request.url.path


def _query(request: Request) -> dict[str, Any]:
    return {
        f"query_{k}": request.query_params[k]
        for k in QUERY_PARAMS_LOGUEABLES
        if k in request.query_params
    }


def _trace(request: Request) -> dict[str, Any]:
    """Del header que agrega Cloud Run, para agrupar las lineas de un mismo request."""
    header = request.headers.get(TRACE_HEADER, "")
    trace_id = header.split("/", 1)[0]
    if not trace_id:
        return {}
    if PROJECT_ID:
        return {TRACE_FIELD: f"projects/{PROJECT_ID}/traces/{trace_id}"}
    return {"trace_id": trace_id}
