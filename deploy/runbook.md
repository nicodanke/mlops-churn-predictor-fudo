# Runbook: correr todo desde Cloud Shell

Los comandos, en orden, para llevar el predictor de churn a GCP desde la terminal de Cloud
Shell: datos, modelo, predicciones, dashboard publicado, monitoreo, reentrenamiento y
limpieza.

Todo sale de `make`, y cada target imprime el comando de `gcloud` o de `docker` que corre
por debajo, así que se puede seguir paso a paso lo que pasa en la nube.

**Lo que se asume:** un proyecto de GCP con billing, el repo clonado en Cloud Shell y el
snapshot mensual (`account_stats_since_2024.csv`) a mano. El detalle de las decisiones de
arquitectura está en [`README.md`](README.md) de esta misma carpeta.

---

## 0. Puesta a punto

```bash
gcloud config set project TU_PROYECTO     # todo lo demás lo toma de acá
cd mlops-churn-predictor-fudo
make gcp-cloudshell-setup                 # Poetry, dependencias y bucket. Unos minutos
```

`setup` crea `gs://TU_PROYECTO-churn-fudo` si no existe e instala las mismas versiones de
librerías que usa la imagen del pipeline (`poetry.lock`).

## 1. Los datos al bucket

El CSV no está en git: tiene datos de cuentas reales y pesa ~160 MB. Se sube a Cloud Shell
con *⋮ Más → Subir* y después:

```bash
make gcp-cloudshell-upload FILE=~/account_stats_since_2024.csv
```

Se guarda comprimido en `gs://TU_PROYECTO-churn-fudo/raw/`. El mes siguiente se repite con
el archivo nuevo: los snapshots se acumulan y el pipeline los concatena solo.

**Opcional, precios reales.** Sin esto el revenue en riesgo sale de la lista de ejemplo y
es ilustrativo. `config/pricing.yaml` tampoco está en git, así que se sube desde tu
computadora:

```bash
gcloud storage cp config/pricing.yaml gs://TU_PROYECTO-churn-fudo/config/pricing.yaml
```

## 2. Entrenar

```bash
make gcp-cloudshell-train      # ~minutos; el modelo queda versionado en el bucket
make gcp-cloudshell-models     # lista los modelos entrenados, por fecha y hora
```

El modelo queda en `gs://TU_PROYECTO-churn-fudo/models/cloudshell/<fecha-hora>/`, junto con
un `metadata.json` con sus métricas. Cloud Shell se puede cerrar: lo que importa está en el
bucket.

Para ver las métricas de un modelo:

```bash
gcloud storage cat gs://TU_PROYECTO-churn-fudo/models/cloudshell/<fecha-hora>/metadata.json
```

> Si termina en `Killed`, a Cloud Shell no le alcanzó la memoria (el pipeline necesita
> ~4 GB). Entrená desde tu computadora con `PROJECT_ID=TU_PROYECTO make gcp-cloudshell-train`
> y seguí el resto acá: el modelo queda igual en el bucket.

## 3. Predicciones

```bash
make gcp-cloudshell-score                      # usa el último modelo del bucket
make gcp-cloudshell-score RUN_ID=<fecha-hora>  # o uno puntual
```

Deja el batch del último mes en `outputs/cloudshell/predictions/<periodo>/`: una fila por
cuenta con su probabilidad de baja, el riesgo económico y las features que explican cada
predicción.

## 4. Ver el dashboard

**Solo para vos, rápido:**

```bash
make gcp-cloudshell-serve
```

Después, botón **Vista previa en la Web** → *puerto 8080*. Para la API, agregar `/docs` a
esa URL. Se corta con `Ctrl+C`. Esa URL **solo abre con tu cuenta**: a otra persona le da
error.

**Para compartir un link**, ver el paso siguiente.

## 5. Publicarlo en Cloud Run desde una imagen

El recorrido de la clase 6: construir la imagen, publicarla en Artifact Registry y levantar
Cloud Run desde esa imagen.

> **El link no pide login.** Cualquiera que lo tenga ve nombres de cuentas, su riesgo y su
> facturación. Compartirlo con criterio y darlo de baja al terminar (paso 9). Para que pida
> iniciar sesión está el despliegue detrás de IAP, en [`README.md`](README.md#autenticación).

Una vez por proyecto:

```bash
make gcp-docker-auth   # autoriza a Docker a publicar en el registry
make gcp-run-repo      # habilita las APIs y crea el repositorio de imágenes
make gcp-run-sa        # cuenta de servicio del servicio, con solo lectura del bucket
```

Y cada vez que cambie el código o los datos:

```bash
make gcp-run-build     # docker build de la imagen de la API + dashboard
make gcp-run-local     # opcional: probar esa imagen en :8000 antes de subirla
make gcp-run-push      # docker push al registry
make gcp-run-data      # sube las predicciones al bucket que monta el servicio
make gcp-run-deploy    # gcloud run deploy con esa imagen; imprime la URL
```

`make gcp-run-release` encadena los cuatro últimos. `make gcp-run-url` vuelve a mostrar la
URL.

**Alternativa en un solo comando:** `make gcp-cloudshell-publish` hace lo mismo pero
construye con Cloud Build en vez de Docker.

## 6. Monitoreo

**Logs.** La API emite una línea JSON por request, así que Cloud Logging la parsea a campos
y se puede filtrar por cualquiera de ellos:

```bash
make gcp-run-logs          # últimos renglones del servicio
make gcp-run-logs-api      # tabla: método, ruta, estado, latencia y revisión
make gcp-run-logs-errors   # solo errores
gcloud logging read 'jsonPayload.event="request" AND jsonPayload.latency_ms>500' --limit=10
```

**Drift.** Compara el mes que se scorea contra los meses con los que se entrenó el modelo:

```bash
make gcp-cloudshell-drift
```

PSI por debajo de 0.10 es ruido; de 0.25 para arriba, el modelo está viendo otro mundo y
conviene reentrenar.

## 7. Volver atrás

**El servicio.** Cada deploy crea una revisión; volver a la anterior es mover el tráfico,
sin reconstruir nada:

```bash
make gcp-run-revisions
make gcp-run-rollback REVISION=churn-demo-00002-abc
```

**El modelo.** Los modelos quedan versionados en el bucket, así que no hay que reentrenar:

```bash
make gcp-cloudshell-models                       # elegir el anterior
make gcp-cloudshell-score RUN_ID=<fecha-hora>    # predicciones con ese modelo
make gcp-run-data && make gcp-run-deploy         # publicarlas
```

## 8. Reentrenar

Cuándo: llegó el snapshot de un mes nuevo, `make gcp-cloudshell-drift` marcó features con
cambio alto, o cambió el código del modelo.

```bash
# 1. Si hay datos nuevos, subirlos
make gcp-cloudshell-upload FILE=~/account_stats_202604.csv

# 2. Entrenar un modelo nuevo (queda como una versión más, no pisa la anterior)
make gcp-cloudshell-train

# 3. Comparar contra el anterior antes de usarlo
make gcp-cloudshell-models
gcloud storage cat gs://TU_PROYECTO-churn-fudo/models/cloudshell/<nuevo>/metadata.json
gcloud storage cat gs://TU_PROYECTO-churn-fudo/models/cloudshell/<anterior>/metadata.json
```

Lo que se mira es el **PR-AUC de test**: si el nuevo no le gana al anterior, no hay razón
para cambiarlo. También conviene mirar que `training_periods` incluya el mes nuevo.

```bash
# 4. Si el nuevo es mejor, generar predicciones y publicarlas
make gcp-cloudshell-score
make gcp-run-data && make gcp-run-deploy
```

Si sale peor, alcanza con seguir scoreando con el modelo anterior (paso 7): los dos quedan
en el bucket.

> En el camino automático —GitHub Actions y Cloud Run Jobs— esta comparación la hace el
> pipeline solo: entrena un candidato, lo evalúa contra el campeón sobre el mismo test y lo
> promueve únicamente si mejora. Ver *Cómo decide si un modelo pasa a producción* en
> [`README.md`](README.md).

## 9. Frenar y limpiar

**Lo que está prendido, cuesta.** Antes de cerrar:

```bash
make gcp-resources   # qué hay creado hoy y qué puede estar costando
make gcp-stop        # da de baja el dashboard público; el resto queda intacto
```

Para borrar también las imágenes, el repositorio y la cuenta de servicio:

```bash
make gcp-teardown
```

Y para borrar además el bucket, con el snapshot y todos los modelos entrenados (no se puede
deshacer: pide escribir el nombre del bucket para confirmar):

```bash
make gcp-teardown TODO=1
```

Cloud Run escala a cero, así que un servicio sin visitas casi no cuesta; lo que se acumula
son las imágenes del registry y lo que ocupa el bucket. Nada de esto toca lo que crea
`bootstrap.sh` para el sistema automático.

---

## Si algo falla

| Síntoma | Qué pasa |
|---|---|
| `GCP_PROJECT no esta configurado` | Falta `gcloud config set project TU_PROYECTO` |
| `Killed` al entrenar o scorear | Cloud Shell se quedó sin memoria: correrlo en tu computadora |
| `Faltan las credenciales de Python` | `gcloud auth application-default login` (en Cloud Shell no suele hacer falta) |
| `denied` al hacer push | Falta `make gcp-docker-auth` |
| `NOT_FOUND` al hacer push | Falta `make gcp-run-repo` |
| `No hay snapshots en .../raw/` | Falta el paso 1 |
| Error de *organization policy* con `allUsers` | El proyecto no permite servicios públicos: usar el despliegue con IAP |
| El link le da error a otra persona | Es la Vista previa de Cloud Shell, que es privada: publicar con el paso 5 |
