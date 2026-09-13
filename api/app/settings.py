"""Configuracion de la API por variables de entorno (12-factor, listo para Cloud Run)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHURN_API_", env_file=".env", extra="ignore")

    # Directorio con los batches de predicciones. Acepta gs://bucket/predictions en GCP.
    predictions_dir: str = str(PROJECT_ROOT / "outputs" / "predictions")
    model_dir: str = str(PROJECT_ROOT / "models")
    # Dashboard estatico. Si el directorio existe, la API tambien lo sirve en "/": en Cloud
    # Run van juntos en un solo servicio detras de Identity-Aware Proxy (ver main.py).
    web_dir: str = str(PROJECT_ROOT / "web")

    title: str = "Fudo Churn API"
    version: str = "0.1.0"
    # Origenes permitidos para un front en otro dominio. "*" solo para desarrollo local
    # (docker compose sirve el dashboard en otro puerto). En Cloud Run va vacio: el
    # dashboard se sirve desde el mismo origen.
    cors_origins: str = "*"
    # Tamano maximo de pagina en el listado de cuentas.
    max_page_size: int = 200

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
