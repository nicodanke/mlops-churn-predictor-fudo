# 5. El deploy en GCP

> Qué corre en la nube, cómo se sube y qué hace cada comando de `make`.

---

## Las tres piezas y dónde va cada una

El sistema se parte según cuánto tiempo necesita estar prendido:

| Pieza | Servicio de GCP | Por qué ahí |
|---|---|---|
| Entrenamiento (+ champion/challenger) | **Cloud Run Job** | corre unos minutos y se apaga |
| Scoring mensual | **Cloud Run Job** + Cloud Scheduler | una vez al mes, fijado a la imagen del modelo campeón |
| API + dashboard | **Cloud Run Service** (+ IAP) | tiene que estar disponible, pero escala a cero |
| Datos, modelos y predicciones | **Cloud Storage** | se monta como un directorio en los contenedores |
| CI/CD | **GitHub Actions** | sin claves: Workload Identity Federation |

La clave de que esto sea barato —**menos de USD 1 por mes**— es que nada queda prendido: los
jobs se apagan solos y el servicio escala a cero cuando nadie lo mira.

```
   GitHub push ──► GitHub Actions ──► build ──► Artifact Registry
                                                      │
                          ┌───────────────────────────┼───────────────────────┐
                          ▼                           ▼                       ▼
                   Cloud Run Job              Cloud Run Job          Cloud Run Service
                   (train)                    (score, mensual)       (API + dashboard)
                          │                           │                       │
                          └──────────► Cloud Storage ◄┘                       │
                                       (bucket)  ▲                            │
                                                 └── montado como directorio ──┘
```

**El código no sabe si está en la nube.** En Cloud Run el bucket se monta como un
directorio, y [`config/gcp.yaml`](../config/gcp.yaml) hereda todo de `model.yaml`
cambiando solo las rutas. Nada de `if cloud:` en el código.

## Los dos caminos

Hay dos formas de llegar a GCP, y conviene no confundirlas:

| | **Automático** (CI/CD) | **Manual** (el del runbook) |
|---|---|---|
| Lo dispara | un push a `main` | vos, comando por comando |
| Construye con | Cloud Build, desde GitHub Actions | `docker build` en tu máquina o en Cloud Shell |
| Acceso al dashboard | detrás de IAP (pide login con Google) | URL **pública, sin login** |
| Para qué sirve | la operación real | entender qué pasa, y para la demo de la clase |

Lo que sigue es el camino manual, paso a paso, porque es el que se ve. El automático está
documentado en [`deploy/README.md`](../deploy/README.md).

> 📋 Estos mismos pasos, pensados para ejecutarse desde Cloud Shell, están en
> [`deploy/runbook.md`](../deploy/runbook.md). Ese es el documento operativo; este es la
> explicación.

## Antes de empezar

```bash
gcloud config set project TU_PROYECTO     # todos los targets lo toman de acá
```

Cada target de `make` **imprime el comando de `gcloud` o `docker` que corre por debajo**, así
que se puede seguir qué está pasando en la nube en vez de confiar a ciegas.

## Paso 0 y 1 — Puesta a punto y datos al bucket

```bash
make gcp-cloudshell-setup                          # Poetry, dependencias y bucket
make gcp-cloudshell-upload FILE="~/account-stats-*.csv"   # los snapshots al bucket
```

`setup` crea `gs://TU_PROYECTO-churn-fudo` si no existe e instala las mismas versiones de
librerías que la imagen del pipeline (desde `poetry.lock`).

Los CSV no están en git (datos reales, ~140 MB), así que se suben aparte. **El bucket es la
fuente de verdad**: una vez subidos, los archivos locales se pueden borrar. En Cloud Shell
el pipeline no lee `data/` sino `data/bucket/`, que se mantiene como espejo exacto de
`gs://BUCKET/raw/` con un `rsync` antes de cada `train`, `score` o `drift`.

> ⚠️ **Que no falte ningún mes.** El índice de período es denso sobre los meses *presentes*:
> si falta uno del medio, los de sus dos lados quedan como consecutivos, los lags comparan
> contra el mes equivocado y una cuenta que se fue en el mes faltante parece no haberse ido
> nunca. **No da error.** El pipeline avisa con un `WARNING` (*"Faltan N periodo(s) en el
> medio de la serie"*) — si aparece, conseguir esos snapshots antes de entrenar.

## Paso 2 y 3 — Entrenar y scorear

```bash
make gcp-cloudshell-train                      # entrena; el modelo queda versionado en el bucket
make gcp-cloudshell-models                     # lista los modelos entrenados, por fecha y hora
make gcp-cloudshell-score                      # predicciones con el último modelo
make gcp-cloudshell-score RUN_ID=<fecha-hora>  # o con uno puntual
```

Cada entrenamiento queda en `gs://BUCKET/models/cloudshell/<fecha-hora>/` con su
`metadata.json`. **No se pisa el anterior** — eso es lo que hace posible volver atrás sin
reentrenar. Para ver las métricas de uno:

```bash
gcloud storage cat gs://TU_PROYECTO-churn-fudo/models/cloudshell/<fecha-hora>/metadata.json
```

> Si el entrenamiento termina en `Killed`, a Cloud Shell no le alcanzó la memoria (el
> pipeline necesita ~4 GB). Se entrena desde tu computadora con
> `PROJECT_ID=TU_PROYECTO make gcp-cloudshell-train` y se sigue igual: el modelo termina en
> el mismo bucket.

`score` deja el batch del último mes y, de paso, la estadística de la base
(`stats.json`) que alimenta la pestaña *Uso de la base*. Se generan juntos para que las dos
pestañas del dashboard hablen siempre del mismo mes.

## Paso 4 — Verlo antes de publicarlo

```bash
make gcp-cloudshell-serve      # luego: Vista previa en la Web → puerto 8080
```

Esa URL **solo abre con tu cuenta**: es la vista previa de Cloud Shell, es privada. A otra
persona le da error (es el síntoma más común de confusión acá). Para compartir, hay que
publicar.

## Paso 5 — Publicar en Cloud Run

**Una vez por proyecto:**

```bash
make gcp-docker-auth   # autoriza a Docker a publicar en Artifact Registry
make gcp-run-repo      # habilita las APIs y crea el repositorio de imágenes
make gcp-run-sa        # cuenta de servicio del servicio, con solo lectura del bucket
```

**Cada vez que cambie el código o los datos:**

```bash
make gcp-run-build     # docker build de la imagen de API + dashboard
make gcp-run-local     # opcional: probar esa imagen en :8000 antes de subirla
make gcp-run-push      # docker push al registry
make gcp-run-data      # sube predicciones, modelo y stats al bucket que monta el servicio
make gcp-run-deploy    # gcloud run deploy; imprime la URL
```

`make gcp-run-release` encadena los cuatro últimos. `make gcp-run-url` vuelve a mostrar la
URL.

Notar que `gcp-run-data` es un paso **aparte** del deploy: la imagen tiene el código, el
bucket tiene los datos. Cambiar las predicciones no requiere reconstruir ni redesplegar
nada; alcanza con subirlas al bucket.

> 🔓 **Este link no pide login.** Cualquiera que lo tenga ve nombres de cuentas, su riesgo y
> su facturación. Compartirlo con criterio y darlo de baja al terminar (paso 9). Para que
> pida iniciar sesión existe el camino con IAP.

## Paso 6 — Monitoreo

```bash
make gcp-run-logs          # últimos renglones del servicio
make gcp-run-logs-api      # tabla: método, ruta, estado, latencia y revisión
make gcp-run-logs-errors   # solo errores
make gcp-cloudshell-drift  # PSI del mes scoreado contra los datos de entrenamiento
```

Los logs son consultables **por campo** porque la API emite una línea JSON por request y
Cloud Logging la parsea:

```bash
gcloud logging read 'jsonPayload.event="request" AND jsonPayload.latency_ms>500' --limit=10
```

Y el drift responde la pregunta "¿el modelo sigue mirando el mundo con el que aprendió?":
PSI por debajo de 0.10 es ruido; de 0.25 para arriba, conviene reentrenar.

## Paso 7 — Volver atrás

Hay **dos cosas distintas** que se pueden revertir, y no se revierten igual.

### El servicio (código roto, deploy malo)

Cada `deploy` crea una **revisión** inmutable. Volver atrás es mover el tráfico: no se
reconstruye ni se vuelve a desplegar nada, y tarda segundos.

```bash
make gcp-run-revisions                                  # listar; anotar la que servía bien
make gcp-run-rollback REVISION=churn-demo-00002-abc     # 100% del tráfico a esa revisión
```

> Detalle que conviene entender: un deploy roto **puede quedar sano para Cloud Run**. Si
> `/health` sigue respondiendo 200 y solo falla un endpoint, la revisión se considera
> correcta y se lleva todo el tráfico. El healthcheck no alcanza; por eso existe el rollback
> y por eso se mira `make gcp-run-logs-errors`.

### El modelo (un modelo nuevo que salió peor)

Los modelos quedan versionados en el bucket, así que tampoco hay que reentrenar: se vuelve a
scorear con el anterior y se publican esas predicciones.

```bash
make gcp-cloudshell-models                       # elegir el anterior
make gcp-cloudshell-score RUN_ID=<fecha-hora>    # predicciones con ese modelo
make gcp-run-data && make gcp-run-deploy         # publicarlas
```

En el camino automático el equivalente es `make gcp-release TAG=<sha> FORCE=1`, que promueve
una versión anterior salteando el gate de mejora.

## Paso 8 — Reentrenar

Cuándo: llegó un snapshot nuevo, el drift marcó features con cambio alto, o cambió el código
del modelo.

```bash
# 1. datos nuevos
make gcp-cloudshell-upload FILE=~/account-stats-202609.csv
# 2. entrenar (queda como una versión más, no pisa la anterior)
make gcp-cloudshell-train
# 3. comparar contra el anterior
make gcp-cloudshell-models
gcloud storage cat gs://…/models/cloudshell/<nuevo>/metadata.json
# 4. si es mejor, scorear y publicar
make gcp-cloudshell-score
make gcp-run-data && make gcp-run-deploy
```

Se mira el **PR-AUC de test**, y que `training_periods` incluya el mes nuevo.

Pero el PR-AUC no decide solo, y el proyecto tiene el caso testigo: al reemplazar el dataset
de 2024-2025 por el de 2025-2026 los dos modelos **empataron** (0.6674 contra 0.6681) y
convino cambiar igual, por razones que la métrica no muestra — el modelo viejo estaba
entrenado con datos que ya no existían en el bucket (no se podía reentrenar ni auditar) y
tenía 43 features con PSI por encima de 0.25. **Un empate con datos más frescos y menos
drift es una mejora.** Al revés también vale: un PR-AUC más alto entrenado sobre un panel al
que le falta un mes no es una mejora, es un error.

En el camino automático esta comparación la hace el pipeline solo (*champion / challenger*):
entrena un candidato, lo evalúa **contra el campeón sobre el mismo test** y lo promueve solo
si pasa el umbral de negocio (PR-AUC ≥ 0.20), le gana por al menos 0.005 y no empeora el
ROC-AUC más de 0.01. Si no, queda guardado como candidato. El mismo gate corre local:

```bash
churn retrain --candidate-dir models/candidates/prueba --champion-dir models --use-cache
```

## Paso 9 — Frenar y limpiar

**Lo que está prendido, cuesta.** Antes de cerrar:

```bash
make gcp-resources   # qué hay creado hoy y qué puede estar costando
make gcp-stop        # da de baja el dashboard público; datos y modelos quedan
make gcp-teardown    # borra además imágenes, repositorio y cuenta de servicio
make gcp-teardown TODO=1   # borra también el bucket (no se deshace; pide confirmación)
```

Cloud Run escala a cero, así que un servicio sin visitas casi no cuesta. Lo que se acumula
son las imágenes del registry y lo que ocupa el bucket.

---

## Resumen de los comandos

### El recorrido manual, en orden

| Comando | Qué hace | Cuándo |
|---|---|---|
| `make gcp-cloudshell-setup` | dependencias + bucket | una vez |
| `make gcp-cloudshell-upload FILE=…` | snapshots al bucket | cada mes nuevo |
| `make gcp-cloudshell-train` | entrena, versiona en el bucket | al reentrenar |
| `make gcp-cloudshell-models` | lista los modelos entrenados | al comparar |
| `make gcp-cloudshell-score` | genera las predicciones | cada mes |
| `make gcp-cloudshell-serve` | vista previa privada en :8080 | para mirar rápido |
| `make gcp-docker-auth` | Docker → Artifact Registry | una vez por máquina |
| `make gcp-run-repo` | APIs + repositorio de imágenes | una vez |
| `make gcp-run-sa` | cuenta de servicio del servicio | una vez |
| `make gcp-run-build` | `docker build` | cada cambio de código |
| `make gcp-run-local` | probar la imagen en local | opcional |
| `make gcp-run-push` | `docker push` | cada cambio de código |
| `make gcp-run-data` | predicciones y modelo al bucket | cada batch nuevo |
| `make gcp-run-deploy` | `gcloud run deploy`, imprime la URL | cada cambio de código |
| `make gcp-run-release` | build + push + data + deploy | atajo de los cuatro |
| `make gcp-run-url` | mostrar la URL | cuando se perdió |

### Operación

| Comando | Qué hace |
|---|---|
| `make gcp-run-logs` | últimos logs del servicio |
| `make gcp-run-logs-api` | tabla de requests: método, ruta, estado, latencia, revisión |
| `make gcp-run-logs-errors` | solo errores |
| `make gcp-cloudshell-drift` | PSI del mes scoreado contra el entrenamiento |
| `make gcp-run-revisions` | lista las revisiones del servicio |
| `make gcp-run-rollback REVISION=…` | manda el 100 % del tráfico a una revisión anterior |
| `make gcp-resources` | qué hay creado y qué puede estar costando |
| `make gcp-stop` / `gcp-teardown` | dar de baja / borrar |

### El camino automático (IAP + CI/CD)

| Comando | Qué hace |
|---|---|
| `make gcp-bootstrap` | puesta a punto completa: bucket, registry, permisos, WIF. Una vez |
| `make gcp-grant MEMBER=user:ana@fu.do` | dar acceso al dashboard (persona, grupo o dominio) |
| `make gcp-revoke MEMBER=…` / `make gcp-access` | quitar acceso / ver quién tiene |
| `make gcp-url` | URL de la app detrás de IAP |
| `make gcp-docker-release` | build + push + deploy con Docker, por el mismo script que CI |
| `make gcp-retrain TAG=<sha>` | entrena un candidato y lo compara contra el campeón |
| `make gcp-release TAG=<sha>` | promueve esa versión y regenera predicciones (`FORCE=1` para rollback) |
| `make gcp-score` | corre el scoring con el campeón actual, a demanda |
| `make gcp-registry` | historial de versiones promovidas a producción |

### Local, para comparar

| Comando | Qué hace |
|---|---|
| `make all` | pipeline completo: `prepare` → `train` → `score` |
| `make up` / `make down` | levanta / baja API (:8000) y dashboard (:8080) con Docker |
| `make api` / `make web` | lo mismo sin Docker |
| `make test` / `make lint` | los tests y el linter |

Y `make help` lista todos los targets con su descripción.

---

## Seguridad, en una línea

El camino automático pone **todo** —dashboard, API y `/docs`— detrás de Identity-Aware
Proxy: hay que iniciar sesión con una cuenta de Google autorizada. El camino manual de la
clase publica una URL abierta, a propósito, para poder compartirla en la demo. Si se usa,
darla de baja al terminar con `make gcp-stop`.

---

**Anterior:** [4. El dashboard](04-web.md) · **Índice:** [docs/](README.md)
