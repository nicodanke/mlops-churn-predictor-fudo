# Datos

Los snapshots mensuales (`account-stats-AAAAMM.csv`) **no están versionados**: son ~140 MB
en total con información de cuentas reales.

El panel en uso va de **202501 a 202608**: 20 archivos, 599.967 filas, 53.160 cuentas.

## Cómo obtenerlo

Es un export de la tabla `accounts_stats` del Data Warehouse de Fudo (también replicada
en Fudata ClickHouse). Cada fila es una cuenta en un mes:

```sql
SELECT * FROM accounts_stats WHERE periodo = 202609 ORDER BY id
```

Guardá el resultado acá como `account-stats-202609.csv`. El nombre importa sólo por el
orden alfabético (ver abajo); lo que define el período es la columna `Periodo`.

`data.raw_path` en `config/model.yaml` apunta a `data/account-stats-*.csv`.

### Un archivo o varios

`data.raw_path` acepta tres formas:

| Valor | Qué lee |
|---|---|
| `data/account-stats-*.csv` | los que matcheen el patrón (lo que usa el proyecto) |
| `data/` | todos los `.csv`, `.csv.gz` y `.parquet` del directorio |
| `data/account-stats-202601.csv` | ese archivo |
| `gs://bucket/raw/stats.csv` | desde Cloud Storage |

Actualizar es dejar caer el archivo del mes nuevo en `data/`. Los archivos se concatenan
y, si un mismo (cuenta, período) aparece repetido, gana el del archivo que ordene último
por nombre — así se puede corregir un mes ya cargado sin borrar el anterior. Por eso el
nombre lleva el período en formato `AAAAMM`: ordena igual alfabética que cronológicamente.

**El histórico tiene que quedarse.** El feature engineering necesita los tres meses previos
de cada cuenta para calcular deltas y tendencias: si en `data/` queda sólo el mes nuevo,
esas features salen todas vacías y el modelo pierde casi toda su señal.

## Qué contiene

- **Granularidad**: una fila por (cuenta, mes). Clave: `id` + `periodo` (YYYYMM).
- **Cobertura**: desde 202501, se agrega un período a principios de cada mes.
- **Sesgo de inclusión clave**: el reporte **solo incluye cuentas con estado comercial
  ACTIVE** al momento de generarse. De ahí se deriva la etiqueta de churn: si una cuenta
  deja de aparecer, dejó de estar activa.
- **Columnas**: 73, descriptas en [`docs/Fudata - Base de Funcionalidades.pdf`](../docs/).
- **Los nulos no son datos faltantes.** En las métricas de evento el DW escribe `null`,
  no `0`, cuando la cuenta no usó la funcionalidad en el mes: en `ad_pc` el 10,6% de las
  filas del último período son `null` y el 100% de las que tienen valor son mayores a
  cero. El pipeline los trata como "no lo usó", que es lo que significan.
- **Hay columnas que no existen en todo el rango.** `cat_gastos` y `sub_cat_gastos`
  aparecen recién en 202507 y su mediana sube todos los meses (0 → 11 en 202608): es una
  funcionalidad nueva en plena adopción. Están excluidas del modelo por eso — ver
  `features.exclude_patterns` en [`config/model.yaml`](../config/model.yaml).

## Datos sensibles

El CSV incluye `nombre` (razón social del restaurante). El pipeline lo excluye
explícitamente de las features del modelo (ver `DROP_FROM_FEATURES` en
[`src/churn/data/schema.py`](../src/churn/data/schema.py)) y sólo lo usa para mostrarlo
en el dashboard.

---

## Intercom (`data/intercom/`)

Export de tickets de soporte, evaluado como fuente adicional de features. El análisis está
en [`notebooks/02_eda_intercom.ipynb`](../notebooks/02_eda_intercom.ipynb).

**Estado: no integrado todavía.** La señal existe y es buena — hay tickets con motivo
`"Solicitar la baja de mi cuenta"` — pero el export disponible no alcanza:

- Está **truncado en 20.000 registros** (límite de paginación de la API).
- Los tickets de **soporte no registran el `ID de dash`** (0% en los de baja, contra 100%
  en los de integraciones). Se recupera parte cruzando por el `external_id` de los
  contactos y por `Company ID` — la cobertura sube de 22% a 44% — pero los mapas se
  derivan del propio archivo truncado y arrastran su límite.
- La ventana temporal (2025-07 en adelante) casi no se superpone con los períodos que
  tienen etiqueta de churn: sólo **19 tickets** caen dentro.

### Qué pedir en el re-export

1. Sin límite de registros, **desde 2024-01**.
2. La **tabla de usuarios de Fudo** (`user_id` → `account_id`). El `external_id` de los
   contactos es ese id de usuario y está en el **92%** de los tickets, contra el 22% del
   atributo `ID de dash`: es la vía más directa para atribuir los tickets de soporte.
   Como alternativa, la tabla de Companies de Intercom cubre el 63%.
3. **Sin la columna `Ticket Parts`** — las conversaciones son el 99% del peso (1 GB para
   20.000 tickets) y el modelo no las necesita.

Campos necesarios: `Ticket ID`, `Created At`, `Updated At`, `Company ID`, `Ticket Type`,
`Ticket State`, `Category`, `Channel`, y de `Ticket Attributes`: `ID de dash`, `Tema`,
`Motivo`, `Submotivo`, `Necesita derivación`.

### La tabla de companies

Intercom distingue dos identificadores de company, y la diferencia importa:

| Campo | Qué es | Dónde aparece |
|---|---|---|
| `id` | ObjectId interno de Intercom, 24 caracteres hex | es el `Company ID` de los tickets |
| `company_id` | el identificador **externo**, que define la empresa | acá debería estar el ID de Fudo |

O sea que el `Company ID` que traen los tickets **no es** el ID de Fudo: hace falta la
tabla de companies para traducirlo. En la API de Intercom sale de
`GET /companies` o del scroll (`GET /companies/scroll`), y si el ID de Fudo no está en
`company_id` hay que buscarlo entre los `custom_attributes`.

### Verificar un export antes de invertir tiempo en él

```bash
churn intercom-check --companies data/intercom/companies.csv
```

Reporta cuántos tickets se pueden atribuir a una cuenta y por qué vía, y —lo que decide
si sirve— cuántos caen dentro de períodos que tienen etiqueta de churn. La lógica de
atribución está en [`src/churn/data/intercom.py`](../src/churn/data/intercom.py), con las
cuatro vías ordenadas por confiabilidad.
