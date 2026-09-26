# 4. El dashboard

> Con qué está hecho, qué muestra y cómo le pide los datos a la API.

---

## La tecnología: ninguna

Tres archivos, 1.600 líneas en total, **sin frameworks y sin dependencias**:

| Archivo | Líneas | Qué es |
|---|---|---|
| [`web/index.html`](../web/index.html) | 167 | el esqueleto: pestañas, tarjetas, tabla y panel lateral, todo vacío |
| [`web/styles.css`](../web/styles.css) | 564 | los estilos, con variables CSS para la paleta de riesgo |
| [`web/app.js`](../web/app.js) | 873 | todo el comportamiento: fetch, estado, render, gráficos |

No hay React, ni Vue, ni Chart.js, ni Tailwind, ni bundler, ni `node_modules`, ni paso de
build. Los gráficos son **SVG dibujado a mano** en JavaScript.

Es una decisión deliberada, y las razones son concretas:

- **El dashboard viaja dentro de la imagen de la API** (~64 KB de archivos estáticos). Sin
  build no hay nada que compilar en el Dockerfile ni en CI.
- **Sin dependencias no hay superficie de ataque ni mantenimiento**: nada que actualizar por
  un CVE, nada que se rompa en la próxima major.
- El alcance lo permite: son dos pestañas, cuatro gráficos y una tabla. Un framework acá
  agregaría más complejidad de la que resuelve.

Se abre con doble clic en el archivo y funciona.

## Qué muestra

Dos pestañas, que se linkean con el hash de la URL (`#riesgo`, `#uso`).

### Pestaña *Riesgo* — a quién llamar este mes

```
┌──────────────────────────────────────────────────────────────────────────┐
│  KPIs:  cuentas en riesgo · riesgo alto · riesgo medio · revenue         │
├─────────────────────────────────────┬────────────────────────────────────┤
│  Histograma de probabilidades       │  Top 10 features del mes           │
│  (con la línea del umbral)          │  (importancia global, |SHAP|)      │
├─────────────────────────────────────┴────────────────────────────────────┤
│  Tabla de cuentas          [buscar…]   [alto] [medio] [bajo]             │
│  ──────────────────────────────────────────────────────────────────────  │
│  Cuenta         Plan   Prob.  Revenue  En riesgo  Categoría              │
│  Ejemplo SRL    Pro     96%      148       142      alto                 │
│  …                                                                       │
│                           ← Anterior   Siguiente →                       │
└──────────────────────────────────────────────────────────────────────────┘

   Un clic en cualquier fila abre el panel lateral con el detalle de esa cuenta.
```

El panel lateral (*drawer*) es lo que usa CX antes de llamar: el diagnóstico en texto, el
desglose del precio, el aviso de estacionalidad si aplica, y el **mapa de calor** de las 8
features que más pesaron — cada una con su valor, su aporte y el percentil de la cuenta
contra el resto de la base, coloreado según empuje o frene el riesgo.

### Pestaña *Uso de la base* — contra qué se lee eso

Es el reporte de EDA, y responde el contexto: cuánto crece la base, cuánto se va por mes
(el churn base contra el que se compara el modelo), qué parte del producto usa una cuenta
promedio, y qué señales distinguen a una cuenta que se va. Cuatro bloques: KPIs, serie de
la base, serie de churn, adopción por funcionalidad y tabla de señales.

## Cómo le habla a la API

### Encontrar la API

La URL se resuelve en tres intentos, en orden ([`app.js:11`](../web/app.js#L11)):

```js
1. ?api=http://otro-host:8000   // query string, para probar contra otra API
2. window.CHURN_API_URL         // lo que inyecta config.js
3. http://localhost:8000        // default de desarrollo
```

Ese `config.js` es el que resuelve el problema de "la misma imagen en dos entornos", y se
genera distinto según quién sirva el dashboard:

| Entorno | Quién sirve la web | De dónde sale `config.js` |
|---|---|---|
| Local con Docker | nginx, en `:8080` | lo escribe el entrypoint del contenedor con la variable `CHURN_API_URL` |
| Cloud Run | **la propia API**, en `/` | lo genera la API: `window.location.origin` |

En Cloud Run, entonces, el dashboard le habla al **mismo origen** que lo sirvió. Eso no es
un detalle estético: la sesión de Identity-Aware Proxy es una cookie del dominio del
servicio, y un front en otro dominio no podría mandarla. De paso, no hay CORS.

El `<script src="config.js" onerror="void 0">` de `index.html` tiene ese `onerror` para que,
abriendo el HTML suelto desde el disco (donde `config.js` no existe), no aparezca un error
en la consola: cae al default de localhost y funciona.

### Un solo helper para todos los fetch

Todo pasa por `api()` ([`app.js:46`](../web/app.js#L46)), que hace cuatro cosas:

```js
async function api(path, params = {}) {
  const url = new URL(API + path);
  if (state.periodo) url.searchParams.set("periodo", state.periodo);  // 1
  // … agrega los params, salteando null/undefined/"" y expandiendo arrays   2
  const response = await fetch(url);                                  // 3
  if (!response.ok) throw new Error(body.detail || `${response.status} …`); // 4
  return response.json();
}
```

1. **Inyecta el período** en cada request, así ninguna función tiene que acordarse de pasarlo.
2. Arma la query string, expandiendo arrays (`risk=alto&risk=medio`).
3. `fetch` sin headers ni tokens: la cookie de IAP viaja sola, y no hay API key.
4. Convierte un status != 2xx en una excepción con el `detail` que mandó la API.

No hay librería HTTP ni capa de servicios: hay `fetch`, y ese wrapper.

### La secuencia de arranque

```
boot()
 ├─ GET /api/v1/me            → muestra el mail de la sesión (si falla, se ignora)
 ├─ GET /api/v1/periodos      → llena el selector de mes. Sin períodos, corta acá con un aviso
 └─ loadAll()
     ├─ GET /api/v1/summary        ─┐
     ├─ GET /api/v1/distribution   ─┼─ en paralelo, con Promise.all
     ├─ GET /api/v1/importance     ─┘
     └─ GET /api/v1/accounts        ← después, porque depende de los filtros ya renderizados
```

Y después, cada interacción dispara solo lo que hace falta:

| Acción del usuario | Request |
|---|---|
| escribir en el buscador, tocar un filtro, paginar, ordenar | `GET /accounts` con los params nuevos |
| clic en una fila | `GET /accounts/{id}` |
| cambiar el mes en el selector | `loadAll()` de nuevo, todo |
| entrar a la pestaña *Uso de la base* | `GET /eda`, **una sola vez** (queda en `state.eda`) |

Los filtros **no se resuelven en el navegador**: se mandan a la API. El front nunca tiene
las 33.000 cuentas en memoria, solo la página que está mostrando.

### El estado

Un objeto plano, `state` ([`app.js:26`](../web/app.js#L26)), con el período, los filtros,
el orden, la página y el reporte de EDA cacheado. No hay store, ni reducers, ni
reactividad: cuando algo cambia, se llama a la función de carga que corresponde y esa
función vuelve a escribir su pedazo de HTML.

## Los gráficos

SVG generado a mano. `chartFrame()` calcula la geometría —ancho útil, escalas, posición de
cada punto— y cada `render*()` arma el `<svg>` como string.

Dos detalles que se resolvieron ahí y valen la pena:

- **Escala desde cero, siempre.** En una serie de conteos o de tasas, recortar la base
  exagera visualmente cualquier variación. Un gráfico que empieza en 3.2 % hace parecer un
  desastre a una fluctuación de medio punto.
- **Redibujado al redimensionar.** El ancho se toma de `clientWidth`, que da 0 si la pestaña
  está oculta. En ese caso se usa un ancho razonable y un listener de `resize` vuelve a
  dibujar cuando la vista se muestra.

Los tooltips se hacen con una capa transparente que escucha `mousemove` sobre el gráfico y
posiciona un div, en vez de poner un listener por punto.

## Manejo de errores

Tres casos, tres respuestas distintas:

| Qué falla | Qué ve el usuario |
|---|---|
| La API no responde | banner arriba: *"No se pudo conectar con la API en … "* |
| No hay predicciones todavía | banner: *"Todavía no hay predicciones generadas. Corré `make score`…"* |
| Una cuenta no carga | el error queda dentro del panel lateral; el resto del dashboard sigue funcionando |

Hay un cuarto caso, medio contraintuitivo, que está manejado a propósito: **detrás de IAP
una sesión vencida no devuelve 401**, redirige al login de Google, y el navegador corta ese
redirect dentro de un `fetch`. Lo que llega es un error de red genérico, así que el mensaje
sugiere recargar la página — que sí vuelve a pedir login.

Y hay un banner específico para `pricing_loaded: false`: avisa que la lista de precios está
sin cargar y que por eso el revenue en riesgo se muestra en 0.

> Los campos de texto que vienen de la API —nombres de cuenta, planes, etiquetas del EDA—
> pasan por `escapeHtml()` antes de interpolarse en el HTML: son datos de un DW, no HTML
> confiable.

## Cómo levantarlo

```bash
make up      # Docker: dashboard en :8080, API en :8000
make web     # sin Docker: sirve web/ en :8080 (la API tiene que estar en :8000)
```

Y las variantes útiles para desarrollo:

```bash
open web/index.html                                  # abre el archivo suelto → pega a localhost:8000
open 'http://localhost:8080/?api=https://otra-api'   # apuntarlo a otra API
open 'http://localhost:8080/#uso'                    # entrar directo a una pestaña
```

En GCP no hay contenedor de web: el dashboard lo sirve la API desde el mismo servicio. Ver
[5. El deploy](05-deploy.md).

---

**Anterior:** [3. La API](03-api.md) · **Siguiente:** [5. El deploy en GCP](05-deploy.md)
