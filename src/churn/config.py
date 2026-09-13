"""Carga y acceso tipado a la configuracion del pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "model.yaml"
DEFAULT_PRICING_PATH = PROJECT_ROOT / "config" / "pricing.yaml"


def _resolve(path: str | Path) -> str:
    """Deja intactas las URIs remotas (gs://, s3://) y absolutiza las rutas locales."""
    s = str(path)
    if "://" in s:
        return s
    p = Path(s)
    return str(p if p.is_absolute() else PROJECT_ROOT / p)


def _read_yaml(path: Path, visited: tuple[Path, ...] = ()) -> dict[str, Any]:
    """Lee un YAML resolviendo `extends: otro.yaml`, relativo al archivo que lo declara.

    Permite que una variante (la config de GCP, un experimento) declare solo lo que
    cambia y herede el resto, en vez de copiar el archivo entero y que las dos copias
    se desincronicen: un cambio de hiperparametros en model.yaml tiene que llegar igual
    al entrenamiento en la nube.
    """
    resolved = path.resolve()
    if resolved in visited:
        cadena = " -> ".join(str(p) for p in (*visited, resolved))
        raise ValueError(f"Herencia circular de configuracion: {cadena}")

    with open(path) as fh:
        data = yaml.safe_load(fh) or {}

    base = data.pop("extends", None)
    if base is None:
        return data
    base_path = Path(base)
    if not base_path.is_absolute():
        base_path = path.parent / base_path
    return _deep_merge(_read_yaml(base_path, (*visited, resolved)), data)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Mezcla diccionarios recursivamente. Listas y escalares se reemplazan enteros."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass
class Config:
    """Wrapper sobre el YAML de configuracion con acceso por path punteado."""

    raw: dict[str, Any] = field(default_factory=dict)
    path: Path = DEFAULT_CONFIG_PATH

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        cfg_path = Path(path) if path else Path(os.getenv("CHURN_CONFIG", DEFAULT_CONFIG_PATH))
        return cls(raw=_read_yaml(cfg_path), path=cfg_path)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path_of(self, dotted: str, default: str | None = None) -> str:
        """Igual que get() pero resolviendo la ruta contra la raiz del proyecto."""
        value = self.get(dotted, default)
        if value is None:
            raise KeyError(f"No hay ruta configurada en '{dotted}'")
        return _resolve(value)

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]
