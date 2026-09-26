# 1. El modelo

> ¿Qué predice, con qué datos, y por qué está hecho así?

---

## Qué predice, exactamente

Para cada cuenta activa hoy, el modelo responde una sola pregunta:

> **¿Cuál es la probabilidad de que esta cuenta no esté el mes que viene?**

La salida es un número entre 0 y 1. Nada más. Todo lo demás del sistema —la plata en
riesgo, las categorías alto/medio/bajo, el texto explicativo— se construye *después* a
partir de ese número.

Es un problema de **clasificación binaria** y, sobre todo, **desbalanceado**: solo el
~3.75 % de las cuentas se va cada mes. De cada 100 cuentas, 96 se quedan. Eso tiene una
consecuencia que conviene tener presente desde el principio: un modelo que dijera
"ninguna se va" acertaría el 96 % de las veces y sería completamente inútil.

## De dónde salen los datos

Un CSV por mes, `data/account-stats-YYYYMM.csv`, que sale del Data Warehouse de Fudo. Cada
archivo tiene una fila por cuenta activa ese mes y ~82 columnas de métricas de uso:
ventas, cantidad de comandas, canales de delivery, productos cargados, estado de cobranza,
plan contratado, país, etc.

Apilando todos los meses se arma el **panel**: 599.967 filas, 53.160 cuentas, de 202501 a
202608.

```
id      periodo   ventas   comandas   estado            plan
1042    202501    18400    980        ACTIVE            pro-tbl-ar
1042    202502    17900    910        ACTIVE            pro-tbl-ar
1042    202503    12000    540        PENDING_PAYMENT   pro-tbl-ar
1042    202504     ...      ...        ...               ...
```

> ⚠️ Los CSV **no están en git**: son datos de cuentas reales y pesan ~140 MB. Hay que
> conseguirlos aparte y ponerlos en `data/`. Ver [`data/README.md`](../data/README.md).

## El problema más interesante: la etiqueta no existe

Este es el punto que más define al proyecto. **En los datos no hay ninguna columna que
diga "esta cuenta se dio de baja".** Hay que deducirla.

El truco: el reporte mensual **solo incluye cuentas activas**. Entonces, si una cuenta
aparece en agosto y no aparece en septiembre, se fue.

Aproximadamente ~17 % de las desapariciones son **temporales**: la cuenta vuelve uno
o dos meses después. Son restaurantes de temporada (un parador de playa que cierra en
invierno) o pausas administrativas. Si eso se cuenta como baja, el modelo aprende que
"dejar de operar en mayo" es churn, y eso es ruido.

La solución es una **ventana de confirmación** de 3 meses:

| Situación | Etiqueta | Qué se hace con esa fila |
|---|---|---|
| Está en `t`, y también en `t+1` | `churn = 0` | se usa para entrenar |
| Está en `t`, y no aparece en `t+1`, `t+2` ni `t+3` | `churn = 1` | se usa para entrenar |
| Está en `t`, falta en `t+1`, pero **reaparece** en `t+2` o `t+3` | *ambigua* | **se descarta** |
| Los últimos 3 meses del panel | *no evaluable* | no hay futuro para confirmar todavía |

Se descartan además las cuentas con menos de 2 meses de historia: una cuenta recién creada
no tiene tendencias que mirar, y su dinámica de baja es distinta.

Código: [`src/churn/data/labeling.py`](../src/churn/data/labeling.py) ·
Configuración: `labeling` en [`config/model.yaml`](../config/model.yaml)

## Las features: lo que importa es el movimiento

Que un restaurante facture 5.000 o 500.000 no dice casi nada sobre si se va a ir. Un bar
de barrio factura poco y es un cliente feliz desde hace cinco años. Lo que anticipa una
baja es el **cambio**: que este mes facture la mitad que el anterior.

Por eso, sobre cada una de las ~82 métricas crudas se calculan features derivadas:

| Feature generada | Qué captura | Ejemplo |
|---|---|---|
| `<col>_delta_1m` | variación contra el mes pasado | las ventas cayeron 40 % |
| `<col>_ratio_3m` | nivel actual sobre el promedio de los 3 meses previos | está al 60 % de su nivel habitual |
| `<col>_delta_3m` | variación contra hace 3 meses | viene bajando desde mayo |
| `<col>_slope_3m` | pendiente de la tendencia reciente | la caída se está acelerando |

Más un conjunto de features de negocio hechas a mano: mix de canales de venta, amplitud de
uso del producto (¿cuántos módulos toca?), ratios de configuración, antigüedad, pausas
previas, severidad y racha del estado de cobranza, y la descomposición del plan en
plan base + módulos + mercado (con detección de downgrades).

**Total: 296 features construidas, 284 entran al modelo.** Las 12 que quedan afuera son
las de categorías de gasto, y el motivo es una trampa clásica que vale la pena conocer:
esa funcionalidad se lanzó en 202507, así que su uso sube todos los meses porque se está
adoptando. Una feature que crece con el calendario funciona como un reloj: separa bien
dentro de los meses que el modelo vio, y en producción sigue subiendo fuera de ese rango.
Sacarlas no costó nada de performance.

> 🔍 Detalle de implementación que importa: los lags se calculan con un *join* sobre
> `(id, t - lag)`, **no** con `groupby().shift()`. Hay cuentas con huecos en el panel, y un
> shift ciego tomaría el mes equivocado sin avisar.

Código: [`src/churn/features/builder.py`](../src/churn/features/builder.py)

## El algoritmo: XGBoost

**XGBoost** (gradient boosted trees). No es una elección de moda, es la que corresponde a
la forma del dataset:

| El dataset es… | …y los árboles |
|---|---|
| tabular, no imágenes ni texto | son el estado del arte en tabular |
| mixto: números y categorías | manejan las dos cosas |
| lleno de huecos estructurales (los canales de delivery que la cuenta no usa vienen vacíos) | tratan los `NaN` de forma nativa, sin imputar |
| de escalas muy distintas (pesos, cantidades, ratios) | no necesitan escalado |
| desbalanceado (~4 % positivos) | se corrige con un peso, sin resamplear |

Hiperparámetros en `model.params` de [`config/model.yaml`](../config/model.yaml): 2000
árboles como techo con *early stopping* a los 100, `learning_rate` 0.05, `max_depth` 6, y
submuestreo de filas y columnas (0.8 / 0.7) para no sobreajustar.

### Tres decisiones que cambian el resultado

**1. Split temporal estricto.** Se entrena con los meses viejos y se evalúa con los
recientes, igual que va a pasar en producción.

```
  202502 ─────────── 202601 │ 202602 ─ 202603 │ 202604 ─ 202605 │ 202606 ─ 202608
        TRAIN               │      VAL        │      TEST       │  sin etiqueta
        (12 meses)          │    (2 meses)    │    (2 meses)    │  (ventana de 3m)
```

Los últimos tres meses del panel no tienen etiqueta todavía —hace falta esperar la ventana
de confirmación— así que no entran a ninguna partición. Y 202501 queda afuera de train
porque sus cuentas no tienen los 2 meses de historia mínimos.

Un split aleatorio sería un error grave acá: la misma cuenta aparece en muchos meses con
features casi idénticas, así que la cuenta 1042 de marzo estaría en train y la de abril en
test. El modelo no predeciría, recordaría. La métrica saldría hermosa y en producción se
derrumbaría.

**2. `scale_pos_weight` en vez de resampleo.** Se le dice al modelo que equivocarse en una
cuenta que se va cuesta ~28 veces más que equivocarse en una que se queda. Se calcula solo
como `negativos / positivos`. No se duplican ni se descartan filas.

**3. Calibración isotónica.** Esta es la menos obvia y la más importante para este
proyecto. `scale_pos_weight` mejora el ranking pero **deforma las probabilidades**: el
modelo empieza a decir 0.6 cuando la frecuencia real es 0.15. Para un ranking eso da
igual — el orden se mantiene. Acá no da igual, porque el riesgo económico se calcula como
`probabilidad × revenue`: una probabilidad inflada es plata mal priorizada.

Entonces, después de entrenar, se ajusta una regresión isotónica sobre el set de
validación que corrige esa distorsión. El `brier score` de 0.019 en test es lo que dice que quedó
bien calibrado.

Código: [`src/churn/models/train.py`](../src/churn/models/train.py) ·
[`split.py`](../src/churn/models/split.py)

## Cómo se mide si está bueno

La métrica de decisión es **PR-AUC** (área bajo la curva precision-recall), no accuracy ni
ROC-AUC. Con un 4 % de positivos, esas dos se ven bien sin esfuerzo y no informan nada.

Resultados sobre el test (202604–202605), meses que el modelo nunca vio:

| Métrica | Valor | Cómo leerlo |
|---|---|---|
| **PR-AUC** | **0.667** | el umbral acordado con negocio era 0.20 |
| ROC-AUC | 0.955 | de referencia, no se usa para decidir |
| Churn base mensual | 3.75 % | lo que acertaría el azar → el modelo da **17.8× lift** |
| Precisión en el top 5 % | 52.9 % (recall 70.4 %) | |
| Precisión en el top 1 % | 93.0 % (recall 24.8 %) | |

La traducción a lenguaje de negocio es la fila del top 5 %:

> Si CX trabaja las **3.100 cuentas más riesgosas**, más de la **mitad** efectivamente se
> iba a dar de baja, y ese grupo contiene **7 de cada 10 bajas del mes**.

Y como el modelo prioriza, se puede elegir cuánto esfuerzo poner: el top 1 % son 612
cuentas donde acierta 93 de cada 100, ideal si el equipo es chico.

## Dos hallazgos que conviene conocer

**La cobranza es una señal tardía.** Buena parte de la performance viene del estado de
cobranza (`PENDING_PAYMENT`, `BLOCKING_ALERT`, `BLOCKED`). Es tramposo: cuando una cuenta
ya está bloqueada, la baja prácticamente ya ocurrió — avisar ahí llega tarde. Se midió el
modelo **sin ninguna feature de cobranza** y el PR-AUC baja a 0.317 (8.4× el baseline),
todavía muy por encima del umbral de negocio. O sea: **hay señal real y anticipada en el
uso del producto**, no solo en la mora.

**Más datos no mejoran nada.** Era la hipótesis al cambiar el dataset, y no se cumplió.
Fijando val y test y variando solo cuánta historia entra a train:

| Períodos de train | Filas | PR-AUC (val) |
|---|---|---|
| 3 | 88.325 | 0.6523 |
| 7 | 197.889 | 0.6544 |
| 12 | 326.691 | 0.6585 |

Cuadruplicar los datos mueve el PR-AUC 0.006. El techo no está en el volumen de historia
sino en la señal disponible, así que esperar más meses no va a cambiar el número. Lo que sí
movió la aguja fue elegir *qué* features entran.

## El artefacto entrenado

Entrenar produce un solo archivo, `models/churn_model.joblib`, con todo lo necesario para
predecir después:

- el modelo XGBoost,
- el calibrador isotónico,
- **la lista exacta de features y su orden** (el contrato),
- los niveles de cada variable categórica,
- las métricas, el umbral de decisión y los períodos con los que se entrenó.

Al lado queda un `metadata.json` legible, que es lo que sirve el endpoint
`/api/v1/model`.

Ese contrato de features es lo que hace que el scoring degrade en vez de romperse: si en
producción aparece una feature nueva, se ignora; si falta una, se completa con `NaN`, que
XGBoost sabe manejar. Queda un `WARNING` en el log.

Código: [`src/churn/models/artifact.py`](../src/churn/models/artifact.py)

## Cómo se entrena

```bash
make prepare   # lee los CSV, infiere las etiquetas y construye las features (cachea)
make train     # entrena, calibra, evalúa e imprime las métricas
make info      # metadatos y métricas del modelo guardado
```

El paso pesado es `prepare` (feature engineering sobre ~600k filas); necesita ~4 GB de RAM.
Queda cacheado en `outputs/interim/features.parquet` y se recalcula solo si cambian los
CSV. Con `make prepare-force` se fuerza.

---

**Siguiente:** [2. El scoring](02-scoring.md) — cómo se usa este modelo cada mes.
