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

    title: str = "Fudo Churn API"
    version: str = "0.1.0"
    # Origenes permitidos para el front. "*" solo para desarrollo local.
    cors_origins: str = "*"
    # Tamano maximo de pagina en el listado de cuentas.
    max_page_size: int = 200

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
