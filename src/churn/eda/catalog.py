"""Catalogo de funcionalidades del producto, con etiquetas legibles.

El snapshot trae 73 columnas con nombres de data warehouse. Para el reporte de uso hace
falta saber cuales representan una funcionalidad que la cuenta *adopta* (y se puede
contar como "la usa / no la usa") y como se llama esa funcionalidad en la interfaz.

No es el mismo recorte que usa el modelo: aca sobran las columnas derivadas y faltan
las de cobranza, que describen la relacion comercial y no el uso del producto.

Un detalle del export que cambia como se cuenta: en las metricas de evento el DW
escribe **null, no cero**, cuando la cuenta no uso la funcionalidad en el mes. En
`ad_pc`, por ejemplo, el 10,6% de las filas del ultimo periodo son null y el 100% de
las que tienen valor son mayores a cero. Por eso el reporte cuenta adopcion como
`fillna(0) > 0`: un null es una cuenta que no lo uso, no un dato que falta.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Funcionalidad:
    """Una metrica del snapshot leida como "funcionalidad que la cuenta usa o no"."""

    columna: str
    etiqueta: str
    grupo: str


# Orden de los grupos en el reporte y en el dashboard.
GRUPOS: tuple[str, ...] = (
    "Canales de venta",
    "Apps de delivery",
    "Dispositivos de carga",
    "Caja y finanzas",
    "Stock y recetas",
    "Clientes",
    "Presencia online",
    "Operacion del salon",
)

FUNCIONALIDADES: tuple[Funcionalidad, ...] = (
    # Canales de venta: por donde entra la venta.
    Funcionalidad("v_salon", "Salon", "Canales de venta"),
    Funcionalidad("v_mostrador", "Mostrador", "Canales de venta"),
    Funcionalidad("v_delivery", "Delivery propio", "Canales de venta"),
    Funcionalidad("v_menu_online", "Menu online", "Canales de venta"),
    # Apps: integraciones con marketplaces.
    Funcionalidad("v_pedidosya", "PedidosYa", "Apps de delivery"),
    Funcionalidad("v_rappi", "Rappi", "Apps de delivery"),
    Funcionalidad("v_ubereats", "Uber Eats", "Apps de delivery"),
    Funcionalidad("v_ifood", "iFood", "Apps de delivery"),
    Funcionalidad("v_didi", "DiDi Food", "Apps de delivery"),
    Funcionalidad("v_justo", "Justo", "Apps de delivery"),
    # Dispositivos desde los que se carga la comanda.
    Funcionalidad("ad_pc", "PC", "Dispositivos de carga"),
    Funcionalidad("ad_tablet", "Tablet", "Dispositivos de carga"),
    Funcionalidad("ad_mobile", "Mobile", "Dispositivos de carga"),
    # Caja y finanzas.
    Funcionalidad("arqueos", "Arqueo de caja", "Caja y finanzas"),
    Funcionalidad("movimientos_caja", "Movimientos de caja", "Caja y finanzas"),
    Funcionalidad("gastos", "Registro de gastos", "Caja y finanzas"),
    Funcionalidad("cat_gastos", "Categorias de gasto", "Caja y finanzas"),
    Funcionalidad("propinas", "Propinas", "Caja y finanzas"),
    Funcionalidad("fiscal", "Facturacion fiscal", "Caja y finanzas"),
    Funcionalidad("ventas_pagadas_con_mp", "Cobro con Mercado Pago", "Caja y finanzas"),
    # Stock y recetas: el modulo mas profundo del producto.
    Funcionalidad("pr_con_stock", "Productos con stock", "Stock y recetas"),
    Funcionalidad("pr_con_costo", "Productos con costo", "Stock y recetas"),
    Funcionalidad("ingredientes", "Ingredientes cargados", "Stock y recetas"),
    Funcionalidad("ing_en_recetas", "Ingredientes en recetas", "Stock y recetas"),
    Funcionalidad(
        "notificaciones_por_falta_de_stock", "Alertas de falta de stock", "Stock y recetas"
    ),
    Funcionalidad("cantidad_de_proveedores", "Proveedores", "Stock y recetas"),
    # Clientes.
    Funcionalidad("cantidad_de_clientes", "Base de clientes", "Clientes"),
    Funcionalidad("ventas_con_clientes_asignados", "Ventas con cliente asignado", "Clientes"),
    Funcionalidad("transacciones_de_cta_cte_de_clientes", "Cuenta corriente", "Clientes"),
    Funcionalidad("clientes_con_descuentos_automaticos", "Descuentos automaticos", "Clientes"),
    Funcionalidad("descuentos", "Descuentos en la venta", "Clientes"),
    # Presencia online.
    Funcionalidad("menu_online_habilitado", "Menu online habilitado", "Presencia online"),
    Funcionalidad("carta_qr_habilitado", "Carta QR", "Presencia online"),
    Funcionalidad("zonas_de_delivery", "Zonas de delivery", "Presencia online"),
    Funcionalidad(
        "ventas_deli_con_repartidor_asignado", "Repartidor asignado", "Presencia online"
    ),
    # Operacion del salon.
    Funcionalidad("mesas", "Mesas configuradas", "Operacion del salon"),
    Funcionalidad("salas", "Salas configuradas", "Operacion del salon"),
    Funcionalidad("usuarios_con_pin_asinado", "Usuarios con PIN", "Operacion del salon"),
    Funcionalidad("cantidad_de_cajas", "Cajas", "Operacion del salon"),
    Funcionalidad("cantidad_de_turnos", "Turnos", "Operacion del salon"),
    Funcionalidad("ventas_con_camarero_o_repartidor", "Venta con mozo", "Operacion del salon"),
    Funcionalidad(
        "adiciones_movidas_de_otras_mesas", "Mover items entre mesas", "Operacion del salon"
    ),
    Funcionalidad("productos_favoritos", "Productos favoritos", "Operacion del salon"),
    Funcionalidad("ad_lista_de_precio", "Listas de precio", "Operacion del salon"),
    Funcionalidad("ad_combo", "Combos", "Operacion del salon"),
)

# Metricas de volumen: no se leen como "la usa o no" sino por su magnitud.
# Son las que describen el tamaño de la cuenta.
VOLUMEN: tuple[tuple[str, str], ...] = (
    ("ventas_totales", "Ventas en el mes"),
    ("usuarios", "Usuarios"),
    ("productos", "Productos"),
    ("cantidad_de_clientes", "Clientes"),
    ("mesas", "Mesas"),
    ("adiciones_totales", "Items cargados"),
    ("funcionalidades_en_uso", "Funcionalidades en uso"),
    ("canales_venta_activos", "Canales de venta activos"),
    ("tenure_months", "Antiguedad (meses)"),
)

# Señales que se comparan entre cuentas que se van y cuentas que se quedan.
# Se eligieron las que un analista de CX puede accionar: uso del producto, no cobranza.
SENALES_CHURN: tuple[tuple[str, str], ...] = (
    # `canales_venta_activos` no esta: como adopcion es identico a `ventas_totales`
    # (tener un canal activo es, por definicion, haber vendido).
    ("ventas_totales", "Ventas en el mes"),
    ("funcionalidades_en_uso", "Funcionalidades en uso"),
    ("adiciones_totales", "Items cargados"),
    ("usuarios", "Usuarios activos"),
    ("productos", "Productos en carta"),
    ("arqueos", "Arqueos de caja"),
    ("movimientos_caja", "Movimientos de caja"),
    ("gastos", "Gastos registrados"),
    ("fiscal", "Comprobantes fiscales"),
    ("cantidad_de_clientes", "Clientes en la base"),
    ("descuentos", "Descuentos aplicados"),
    ("ad_tablet", "Carga desde tablet"),
    ("pr_con_stock", "Productos con stock"),
    # `tenure_months` no esta: como adopcion solo dice si la fecha de alta es conocida.
    # La antiguedad si separa mucho, pero por tramos — ver `_churn_por_antiguedad`.
)
