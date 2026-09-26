# Predictor de Churn — Fudo

Modelo de predicción de bajas para las cuentas (restaurantes) del SaaS **Fudo**, con
scoring batch mensual, explicabilidad por cuenta y una capa de aplicación para el equipo
de Customer Experience.

Proyecto final de la materia de **MLOps (ITBA)**.
Dankiewicz Nicolás · Zaffar Camila · Sánchez María Clara

---

## Qué hace

Cada mes, sobre el snapshot de todas las cuentas activas, el sistema:

1. **Infiere la etiqueta de churn** — no viene en los datos: se deriva de que una cuenta
   deje de aparecer en el snapshot del mes siguiente.
2. **Predice la probabilidad de baja** de cada cuenta activa, calibrada.
3. **La convierte en riesgo económico** — `probabilidad × revenue mensual` — y asigna una
   categoría: **alto / medio / bajo / no churn**.
4. **Explica cada predicción** con las features que más pesaron, su valor y el percentil
   de esa cuenta contra el resto de la base, más un aviso si la cuenta tiene patrón de
   negocio de temporada.
5. **Describe la base contra la que se lee todo eso** — cuánto crece, cuánto se va por mes,
   qué parte del producto usa una cuenta promedio y qué se apaga antes de una baja.
6. **Deja todo escrito** para que la API lo sirva y el dashboard lo muestre.

### Resultados del modelo actual

Entrenado sobre el panel 202501–202608 (599.967 filas, 53.160 cuentas) y evaluado sobre
los dos meses más recientes con etiqueta confirmada, sin haberlos visto durante el
entrenamiento:

| Métrica | Test (202604–202605) | Referencia |
|---|---|---|
| **PR-AUC** | **0.667** | umbral acordado con negocio: 0.20 |
| ROC-AUC | 0.955 | |
| Churn base mensual | 3.75 % | el lift es de **17.8×** |
| Precisión en el top 5 % de cuentas | 52.9 % | recall 70.4 % |
| Precisión en el top 1 % de cuentas | 93.0 % | recall 24.8 % |

Leído en términos de operación: **si CX trabaja las 3.100 cuentas más riesgosas (top 5 %),
más de la mitad efectivamente se iba a dar de baja, y ese grupo contiene 7 de cada 10 bajas
del mes.**

> El PR-AUC oscila en el tercer decimal según la máquina: `tree_method: hist` con
> `n_jobs: -1` no es bit-a-bit determinista al cambiar la cantidad de cores. Fijar
> `n_jobs: 1` en `config/model.yaml` lo hace reproducible exacto, a costa de velocidad.

Un dato que vale la pena mirar: buena parte de la señal viene del **estado de cobranza**
(`PENDING_PAYMENT`, `BLOCKING_ALERT`, `BLOCKED`), que es un predictor tardío — cuando una
cuenta ya está bloqueada, la baja prácticamente ya ocurrió. Entrenando **sin ninguna
feature de cobranza**, el PR-AUC baja a **0.317** (8.4× el baseline), todavía por encima
del umbral de negocio. Es decir: hay señal real y anticipada en el comportamiento de uso
del producto, no sólo en la mora.

```bash
# Para reproducir esa variante:
# en config/model.yaml -> features.exclude_patterns: ["cat_gastos", "estado", "deuda"]
make train
```

#### Más datos no mejoran el modelo

Vale la pena dejarlo anotado porque era la hipótesis al reemplazar el dataset. El modelo
anterior estaba entrenado con el panel 202401–202508; el nuevo, con 202501–202608, ocho
meses más fresco. Evaluados **sobre el mismo test**, empatan:

| Métrica (test 202604–202605) | Modelo nuevo | Modelo anterior |
|---|---|---|
| PR-AUC | 0.6674 | 0.6681 |
| ROC-AUC | 0.9550 | 0.9582 |

La curva de aprendizaje explica por qué. Fijando val y test y recortando sólo cuánta
historia entra a train:

| Períodos de train | Filas | PR-AUC (val) |
|---|---|---|
| 3 | 88.325 | 0.6523 |
| 7 | 197.889 | 0.6544 |
| 12 | 326.691 | 0.6585 |

**Cuadruplicar los datos mueve el PR-AUC 0.006.** El techo no está en el volumen de
historia sino en la señal disponible, así que ni recuperar 2024 ni esperar más meses va a
cambiar el número. Lo que sí movió la aguja fue mirar *qué* features entran: ver
`features.exclude_patterns` en [`config/model.yaml`](config/model.yaml).

El modelo nuevo se promovió igual, por tres razones que no son la métrica: está entrenado
con datos ocho meses más recientes, el dataset del anterior ya no existe en `data/` (no era
reproducible ni auditable), y su drift contra el mes corriente era enorme — 43 features
con PSI por encima de 0.25. Queda registrado en `registry.json`, con rollback disponible
vía `churn promote --force`.

---

## Arranque rápido

### Con Docker (recomendado)

```bash
# 1. Poner el snapshot en data/ (ver data/README.md)
# 2. Pipeline completo: features -> modelo -> predicciones
make docker-all

# 3. Levantar API y dashboard
make up
```

- Dashboard → <http://localhost:8080>
- API + docs interactivas → <http://localhost:8000/docs>

Si los puertos 8000 u 8080 ya están ocupados, `make up` te avisa antes de intentar
levantar nada y te dice cómo cambiarlos:

```bash
make up API_PORT=8001 WEB_PORT=8081        # solo esta vez
printf 'API_PORT=8001\nWEB_PORT=8081\n' >> .env   # permanente, el .env no se versiona
```

### Con Poetry, sin Docker

```bash
make setup            # instala dependencias
make all              # prepare + train + score
make api              # API en :8000
make web              # dashboard en :8080 (en otra terminal)
```

`make help` lista todos los comandos disponibles.

### Sin `make` (Windows)

`make` es sólo un atajo: cada target es un `docker compose` que se puede escribir a mano
desde PowerShell, cmd o Git Bash. Los comandos son idénticos en los tres.

```powershell
# 1. Construir las imagenes (la primera vez, y cada vez que cambie el codigo)
docker compose --profile jobs build

# 2. Pipeline completo: features -> modelo -> predicciones
docker compose --profile jobs run --rm pipeline run-all

# 3. Levantar API y dashboard
docker compose up -d api web
```

Equivalencias de los comandos que se usan a diario:

| Con `make` | Sin `make` |
|---|---|
| `make build` | `docker compose --profile jobs build` |
| `make docker-all` | `docker compose --profile jobs run --rm pipeline run-all` |
| `make docker-prepare` | `docker compose --profile jobs run --rm pipeline prepare` |
| `make docker-train` | `docker compose --profile jobs run --rm pipeline train` |
| `make docker-score` | `docker compose --profile jobs run --rm pipeline score` |
| `make docker-eda` | `docker compose --profile jobs run --rm pipeline eda` |
| `make docker-seasonality` | `docker compose --profile jobs run --rm pipeline seasonality` |
| `make pricing-template` | `docker compose --profile jobs run --rm pipeline pricing-template` |
| `make up` | `docker compose up -d api web` |
| `make down` | `docker compose down` |
| `make logs` | `docker compose logs -f api web` |
| `make gcp-docker-build` | `docker build -f docker/Dockerfile.api -t REGION-docker.pkg.dev/PROYECTO/churn/api:SHA .` |
| `make gcp-docker-push` | `docker push REGION-docker.pkg.dev/PROYECTO/churn/api:SHA` |
| `make gcp-docker-deploy` | `bash deploy/cloudrun.sh app REGION-docker.pkg.dev/PROYECTO/churn/api:SHA` |
| `make gcp-run-build` | `docker build --platform linux/amd64 -f docker/Dockerfile.api -t REGION-docker.pkg.dev/PROYECTO/churn/api:SHA .` |
| `make gcp-run-push` | `docker push REGION-docker.pkg.dev/PROYECTO/churn/api:SHA` |
| `make gcp-run-deploy` | `gcloud run deploy churn-demo --image REGION-docker.pkg.dev/PROYECTO/churn/api:SHA --allow-unauthenticated …` |

Cualquier comando de la CLI va después de `pipeline`, con sus flags:

```powershell
docker compose --profile jobs run --rm pipeline score --pricing config/pricing.example.yaml
docker compose --profile jobs run --rm pipeline --help
```

**Puertos.** Sin `make` no corre el chequeo previo, así que si el 8000 u 8080 están
ocupados vas a ver `Bind for 0.0.0.0:8000 failed: port is already allocated`. Se arregla
creando un archivo `.env` en la raíz — `docker compose` lo lee solo:

```
API_PORT=8001
WEB_PORT=8081
```

**Memoria.** El pipeline necesita 4 GB. En Windows, Docker Desktop con WSL2 toma por
defecto hasta la mitad de la RAM del equipo; si el contenedor muere con **exit 137**, es
eso. Se sube creando `C:\Users\<usuario>\.wslconfig`:

```ini
[wsl2]
memory=8GB
```

y reiniciando con `wsl --shutdown`.

**Fin de línea.** El repo trae un `.gitattributes` que fuerza LF en los scripts. Sin él,
Git for Windows los convierte a CRLF al clonar y el contenedor del dashboard falla con
`40-churn-config.sh: not found` — sobre un archivo que existe — dejando el dashboard
apuntado al puerto equivocado. Si clonaste antes de que existiera ese archivo:

```powershell
git rm --cached -r .
git reset --hard
```

### Cuánto tarda

Medido de punta a punta sobre el dataset completo (599.967 filas, 53.160 cuentas,
20 períodos), en un MacBook M4 Pro de 14 núcleos:

| Comando | Qué hace | Local | Docker |
|---|---|---|---|
| `make all` / `make docker-all` | CSV → features → modelo → predicciones | **~59 s** | **~48 s** |
| `make prepare` | lee los 20 CSV y construye las 296 features | ~17 s | ~25 s |
| `make train` | entrena y evalúa (features cacheadas) | ~50 s | ~22 s |
| `make score` | predice + SHAP sobre 33.517 cuentas, y deja la estadística | ~15 s | ~15 s |

En resumen: **el pipeline completo tarda menos de un minuto**. Iterar sobre el modelo
cuesta ~30 s, porque `prepare` cachea las features en `outputs/interim/` y sólo se
recalculan con `--force`.

Los tiempos varían con la cantidad de núcleos: `train` es la etapa que más paraleliza
(XGBoost con `n_jobs: -1`) y por eso el número local, con más núcleos disponibles, no
siempre le gana al del contenedor.

### Actualizar con un mes nuevo

Cuando llega el snapshot de un mes, alcanza con **dejar el archivo en `data/` y volver a
scorear**:

```bash
cp account-stats-202609.csv data/
make score
```

`data.raw_path` acepta un archivo, un directorio o un patrón, así que no hace falta
concatenar nada a mano ni tocar la configuración. Si un mismo (cuenta, período) aparece
en dos archivos gana el del archivo que ordene último por nombre, lo que permite corregir
un mes ya cargado sin borrar el anterior.

`prepare` compara la fecha del snapshot contra las features cacheadas y las recalcula solo
cuando hace falta — no hay que acordarse de `--force`.

**Reentrenar no hace falta todos los meses.** El modelo entrenado hasta 202601 puntúa datos
nuevos sin problema; tiene sentido correr `make train` cada tres meses o cuando `make drift`
muestre features corridas. Y como cada batch queda en su propia carpeta
(`outputs/predictions/YYYYMM/`), el selector de período del dashboard te deja comparar
meses.

> Si el export trae **solo el mes nuevo**, el histórico tiene que seguir en `data/`: el
> feature engineering necesita los tres meses previos de cada cuenta para calcular deltas
> y tendencias. Con un archivo suelto de un mes, todas esas features quedarían vacías.

### Requisitos

- **Python 3.11 o 3.12** (el proyecto usa Poetry; `make setup` instala el resto).
- **4 GB de RAM disponibles** para el pipeline. El paso pesado es el feature engineering
  sobre las 600k filas del panel. El `docker-compose.yml` declara `mem_limit: 4g`; con
  menos, el contenedor muere por OOM (exit 137) sin mensaje propio. Se puede subir con
  `PIPELINE_MEM=6g make docker-all`.
- La API y el dashboard son livianos: 1 GB y 256 MB alcanzan de sobra.

---

## Estadística de la base

El tablero de riesgo responde *a quién llamo*. La pestaña **Uso de la base** responde la
otra mitad: *contra qué base estoy leyendo eso*. La genera `churn eda` — que corre solo
junto con cada scoring — y la sirve `GET /api/v1/eda`.

```bash
make eda        # -> outputs/eda/stats.json
```

Lo que sale del panel 202501–202608:

| | |
|---|---|
| Cuentas activas | 26.256 → **33.517** (+27,6 % en 20 meses, 1,29 % mensual compuesto) |
| **Churn base** | **3,72 % mensual** (entre 3,27 % y 4,36 %) |
| Retención anual | 63,5 % · vida media de una cuenta: **27 meses** |
| Altas / bajas confirmadas | 26.904 / 18.521 |
| Cuentas con pausas | 2.888 (5,4 %), de las cuales 729 son estacionales |

**El churn es un problema de los primeros meses.** Una cuenta de menos de 3 meses se da de
baja 5,7 veces más seguido que una de más de 4 años:

| Antigüedad | 0-3 m | 3-6 m | 6-12 m | 1-2 a | 2-4 a | 4+ a |
|---|---|---|---|---|---|---|
| Churn mensual | **8,59 %** | 5,20 % | 3,93 % | 2,79 % | 2,05 % | **1,51 %** |

**Qué usa una cuenta de Fudo**, sobre las 33.517 activas del último mes. Casi todas
configuran el salón (mesas y salas, 98 %) y cargan desde PC (89 %), pero la profundidad
cae rápido: recetas 51 %, facturación fiscal 34 %, menú online habilitado 25 %, apps de
delivery menos del 10 % cada una. La carga desde mobile es marginal (0,07 %).

**Qué se apaga antes de una baja.** Comparando la adopción de cada funcionalidad el mes
previo, entre cuentas que se fueron y cuentas que se quedaron:

| Funcionalidad | Se queda | Se da de baja | Lift |
|---|---|---|---|
| Comprobantes fiscales | 31,2 % | 10,2 % | **0,33** |
| Movimientos de caja | 32,1 % | 12,6 % | **0,39** |
| Gastos registrados | 37,7 % | 16,0 % | **0,42** |
| Arqueos de caja | 64,5 % | 36,0 % | 0,56 |
| Ventas en el mes | 95,5 % | 68,8 % | 0,72 |

Es descriptivo, no causal: una cuenta que ya dejó de operar deja de usar todo a la vez. Lo
que sí dice es **en qué orden se apagan las cosas** — la operación administrativa (fiscal,
caja, gastos) se abandona mucho antes que la venta misma, que sigue en el 69 % de las
cuentas el mes previo a la baja.

---

## Cómo se infiere el churn

Esta es la decisión de diseño central del proyecto, porque **la etiqueta no existe en los
datos**. El reporte mensual sólo incluye cuentas con estado comercial `ACTIVE` al momento
de generarse, así que la desaparición de una cuenta *es* la señal de baja.

El problema es que **el 15 % de las desapariciones son temporales**: la cuenta vuelve
uno o dos meses después. Son restaurantes estacionales o pausas, no bajas. Etiquetarlas
como churn le enseña ruido al modelo.

Por eso se exige una **ventana de confirmación** (`labeling.confirm_window`, 3 meses por
defecto):

| Situación | Etiqueta |
|---|---|
| Presente en `t` y en `t+1` | `0` — no churn |
| Presente en `t`, ausente en `t+1 … t+3` | `1` — churn |
| Ausente en `t+1` pero vuelve dentro de la ventana | ambigua → se descarta del entrenamiento |
| Últimos 3 meses del panel | no evaluable — todavía no hay futuro observado |

El churn rate resultante es **3,72 % mensual**, y las filas ambiguas que se descartan son
2.099 (0,3 % del panel).

Ver [`src/churn/data/labeling.py`](src/churn/data/labeling.py).

---

## Cuentas estacionales

El ML Canvas pide excluir del entrenamiento las cuentas con "altas y bajas recurrentes".
Están identificadas, y el reporte completo sale con:

```bash
make seasonality
```

### Qué muestran los datos

**5,4 % de las cuentas (2.888) pausó alguna vez**, pero solo **447 lo hicieron dos o más
veces**. Haber pausado una vez multiplica por **2,8** la probabilidad de volver a pausar.

El ciclo anual tiene una firma nítida. El índice compara contra lo esperable si el evento
fuera uniforme, corrigiendo por cuántas veces cada mes calendario entra en la ventana
observada (ver el recuadro más abajo):

| Evento | Meses pico | Índice |
|---|---|---|
| Inicio de pausa | mayo, octubre, enero | 1,18 · 1,14 · 1,11 |
| Reactivación | diciembre, octubre, noviembre | **1,68** · 1,36 · 1,26 |
| Alta nueva | enero, octubre, diciembre | 1,11 · 1,07 · 1,07 |
| Baja definitiva | abril, mayo, junio | 1,15 · 1,08 · 1,07 |

Las pausas arrancan en otoño y las reaperturas se concentran con fuerza en
primavera-verano: es el **invierno del hemisferio sur**. Las bajas definitivas, en cambio,
son casi planas (0,84 a 1,15): **no son un fenómeno de calendario**, que es exactamente la
distinción que justifica la ventana de confirmación del etiquetado.

> **Cuidado con este índice.** Compararlo contra un doceavo fijo sólo es válido si el
> panel cubre una cantidad entera de años. Con 20 meses (202501–202608), febrero a junio
> entran dos veces en la ventana y septiembre a diciembre una sola: medido así, las bajas
> definitivas mostraban picos de 1,63 en abril-junio que eran **la forma del panel, no el
> negocio**. `monthly_profile` normaliza por la cobertura de cada mes calendario.

El patrón de pausas es geográfico, no del producto — Argentina y Chile concentran 46 % y
45 % de sus pausas en abril-julio:

| País | Cuentas | Pausan | Pausas en abril-julio |
|---|---|---|---|
| Colombia | 5.482 | 5,62 % | 40,6 % |
| Chile | 15.874 | 5,59 % | 45,0 % |
| Argentina | 20.216 | 5,51 % | 46,0 % |
| Brasil | 3.472 | 5,39 % | 42,8 % |
| México | 7.610 | 4,65 % | 43,3 % |

(Si fuera uniforme, 33 % de las pausas caerían en abril-julio.)

### Dos fenómenos distintos

El detector marca **729 cuentas (1,4 % de la base)** y las separa en dos tipos, porque para
negocio no son lo mismo:

- **302 de temporada** — cierran media temporada y reabren. Baja en abril-mayo, alta en
  diciembre-febrero, tres meses o más. El listado se valida solo: *Balneario Atlántico*,
  *Nelson Beach*, *restobar entre mar*, *fuegos patagonia lodge*.
- **427 intermitentes** — pausas cortas y repetidas sin patrón de calendario. No son
  negocios de temporada: suele ser uso errático o idas y vueltas de cobranza.

> El detector ve menos cuentas que antes (729 contra 1.117) simplemente porque el panel es
> más corto: con 20 meses en vez de 32 hay menos espacio para que un ciclo completo de
> pausa y retorno entre en la ventana. No es que haya menos negocios de temporada.

### Excluirlas del entrenamiento: se midió y no aporta

Está implementado (`seasonality.exclude_from_training`) pero viene **desactivado**, porque
el A/B sobre el mismo test set dice que no sirve:

| | PR-AUC (test) |
|---|---|
| Sin exclusión (el del pipeline) | **0,6674** |
| Excluyendo las estacionales de train y val | 0,6647 |

La razón: son 729 cuentas sobre 53.160, y la mayoría de sus filas son meses de operación
perfectamente normal — información válida que la exclusión tira. El ruido que se quería
sacar, las filas donde la cuenta pausa, **ya lo neutraliza la ventana de confirmación del
etiquetado**, que las marca ambiguas y las descarta. La exclusión es redundante con algo
que el pipeline ya hacía.

> Cuando se excluye, se saca de train y val pero **nunca de test**: en producción esas
> cuentas se scorean igual, así que evaluarlas fuera daría una métrica más linda que la
> realidad.

### Dónde sí sirve detectarlas

En el **scoring**, como contexto para CX. Cada predicción viene con `pausas_historicas`,
`mes_de_pausa_habitual` y `posible_estacional`, calculados **solo con el pasado de la
cuenta** — la versión causal de la señal, porque en producción no se sabe el futuro.

En el batch de agosto 2026, **282 cuentas** salen marcadas y **38 de ellas están señaladas
con riesgo de baja**. El dashboard las muestra con un aviso:

> **Posible negocio de temporada.** Esta cuenta ya se dio de baja y volvió 2 veces. Suele
> pausar en mayo. Su desaparición puede ser un cierre estacional y no una pérdida: conviene
> confirmarlo antes de gastar una acción de retención.

Y se pueden filtrar, en el dashboard o vía `GET /api/v1/accounts?seasonal=true`.

### ¿Se puede predecir la estacionalidad?

Sí, pero con señal moderada. Se entrenó un modelo específico para responder *"esta cuenta
que está por desaparecer, ¿vuelve o se va para siempre?"*. Sobre el panel 202401–202508
(22.852 desapariciones, 19 % de ellas pausas) daba PR-AUC **0,389** (lift 2,2×) y ROC-AUC
**0,724**: separa razonablemente, pero no es determinante.

> Ese experimento vivía fuera del pipeline y **no se rehizo sobre el panel actual**, donde
> las desapariciones son 23.108 y el 15 % resulta ser pausa. Los dos números de arriba son
> del panel anterior; lo que sigue sí está remedido.

El predictor que más pesa es **`pausas_previas`** — el historial de la cuenta — muy por
encima de cualquier señal de comportamiento, lo cual es consistente con el **2,8×** de
reincidencia. Dicho de otro modo: la mejor forma de saber si una cuenta va a pausar es
mirar si ya pausó antes, que es exactamente lo que expone el flag del scoring.

Un matiz que importa: solo el **3 %** de las cuentas con pausas recurrentes pausa siempre
en el mismo mes del año (8 % con ±1 mes). La estacionalidad es un patrón agregado sólido,
pero a nivel de cuenta individual no es un reloj.

---

## Cómo se construyen las features

El valor absoluto de una métrica dice poco: un bar de barrio factura menos que una cadena
y ninguno de los dos se está yendo por eso. Lo que anticipa una baja es el **movimiento**.

Sobre las 82 métricas del snapshot (más las derivadas de negocio) se calculan, para cada
cuenta y mes:

| Feature | Qué captura |
|---|---|
| `<col>_delta_1m` | variación contra el mes anterior |
| `<col>_ratio_3m` | nivel actual sobre el promedio de los 3 meses previos |
| `<col>_delta_3m` | variación contra hace 3 meses (métricas núcleo) |
| `<col>_slope_3m` | pendiente de la tendencia reciente (métricas núcleo) |

Más features de negocio construidas a mano: ventas totales y mix de canales, amplitud de
uso del producto, ratios de configuración (¿cuántos productos tienen costo cargado?),
antigüedad, pausas previas, severidad y racha del estado de cobranza, y la descomposición
del plan en plan base + módulos + mercado, con detección de downgrades.

Total: **296 features** construidas, de las que **284 entran al modelo** — las 12 de
categorías de gasto quedan afuera por `features.exclude_patterns` (ver
[`config/model.yaml`](config/model.yaml)).

> Los lags se calculan con un *join* sobre `(id, t - lag)` y **no** con `groupby().shift()`:
> hay cuentas con huecos en el panel y un shift ciego tomaría el mes equivocado.

Ver [`src/churn/features/builder.py`](src/churn/features/builder.py).

---

## Modelo

**XGBoost** sobre gradient boosted trees. Es la elección adecuada para este dataset:
tabular, mixto (numéricas + categóricas), con muchos *missings* estructurales (los canales
de delivery que la cuenta no usa vienen vacíos) y una clase positiva del ~4 %. Los árboles
manejan los NaN de forma nativa y no requieren escalado ni imputación.

Tres decisiones que importan:

- **Split temporal estricto.** Se entrena con meses viejos y se evalúa con los recientes,
  igual que en producción. Un split aleatorio filtraría el futuro: la misma cuenta aparece
  en muchos meses con features correlacionadas.
- **Desbalance vía `scale_pos_weight`**, no resampleo.
- **Calibración isotónica** sobre validación. Sin esto las probabilidades quedan infladas
  (un 0.6 que en realidad significa 0.15), y como el riesgo económico es
  `probabilidad × revenue`, una probabilidad mal calibrada se traduce directo en plata
  mal priorizada.

La métrica de decisión es **PR-AUC**: con un churn base de ~4 %, el accuracy y el ROC-AUC
son engañosos.

---

## Precios y riesgo económico

El revenue **no entra al modelo** — el modelo sólo estima probabilidad de churn. Se usa
después, para convertir esa probabilidad en riesgo económico.

Un plan code se descompone en plan base + módulos + mercado:

```
adv-tbl-mx  →  plan Avanzado (adv) + módulo Mesas (tbl), facturado en México (mx)
revenue     =  plans.adv.mx + modules.tbl.mx
```

Los precios se cargan **a mano** en `config/pricing.yaml`:

```bash
make pricing-template   # genera el YAML con TODOS los planes y módulos vistos en los datos
make pricing-check      # muestra qué falta completar y a cuántas cuentas afecta
```

Son **7 planes base × 8 módulos × 7 mercados = 82 valores** a completar. La plantilla trae
cada código anotado con su nombre legible. Regenerarla conserva los valores ya cargados.

Hay un `config/pricing.example.yaml` con **precios inventados**, sólo para poder ver el
sistema funcionando de punta a punta antes de tener la lista real:

```bash
make score-demo
```

Categorías de riesgo (cortes en `config/model.yaml`, a definir con negocio):

| Categoría | Condición |
|---|---|
| `no_churn` | probabilidad por debajo del umbral de decisión |
| `bajo` | señalada, pero poco revenue en juego |
| `medio` | ≥ 15 USD/mes en riesgo |
| `alto` | ≥ 40 USD/mes en riesgo |

---

## Explicabilidad

Cada predicción viene con las 8 features que más pesaron **en esa cuenta**, calculadas con
**SHAP** (`TreeExplainer`), cada una con:

- su aporte al riesgo, con signo (`aumenta` / `reduce`),
- el valor de la cuenta en esa feature,
- el **percentil** de la cuenta contra el resto de la base del mes — es lo que alimenta el
  mapa de calor del dashboard.

Y un **diagnóstico en texto** listo para leer:

> Riesgo alto de baja: probabilidad estimada de 92%. Representa 170 USD/mes, con 157
> USD/mes en riesgo. Lo que más empuja el riesgo: cambio en el estado de cobranza vs mes
> anterior (muy por encima del resto de la base, percentil 93); adiciones totales del mes
> (muy por debajo del resto de la base, percentil 3); meses consecutivos fuera de estado
> active (por encima del resto de la base, percentil 87).

Ese texto se arma con plantillas sobre las contribuciones SHAP: es determinista, gratis y
no puede alucinar. Para las cuentas de riesgo alto hay un modo alternativo que se lo pasa
a **Claude** para que redacte un párrafo más natural y sugiera una acción concreta:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
make score-llm
```

En los dos casos el insumo son las mismas contribuciones SHAP: el texto nunca inventa una
causa que el modelo no haya usado.

---

## Arquitectura

```
                                    ┌──────────────────────┐
   data/*.csv  ──────────────────►  │   Pipeline (batch)   │
   (snapshot mensual del DW)        │  prepare → train →   │
                                    │    score → eda       │
                                    └──────────┬───────────┘
                                               │ escribe
                                               ▼
                                    outputs/predictions/YYYYMM/
                                      predictions.parquet
                                      predictions.json
                                      metadata.json
                                    outputs/eda/
                                      stats.json
                                               │ lee
                                               ▼
                                    ┌──────────────────────┐
                                    │   API (FastAPI)      │  solo lectura
                                    └──────────┬───────────┘
                                               │ HTTP
                                               ▼
                                    ┌──────────────────────┐
                                    │  Dashboard (web)     │
                                    └──────────────────────┘
```

El modelo corre **en batch**; la API sólo lee lo que el batch dejó escrito. Esa separación
es lo que permite que la capa de aplicación sea liviana (no carga XGBoost, arranca en menos
de un segundo) y barata de hostear escalando a cero.

### Estructura del repositorio

```
├── config/
│   ├── model.yaml              configuración de todo el pipeline
│   ├── pricing.yaml            lista de precios (a completar a mano, no versionada)
│   └── pricing.example.yaml    precios de ejemplo para el demo
├── src/churn/
│   ├── data/                   carga, etiqueta de churn y deteccion de estacionalidad
│   ├── eda/                    estadistica descriptiva de la base (catalogo + reporte)
│   ├── features/               feature engineering y nombres legibles
│   ├── models/                 split temporal, entrenamiento, evaluación, artefacto
│   ├── pricing/                parseo de plan codes y revenue
│   ├── explain/                SHAP y generación de diagnósticos
│   ├── scoring/                job batch y categorización de riesgo
│   ├── pipeline.py             orquestación de las etapas
│   └── cli.py                  interfaz de línea de comandos
├── api/app/                    API FastAPI de solo lectura
├── web/                        dashboard (HTML/CSS/JS, sin dependencias externas)
├── docker/                     Dockerfiles de las tres piezas
├── deploy/                     puesta a punto de GCP, despliegue en Cloud Run y guía
├── .github/workflows/          CI y despliegue continuo con champion / challenger
├── notebooks/                  EDA que respalda las decisiones de diseño
├── tests/
└── Makefile
```

---

## API

| Endpoint | Qué devuelve |
|---|---|
| `GET /health` | estado y períodos disponibles |
| `GET /api/v1/me` | cuenta con la que se inició sesión (solo en GCP, detrás de IAP) |
| `GET /api/v1/periodos` | períodos con predicciones generadas |
| `GET /api/v1/model` | metadatos y métricas offline del modelo |
| `GET /api/v1/eda` | estadística de la base: salud, adopción del producto y señales de churn |
| `GET /api/v1/summary` | cuentas y revenue en riesgo por categoría |
| `GET /api/v1/accounts` | listado paginado, con filtros por riesgo, país, estacionalidad y búsqueda |
| `GET /api/v1/accounts/{id}` | detalle con las contribuciones SHAP y el diagnóstico |
| `GET /api/v1/importance` | importancia global de las features en el lote |
| `GET /api/v1/distribution` | histograma de probabilidades |

Documentación interactiva en `/docs`.

---

## Despliegue en GCP

El despliegue es automático: cada push a `main` despliega solo la pieza que cambió.

| Pieza | Servicio | Por qué |
|---|---|---|
| Entrenamiento + champion / challenger | **Cloud Run Job** | corre unos minutos y se apaga |
| Scoring mensual | **Cloud Run Job** + Cloud Scheduler | fijado a la imagen del modelo campeón |
| API + dashboard | **Cloud Run Service** + Identity-Aware Proxy | solo usuarios autorizados; escala a cero |
| Datos, modelos y predicciones | **Cloud Storage** | montado como disco en los contenedores |
| CI/CD | **GitHub Actions** | sin claves: Workload Identity Federation |

**La app no es pública.** Dashboard, API y `/docs` están detrás de Identity-Aware Proxy:
hay que iniciar sesión con una cuenta de Google que esté en la lista de acceso, que se
administra con `make gcp-grant` / `make gcp-revoke` (personas, grupos o un dominio entero).
La API sirve también el dashboard, para que compartan el origen y la sesión.

Si el push cambia el modelo (`src/`, `config/model.yaml`, dependencias), CI lo reentrena
en Cloud Run y lo compara contra el modelo en producción **sobre el mismo test**. Solo si
supera el umbral de negocio y le gana por un margen mínimo se promueve y se regeneran las
predicciones; si no, queda guardado como candidato. El mismo gate corre localmente:

```bash
churn retrain --candidate-dir models/candidates/prueba --champion-dir models --use-cache
```

El código no distingue local de nube: en Cloud Run el bucket se monta como un directorio,
y [`config/gcp.yaml`](config/gcp.yaml) hereda todo de `model.yaml` y solo cambia las rutas.
Costo estimado: menos de USD 1 por mes.

Guía completa, con la puesta a punto y la operación: [`deploy/README.md`](deploy/README.md).

```bash
make gcp-bootstrap GCP_PROJECT=tu-proyecto                      # una vez
make gcp-grant GCP_PROJECT=tu-proyecto MEMBER=user:ana@fu.do    # dar acceso
make gcp-url GCP_PROJECT=tu-proyecto                            # URL de la app
```

El mismo recorrido, pero construyendo las imágenes con Docker en tu máquina en vez de
con Cloud Build —un comando por paso, como en la clase 6— y desplegando por el mismo
`deploy/cloudrun.sh` que usa CI:

```bash
make gcp-docker-auth                               # una vez por máquina
make gcp-docker-release GCP_PROJECT=tu-proyecto    # build + push + deploy
```

Y una variante más corta, la de la clase 6: construir con Docker, publicar en el registry
y desplegar en Cloud Run con una URL **pública, sin login**, sin depender del bootstrap ni
de GitHub Actions. Un comando por paso:

```bash
make gcp-run-repo   GCP_PROJECT=tu-proyecto    # una vez: repositorio de imágenes
make gcp-run-sa     GCP_PROJECT=tu-proyecto    # una vez: cuenta de servicio del servicio
make gcp-run-build  GCP_PROJECT=tu-proyecto    # docker build
make gcp-run-push   GCP_PROJECT=tu-proyecto    # docker push
make gcp-run-data   GCP_PROJECT=tu-proyecto    # sube las predicciones al bucket
make gcp-run-deploy GCP_PROJECT=tu-proyecto    # gcloud run deploy
make gcp-run-delete GCP_PROJECT=tu-proyecto    # dar de baja el servicio
```

El link no pide login y muestra datos de cuentas reales: compartirlo con criterio y darlo
de baja al terminar. Detalle de cada paso en
[`deploy/README.md`](deploy/README.md#compartir-el-dashboard-con-un-link-público).

---

## Desarrollo

```bash
make test     # 124 tests
make lint     # ruff
make fmt      # formateo automático
```

Los tests cubren lo que más fácil se rompe en silencio: el parseo de plan codes, la
inferencia de la etiqueta (incluyendo pausas y huecos del panel), la detección de
episodios de ausencia, el índice del ciclo anual (que con un panel de 20 meses llegó a
mostrar picos que eran la forma del panel), el cálculo de revenue, la categorización de
riesgo, el feature engineering temporal y los conteos del reporte de la base.

---

## Estado y próximos pasos

**Listo:** etiquetado, features, modelo entrenado y evaluado por encima del umbral de
negocio, scoring batch con explicabilidad, estadística de la base, API, dashboard, Docker
y despliegue a GCP.

**Pendiente:**

- Cargar la lista de precios real en `config/pricing.yaml` (hoy sólo hay un ejemplo).
- **Revisar la exclusión de `cat_gastos`** cuando la adopción de categorías de gasto se
  estabilice. Hoy está afuera porque crece con el calendario y funciona como un reloj
  (PSI 11,34); cuando se aplane deja de ser un riesgo y pasa a ser una señal de uso más.
  Es una línea en `features.exclude_patterns` y un `make train`.
- **Subir el techo del modelo.** La curva de aprendizaje es plana, así que no va a venir
  de más datos del mismo snapshot: tiene que venir de otra fuente (soporte, interacciones
  comerciales) o de un etiquetado más fino.
- Validar con CX el listado de cuentas de temporada: hoy se infiere del patrón de pausas,
  pero el dato duro (rubro, ubicación, estacionalidad declarada) está en el CRM.
- Definir con negocio los cortes de riesgo alto/medio/bajo en revenue.
- Incorporar las fuentes que el canvas menciona y todavía no están: tickets de soporte e
  interacciones comerciales. La hipótesis es que aportan señal **temprana**, que es justo
  donde el modelo actual es más débil. El export de Intercom ya está evaluado
  ([`notebooks/02_eda_intercom.ipynb`](notebooks/02_eda_intercom.ipynb)): la señal existe
  pero el archivo está truncado y los tickets de soporte no registran el ID de cuenta;
  ahí está la especificación de qué re-exportar.
- Monitoreo en producción: comparar el grupo predicho como no-churn contra las cuentas
  efectivamente targeteadas, y medir el revenue perdido por abandono.
- Reentrenamiento automático y detección de deriva.

---

## Documentación de referencia

- [`notebooks/01_eda_churn.ipynb`](notebooks/01_eda_churn.ipynb) — el análisis exploratorio
  del que salieron las decisiones de etiquetado, split y features.
- [`notebooks/02_eda_intercom.ipynb`](notebooks/02_eda_intercom.ipynb) — evaluación de los
  tickets de soporte como fuente adicional.

- [`docs/Fudata - Base de Funcionalidades.pdf`](docs/) — diccionario de datos del snapshot.
- [`docs/Prediccion_Churn_ML_Canvas.pdf`](docs/) — el ML Canvas del proyecto.
