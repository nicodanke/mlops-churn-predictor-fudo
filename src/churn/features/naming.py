"""Traduccion de nombres de features a texto legible para el equipo de CX.

El modelo trabaja con nombres como `ad_pc_ratio_3m`; el dashboard tiene que mostrar
"Adiciones desde la app de escritorio - nivel actual vs promedio de los 3 meses previos".
"""

from __future__ import annotations

# Descripciones tomadas de la documentacion de Fudata (Base de Funcionalidades).
BASE_LABELS: dict[str, str] = {
    "mesas": "Mesas configuradas",
    "salas": "Salas configuradas",
    "productos": "Productos cargados",
    "cat_productos": "Categorias de productos",
    "pr_con_stock": "Productos con control de stock",
    "pr_con_costo": "Productos con costo configurado",
    "ingredientes": "Ingredientes cargados",
    "cat_ingredientes": "Categorias de ingredientes",
    "ing_con_stock": "Ingredientes con control de stock",
    "ing_con_costo": "Ingredientes con costo configurado",
    "ing_en_recetas": "Ingredientes usados en recetas",
    "sub_ing_en_recetas": "Sub-ingredientes en recetas",
    "usuarios": "Usuarios activos",
    "v_salon": "Ventas en salon",
    "v_delivery": "Ventas de delivery",
    "v_mostrador": "Ventas por mostrador",
    "v_menu_online": "Ventas por Menu Online",
    "v_pedidosya": "Ventas via PedidosYa",
    "v_ubereats": "Ventas via Uber Eats",
    "v_justo": "Ventas via Justo",
    "v_rappi": "Ventas via Rappi",
    "v_ifood": "Ventas via iFood",
    "v_didi": "Ventas via DiDi",
    "ad_pc": "Adiciones desde la app de escritorio",
    "ad_tablet": "Adiciones desde la app de camareros",
    "ad_mobile": "Adiciones desde la app de comensales",
    "ad_lista_de_precio": "Adiciones con lista de precios",
    "ad_combo": "Adiciones de grupos modificadores",
    "arqueos": "Arqueos de caja",
    "propinas": "Propinas registradas",
    "movimientos_caja": "Movimientos de caja",
    "gastos": "Gastos registrados",
    "fiscal": "Facturas registradas",
    "menu_online_habilitado": "Menu Online habilitado",
    "carta_qr_habilitado": "Carta QR habilitada",
    "zonas_de_delivery": "Zonas de delivery configuradas",
    "adiciones_movidas_de_otras_mesas": "Adiciones movidas entre mesas",
    "ventas_con_camarero_o_repartidor": "Ventas con camarero o repartidor asignado",
    "descuentos": "Descuentos otorgados",
    "transacciones_de_cta_cte_de_clientes": "Transacciones de cuenta corriente de clientes",
    "productos_favoritos": "Productos marcados como favoritos",
    "notificaciones_por_falta_de_stock": "Notificaciones por falta de stock",
    "productos_con_venta_sin_disponibilidad": "Productos vendibles sin disponibilidad",
    "usuarios_con_pin_asinado": "Usuarios con PIN asignado",
    "ventas_con_clientes_asignados": "Ventas con comensal asociado",
    "ventas_pagadas_con_mp": "Ventas pagadas con Mercado Pago",
    "cantidad_de_proveedores": "Proveedores activos",
    "transacciones_de_cta_cte_de_proveedores": "Transacciones de cuenta corriente de proveedores",
    "cantidad_de_clientes": "Comensales registrados",
    "usuarios_con_cajas_asignadas": "Usuarios con caja asignada",
    "cantidad_de_cajas": "Cajas registradoras",
    "cantidad_de_turnos": "Turnos configurados",
    "clientes_con_descuentos_automaticos": "Comensales con descuentos automaticos",
    "ventas_deli_con_repartidor_asignado": "Ventas de delivery con repartidor asignado",
    "cantidad_de_costos_de_envio": "Costos de envio aplicados",
    "sii_set_basico": "Habilitado para facturar por SII",
    "sii_boletas": "Habilitado para emitir boletas por SII",
    "sii_cantidad_de_facturas": "Facturas emitidas por SII",
    "sii_cantiad_de_boletas": "Boletas emitidas por SII",
    "sii_cantidad_de_notas": "Notas de credito emitidas por SII",
    "cat_gastos": "Categorias de gastos",
    "sub_cat_gastos": "Sub-categorias de gastos",
    "cat_gastos_con_cat_financiera": "Categorias de gastos con categoria financiera",
    # Derivadas del pipeline.
    "ventas_totales": "Ventas totales del mes",
    "adiciones_totales": "Adiciones totales del mes",
    "canales_venta_activos": "Canales de venta con actividad",
    "ratio_ventas_delivery": "Peso del delivery sobre el total de ventas",
    "ratio_ventas_salon": "Peso del salon sobre el total de ventas",
    "sin_ventas_en_el_mes": "Mes sin ninguna venta registrada",
    "funcionalidades_en_uso": "Funcionalidades del producto con actividad",
    "ratio_productos_con_stock": "Cobertura de control de stock en productos",
    "ratio_productos_con_costo": "Cobertura de costos en productos",
    "ratio_ingredientes_con_stock": "Cobertura de control de stock en ingredientes",
    "ratio_ingredientes_con_costo": "Cobertura de costos en ingredientes",
    "ratio_ingredientes_en_recetas": "Ingredientes efectivamente usados en recetas",
    "ratio_usuarios_con_pin": "Usuarios con PIN sobre el total",
    "ratio_usuarios_con_caja": "Usuarios con caja asignada sobre el total",
    "ratio_ventas_con_cliente": "Ventas con comensal identificado sobre el total",
    "ratio_cat_gastos_con_financiera": "Categorias de gasto con categoria financiera",
    "productos_por_categoria": "Productos por categoria",
    "tenure_months": "Antiguedad de la cuenta (meses desde el alta)",
    "months_in_panel": "Meses observados en el panel",
    "meses_desde_alta_en_panel": "Meses desde la primera aparicion en el panel",
    "pausas_previas": "Meses en los que la cuenta estuvo ausente antes",
    "estado_severity": "Severidad del estado de cobranza",
    "estado_severity_max_3m": "Peor estado de cobranza de los ultimos 3 meses",
    "estado_severity_delta_1m": "Cambio en el estado de cobranza vs mes anterior",
    "meses_consecutivos_con_deuda": "Meses consecutivos fuera de estado ACTIVE",
    "plan_rank": "Nivel del plan contratado",
    "plan_n_modulos": "Modulos contratados",
    "plan_mercado": "Mercado de facturacion",
    "plan_base": "Plan base contratado",
    "pais_cat": "Pais de la cuenta",
    "plan_cambio_1m": "Hubo cambio de plan respecto del mes anterior",
    "plan_rank_delta_1m": "Cambio de nivel de plan vs mes anterior",
    "plan_modulos_delta_1m": "Cambio en cantidad de modulos vs mes anterior",
    "plan_downgrade_1m": "Downgrade de plan o baja de modulo en el mes",
}

# Sufijos que agrega el feature engineering temporal.
SUFFIX_LABELS: list[tuple[str, str]] = [
    ("_delta_1m", "variacion vs mes anterior"),
    ("_delta_3m", "variacion vs hace 3 meses"),
    ("_ratio_3m", "nivel actual vs promedio de los 3 meses previos"),
    ("_slope_3m", "tendencia de los ultimos 3 meses"),
]

MODULE_FEATURE_PREFIX = "plan_modulo_"


def humanize_feature(name: str) -> str:
    """`ad_pc_ratio_3m` -> 'Adiciones desde la app de escritorio (nivel actual vs ...)'."""
    if name in BASE_LABELS:
        return BASE_LABELS[name]

    if name.startswith(MODULE_FEATURE_PREFIX):
        from churn.pricing.plans import MODULE_LABELS

        code = name[len(MODULE_FEATURE_PREFIX) :]
        return f"Modulo {MODULE_LABELS.get(code, code)} contratado"

    for suffix, description in SUFFIX_LABELS:
        if name.endswith(suffix):
            base = name[: -len(suffix)]
            label = BASE_LABELS.get(base, base.replace("_", " ").capitalize())
            return f"{label} ({description})"

    return name.replace("_", " ").capitalize()


def feature_group(name: str) -> str:
    """Agrupa la feature en una dimension de negocio, para agrupar en el dashboard."""
    if name.startswith("plan"):
        return "Plan y contratacion"
    if name.startswith("estado") or "deuda" in name:
        return "Cobranza"
    if name.startswith(("v_", "ventas", "ratio_ventas", "canales")):
        return "Ventas"
    if name.startswith(("ad_", "adiciones")):
        return "Operacion diaria"
    if name.startswith(("pr_", "ing_", "productos", "ingredientes", "cat_", "sub_", "ratio_")):
        return "Configuracion del producto"
    if name.startswith(("usuarios", "cantidad_de_cajas", "cantidad_de_turnos")):
        return "Equipo y accesos"
    if name in {"tenure_months", "months_in_panel", "meses_desde_alta_en_panel", "pausas_previas"}:
        return "Ciclo de vida"
    return "Otros"
