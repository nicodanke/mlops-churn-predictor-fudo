# Datos

El snapshot mensual (`account_stats_since_2024.csv`) **no está versionado**: son ~160 MB
con información de cuentas reales.

## Cómo obtenerlo

Es un export de la tabla `accounts_stats` del Data Warehouse de Fudo (también replicada
en Fudata ClickHouse). Cada fila es una cuenta en un mes:

```sql
SELECT * FROM accounts_stats WHERE periodo >= 202401 ORDER BY periodo, id
```

Colocá el archivo acá con el nombre `account_stats_since_2024.csv`, o apuntá
`data.raw_path` en `config/model.yaml` a donde lo tengas.

### Un archivo o varios

`data.raw_path` acepta tres formas:

| Valor | Qué lee |
|---|---|
| `data/account_stats_since_2024.csv` | ese archivo |
| `data/` | todos los `.csv`, `.csv.gz` y `.parquet` del directorio |
| `data/account_stats_*.csv` | los que matcheen el patrón |
| `gs://bucket/raw/stats.csv` | desde Cloud Storage |

Para la actualización mensual lo cómodo es dejar `raw_path: "data/"` y tirar ahí el archivo
de cada mes. Los archivos se concatenan y, si un mismo (cuenta, período) aparece repetido,
gana el del archivo que ordene último por nombre — así se puede corregir un mes ya cargado
sin borrar el anterior.

**El histórico tiene que quedarse.** El feature engineering necesita los tres meses previos
de cada cuenta para calcular deltas y tendencias: si en `data/` queda sólo el mes nuevo,
esas features salen todas vacías y el modelo pierde casi toda su señal.

## Qué contiene

- **Granularidad**: una fila por (cuenta, mes). Clave: `id` + `periodo` (YYYYMM).
- **Cobertura**: desde 202401, se agrega un período a principios de cada mes.
- **Sesgo de inclusión clave**: el reporte **solo incluye cuentas con estado comercial
  ACTIVE** al momento de generarse. De ahí se deriva la etiqueta de churn: si una cuenta
  deja de aparecer, dejó de estar activa.
- **Columnas**: ~73, descriptas en [`docs/Fudata - Base de Funcionalidades.pdf`](../docs/).

## Datos sensibles

El CSV incluye `nombre` (razón social del restaurante). El pipeline lo excluye
explícitamente de las features del modelo (ver `DROP_FROM_FEATURES` en
[`src/churn/data/schema.py`](../src/churn/data/schema.py)) y sólo lo usa para mostrarlo
en el dashboard.
