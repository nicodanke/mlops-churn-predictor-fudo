"""Vinculacion de los tickets de Intercom con las cuentas del snapshot.

Un ticket de Intercom no dice a que cuenta de Fudo pertenece — al menos no siempre. Hay
cuatro caminos para averiguarlo, y este modulo los aplica en orden de confiabilidad:

1. `Ticket Attributes -> "ID de dash"`. Es el dato explicito y el unico que no requiere
   inferencia. Presente en el 22% de los tickets: el 100% de los de integraciones y
   practicamente ninguno de los de soporte, que son los que interesan para churn.

2. La tabla de **companies** de Intercom. Cada ticket trae el `Company ID`, que es el
   ObjectId interno de Intercom (24 caracteres hex), no el id de Fudo. La tabla de
   companies es la que traduce uno en otro. Cubre el 63% de los tickets.

3. El `external_id` del contacto, cuando es un numero que existe como cuenta. Ojo: es
   una pista, no una verdad. Medido contra el atributo "ID de dash", contradice en 31 de
   392 casos verificables, asi que va despues de las dos vias anteriores y nunca las pisa.

4. Un mapa derivado de los propios datos: si un `external_id` largo (el id de usuario de
   Fudo) o un `Company ID` aparecen alguna vez junto a un "ID de dash", esa asociacion se
   propaga al resto de sus tickets. Es lo mas debil, porque depende de que el archivo
   traiga suficiente solapamiento.

Con las cuatro vias sobre el export truncado la cobertura pasa de 22% a 44%. Con la tabla
de companies completa deberia acercarse al 63%, y con la tabla de usuarios de Fudo
(`user_id` -> `account_id`) al 92%, que es la proporcion de tickets que trae `external_id`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# Columnas del export de tickets que se necesitan. `Ticket Parts` se ignora a proposito:
# son las conversaciones completas y explican el 99% del peso del archivo.
TICKET_COLS = [
    "Ticket ID", "Created At", "Updated At", "Company ID", "Contacts",
    "Ticket Attributes", "Ticket Type", "Ticket State", "Category", "Channel",
]

# Atributos utiles dentro de `Ticket Attributes`.
ATTR_FIELDS = ["ID de dash", "Tema", "Motivo", "Submotivo", "Necesita derivación", "País"]

# Nombres que suele tomar el id de Fudo en un export de companies de Intercom.
# `company_id` es el identificador externo que define la empresa (a diferencia de `id`,
# que es el ObjectId interno), asi que es el candidato natural.
COMPANY_ID_CANDIDATES = [
    "company_id", "external_id", "ID de dash", "id_de_dash", "dash_id",
    "account_id", "fudo_id", "id_fudo",
]


def load_tickets(path: str | Path, chunksize: int = 5000) -> pd.DataFrame:
    """Aplana el export de tickets a una tabla plana, sin las conversaciones."""
    partes = []
    for chunk in pd.read_csv(path, chunksize=chunksize, low_memory=False):
        chunk.columns = [c.strip("\ufeff") for c in chunk.columns]
        attrs = chunk["Ticket Attributes"].map(_parse_json_dict)
        contactos = chunk.get("Contacts", pd.Series(dtype=object)).map(_first_contact)

        out = pd.DataFrame(
            {
                "ticket_id": chunk["Ticket ID"],
                "created_at": pd.to_datetime(chunk["Created At"], unit="s", errors="coerce"),
                "company_ref": chunk.get("Company ID"),
                "category": chunk.get("Category"),
                "channel": chunk.get("Channel"),
                "tipo": chunk["Ticket Type"].map(lambda s: _parse_json_dict(s).get("name")),
                "estado": chunk["Ticket State"].map(
                    lambda s: _parse_json_dict(s).get("category")
                ),
                "external_id": contactos.map(lambda c: c.get("external_id")),
                "email": contactos.map(lambda c: c.get("email")),
            }
        )
        for campo in ATTR_FIELDS:
            out[campo] = attrs.map(lambda d, c=campo: d.get(c))
        partes.append(out)

    tickets = pd.concat(partes, ignore_index=True)
    tickets["periodo"] = tickets["created_at"].dt.strftime("%Y%m").astype("Int64")
    logger.info(
        "Tickets leidos: %s (%s a %s)",
        f"{len(tickets):,}",
        tickets["periodo"].min(),
        tickets["periodo"].max(),
    )
    return tickets


def load_companies(path: str | Path) -> pd.DataFrame:
    """Lee el export de companies de Intercom y detecta cual es el id de Fudo.

    Devuelve un DataFrame con `company_ref` (el ObjectId interno, que es lo que traen los
    tickets) y `account_id` (el id de Fudo). Busca la columna del id entre los nombres
    habituales y, si no la encuentra por nombre, la deduce: la primera columna numerica
    cuyos valores parezcan ids de cuenta.
    """
    ruta = str(path)
    df = pd.read_csv(ruta, low_memory=False) if ruta.endswith(".csv") else pd.read_parquet(ruta)
    df.columns = [c.strip("\ufeff").strip() for c in df.columns]

    ref_col = _find_column(df, ["id", "intercom_id", "company_ref"], hex24=True)
    if ref_col is None:
        raise ValueError(
            "El export de companies no tiene una columna con el id interno de Intercom "
            "(24 caracteres hex). Es la que cruza con el 'Company ID' de los tickets."
        )

    id_col = _find_column(df, COMPANY_ID_CANDIDATES, hex24=False)
    if id_col is None:
        id_col = _guess_account_column(df, exclude={ref_col})
    if id_col is None:
        raise ValueError(
            "No se encontro en el export de companies ninguna columna con el id de Fudo. "
            f"Se buscaron: {COMPANY_ID_CANDIDATES}. Revisar los custom attributes."
        )

    logger.info("Companies: id interno en '%s', id de Fudo en '%s'", ref_col, id_col)

    out = pd.DataFrame(
        {
            "company_ref": df[ref_col].astype(str).str.strip(),
            "account_id": pd.to_numeric(df[id_col], errors="coerce").astype("Int64"),
        }
    ).dropna(subset=["account_id"])

    duplicadas = out["company_ref"].duplicated().sum()
    if duplicadas:
        logger.warning("%s companies repetidas; se conserva la primera", f"{duplicadas:,}")
        out = out.drop_duplicates("company_ref", keep="first")

    logger.info("Companies con id de Fudo: %s", f"{len(out):,}")
    return out


def attribute_accounts(
    tickets: pd.DataFrame,
    companies: pd.DataFrame | None = None,
    valid_account_ids: set[int] | None = None,
) -> pd.DataFrame:
    """Agrega `account_id` y `attribution_source` a cada ticket.

    Las vias se aplican en orden de confiabilidad y **no se pisan**: una via posterior
    solo completa los tickets que las anteriores dejaron sin resolver.
    """
    out = tickets.copy()
    directo = pd.to_numeric(out.get("ID de dash"), errors="coerce").astype("Int64")

    resultado = directo.copy()
    fuente = pd.Series(pd.NA, index=out.index, dtype="object")
    fuente[resultado.notna()] = "id_de_dash"

    # 2. tabla de companies
    if companies is not None and len(companies):
        mapa = companies.set_index("company_ref")["account_id"]
        via_company = out["company_ref"].astype(str).str.strip().map(mapa).astype("Int64")
        resultado, fuente = _fill(resultado, fuente, via_company, "tabla_companies")

    # 3. external_id que existe como cuenta
    ext = pd.to_numeric(
        out["external_id"].astype(str).where(out["external_id"].astype(str).str.isdigit()),
        errors="coerce",
    ).astype("Int64")
    if valid_account_ids:
        ext = ext.where(ext.isin(list(valid_account_ids)))
    resultado, fuente = _fill(resultado, fuente, ext, "external_id")

    # 4. mapas derivados de los propios datos
    for clave, nombre in (("external_id", "mapa_usuario"), ("company_ref", "mapa_company")):
        derivado = _derive_map(out, resultado, clave)
        if derivado is not None:
            resultado, fuente = _fill(resultado, fuente, derivado, nombre)

    out["account_id"] = resultado
    out["attribution_source"] = fuente

    cobertura = out["account_id"].notna().mean()
    logger.info(
        "Tickets atribuidos a una cuenta: %s de %s (%.0f%%)",
        f"{int(out['account_id'].notna().sum()):,}",
        f"{len(out):,}",
        cobertura * 100,
    )
    return out


def attribution_report(tickets: pd.DataFrame) -> pd.DataFrame:
    """Cuantos tickets resolvio cada via. Sirve para saber que falta conseguir."""
    total = len(tickets)
    conteo = tickets["attribution_source"].value_counts(dropna=False).rename("tickets")
    out = conteo.reset_index()
    out.columns = ["via", "tickets"]
    out["via"] = out["via"].fillna("sin atribuir")
    out["pct"] = (out["tickets"] / total * 100).round(1)
    return out


# ---------------------------------------------------------------------------


def _fill(
    actual: pd.Series, fuente: pd.Series, candidato: pd.Series, nombre: str
) -> tuple[pd.Series, pd.Series]:
    """Completa solo donde todavia no hay valor: las vias no se pisan entre si."""
    faltante = actual.isna() & candidato.notna()
    actual = actual.where(~faltante, candidato)
    fuente = fuente.where(~faltante, nombre)
    return actual, fuente


def _derive_map(tickets: pd.DataFrame, resueltos: pd.Series, clave: str) -> pd.Series | None:
    """Propaga la cuenta conocida de una clave al resto de sus tickets.

    Solo se propaga si la clave apunta siempre a la misma cuenta: una clave ambigua no
    aporta informacion, aporta ruido.
    """
    if clave not in tickets.columns:
        return None
    base = pd.DataFrame({"clave": tickets[clave].astype(str), "cuenta": resueltos})
    base = base[base["cuenta"].notna() & base["clave"].ne("None") & base["clave"].ne("nan")]
    if base.empty:
        return None

    unicas = base.groupby("clave")["cuenta"].nunique()
    mapa = base.drop_duplicates("clave").set_index("clave")["cuenta"]
    mapa = mapa[mapa.index.isin(unicas[unicas == 1].index)]
    if mapa.empty:
        return None
    return tickets[clave].astype(str).map(mapa).astype("Int64")


def _parse_json_dict(value: Any) -> dict:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_contact(value: Any) -> dict:
    parsed = _parse_json_dict(value)
    contactos = parsed.get("contacts") or []
    return contactos[0] if contactos else {}


def _find_column(df: pd.DataFrame, nombres: list[str], hex24: bool) -> str | None:
    """Busca una columna por nombre, tolerando mayusculas y separadores."""
    normal = {c.lower().replace(" ", "_").replace("-", "_"): c for c in df.columns}
    for nombre in nombres:
        clave = nombre.lower().replace(" ", "_").replace("-", "_")
        if clave in normal:
            columna = normal[clave]
            if not hex24 or _looks_hex24(df[columna]):
                return columna
    if hex24:
        for columna in df.columns:
            if _looks_hex24(df[columna]):
                return columna
    return None


def _looks_hex24(serie: pd.Series) -> bool:
    muestra = serie.dropna().astype(str).head(50)
    if muestra.empty:
        return False
    return bool((muestra.str.fullmatch(r"[0-9a-fA-F]{24}")).mean() > 0.8)


def _guess_account_column(df: pd.DataFrame, exclude: set[str]) -> str | None:
    """Ultimo recurso: la columna numerica que mas parece un id de cuenta de Fudo."""
    mejor, mejor_score = None, 0.0
    for columna in df.columns:
        if columna in exclude:
            continue
        valores = pd.to_numeric(df[columna], errors="coerce").dropna()
        if len(valores) < max(10, 0.1 * len(df)):
            continue
        # Los ids de cuenta son enteros positivos de hasta seis cifras.
        score = float(((valores > 0) & (valores < 1_000_000) & (valores % 1 == 0)).mean())
        if score > mejor_score and score > 0.9:
            mejor, mejor_score = columna, score
    if mejor:
        logger.warning(
            "El id de Fudo se dedujo de la columna '%s' por su forma; conviene verificarlo",
            mejor,
        )
    return mejor
