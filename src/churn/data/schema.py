"""Mapeo entre los headers del CSV del data warehouse y nombres de columna canonicos.

El export viene con headers "bonitos" ("Cat Productos", "Pr Con Costo ($)") mientras que
la documentacion de Fudata usa snake_case. Trabajamos siempre con snake_case.
"""

from __future__ import annotations

import re

# Columnas que identifican la fila, no son features.
ID_COLS = ["internal_pk", "periodo", "id", "nombre", "fecha"]

# Columnas categoricas / de contexto.
CATEGORICAL_COLS = ["plan", "pais", "estado", "estado_comercial", "direcciones_google_maps"]

# Columnas que nunca entran al modelo:
#  - internal_pk: id tecnico de la tabla, sin significado.
#  - nombre: texto libre, riesgo de memorizar cuentas.
#  - estado_comercial: constante ACTIVE en todo el dataset (el reporte solo trae activas).
#  - plan: se usa descompuesto en base/modulos/mercado, no como string.
DROP_FROM_FEATURES = ["internal_pk", "nombre", "estado_comercial", "plan", "fecha"]

# Metricas nucleo del uso del producto. Se les calculan tendencias y pendientes,
# ademas de los deltas que se calculan para todas las numericas.
CORE_USAGE_COLS = [
    "usuarios",
    "productos",
    "mesas",
    "ad_pc",
    "ad_tablet",
    "arqueos",
    "movimientos_caja",
    "gastos",
    "fiscal",
    "descuentos",
    "ventas_con_camarero_o_repartidor",
    "cantidad_de_clientes",
]

# Columnas de ventas por canal. Su suma es el volumen total de la cuenta.
SALES_COLS = [
    "v_salon",
    "v_delivery",
    "v_mostrador",
    "v_menu_online",
    "v_pedidosya",
    "v_ubereats",
    "v_justo",
    "v_rappi",
    "v_ifood",
    "v_didi",
]

# Columnas de adiciones (cargas de items a una comanda) por origen.
ADDITION_COLS = ["ad_pc", "ad_tablet", "ad_mobile"]

# Estados de cuenta ordenados por severidad. Un estado peor es señal de riesgo.
ESTADO_SEVERITY = {
    "ACTIVE": 0,
    "PAYMENT_VERIFICATION": 1,
    "PENDING_PAYMENT": 2,
    "BLOCKING_ALERT": 3,
    "BLOCKED": 4,
}


def normalize_column(name: str) -> str:
    """'Pr Con Costo ($)' -> 'pr_con_costo'."""
    out = name.strip().lower()
    out = out.replace("($)", "")
    out = re.sub(r"[^a-z0-9]+", "_", out)
    return out.strip("_")
