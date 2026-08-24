/* Dashboard de riesgo de churn.
 *
 * Consume la API de solo lectura que sirve los resultados del scoring batch.
 * La URL de la API se resuelve, en orden: ?api= en la query string, la variable
 * window.CHURN_API_URL (que inyecta el contenedor via config.js), o localhost:8000.
 */

const API = (() => {
  const fromQuery = new URLSearchParams(location.search).get("api");
  if (fromQuery) return fromQuery.replace(/\/$/, "");
  if (window.CHURN_API_URL) return String(window.CHURN_API_URL).replace(/\/$/, "");
  return "http://localhost:8000";
})();

const RISK_LABELS = { alto: "Alto", medio: "Medio", bajo: "Bajo", no_churn: "Sin riesgo" };
const MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
               "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"];

const RISK_COLORS = {
  alto: "var(--alto)", medio: "var(--medio)", bajo: "var(--bajo)", no_churn: "var(--ok)",
};

const state = {
  periodo: null,
  currency: "USD",
  risk: ["alto", "medio", "bajo"],
  search: "",
  sortBy: "revenue_at_risk",
  ascending: false,
  // null = todas · true = solo estacionales · false = excluirlas
  seasonal: null,
  page: 1,
  size: 25,
  pages: 1,
};

const $ = (id) => document.getElementById(id);

/* ---------------------------------------------------------------- helpers */

async function api(path, params = {}) {
  const url = new URL(API + path);
  if (state.periodo) url.searchParams.set("periodo", state.periodo);
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value)) value.forEach((v) => url.searchParams.append(key, v));
    else url.searchParams.set(key, value);
  }
  const response = await fetch(url);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

const money = (n) =>
  new Intl.NumberFormat("es-AR", { maximumFractionDigits: 0 }).format(Math.round(n || 0));
const pct = (n, digits = 0) => `${(100 * (n || 0)).toFixed(digits)}%`;
const num = (n) => new Intl.NumberFormat("es-AR").format(n || 0);

function periodLabel(periodo) {
  const s = String(periodo);
  const months = ["ene", "feb", "mar", "abr", "may", "jun",
                  "jul", "ago", "sep", "oct", "nov", "dic"];
  return `${months[parseInt(s.slice(4, 6), 10) - 1]} ${s.slice(0, 4)}`;
}

/** Color del mapa de calor: un percentil alto en una feature que empuja el riesgo
 *  se pinta rojo; una que lo contiene, verde. */
function heatColor(contribution) {
  return contribution.direction === "aumenta" ? "var(--alto)" : "var(--ok)";
}

function showBanner(message, kind = "warn") {
  const el = $("banner");
  el.textContent = message;
  el.classList.toggle("hidden", !message);
}

/* ------------------------------------------------------------------ carga */

async function boot() {
  try {
    const periodos = await api("/api/v1/periodos");
    if (!periodos.length) {
      showBanner("Todavía no hay predicciones generadas. Corré `make score` para producir el primer batch.");
      $("accounts-body").innerHTML = `<tr><td colspan="6" class="empty">Sin datos</td></tr>`;
      return;
    }
    state.periodo = periodos[0];

    const select = $("period-select");
    select.innerHTML = periodos
      .map((p) => `<option value="${p}">${periodLabel(p)}</option>`)
      .join("");
    select.value = state.periodo;
    select.onchange = () => {
      state.periodo = Number(select.value);
      state.page = 1;
      loadAll();
    };

    renderRiskFilters();
    wireControls();
    await loadAll();
  } catch (error) {
    showBanner(`No se pudo conectar con la API en ${API} — ${error.message}`);
  }
}

async function loadAll() {
  await Promise.all([loadSummary(), loadHistogram(), loadImportance()]);
  await loadAccounts();
}

/* --------------------------------------------------------------- resumen */

async function loadSummary() {
  const data = await api("/api/v1/summary");
  state.currency = data.currency;

  $("batch-meta").innerHTML = `
    <div>Predicción de bajas de <strong>${periodLabel(data.periodo_prediccion)}</strong></div>
    <div>${num(data.n_accounts)} cuentas activas · generado ${data.generated_at.slice(0, 10)}</div>`;

  if (!data.pricing_loaded) {
    showBanner(
      "La lista de precios está sin cargar (config/pricing.yaml): el revenue en riesgo se muestra en 0. " +
      "Completá los precios por plan y módulo y volvé a correr el scoring."
    );
  } else {
    showBanner("");
  }

  const by = Object.fromEntries(data.summary.map((r) => [r.risk_category, r]));
  const enRiesgo = ["alto", "medio", "bajo"].reduce((acc, k) => acc + (by[k]?.cuentas || 0), 0);
  const revenueEnRiesgo = ["alto", "medio", "bajo"]
    .reduce((acc, k) => acc + (by[k]?.revenue_en_riesgo || 0), 0);

  const cards = [
    {
      label: "Cuentas en riesgo", value: num(enRiesgo), accent: "var(--brand)",
      sub: `${pct(enRiesgo / data.n_accounts, 1)} de la base · umbral ${pct(data.decision_threshold)}`,
    },
    {
      label: "Riesgo alto", value: num(by.alto?.cuentas || 0), accent: "var(--alto)",
      sub: `${money(by.alto?.revenue_en_riesgo)} ${data.currency}/mes en juego`,
    },
    {
      label: "Riesgo medio", value: num(by.medio?.cuentas || 0), accent: "var(--medio)",
      sub: `${money(by.medio?.revenue_en_riesgo)} ${data.currency}/mes en juego`,
    },
    {
      label: "Revenue en riesgo", value: `${money(revenueEnRiesgo)}`, accent: "var(--brand)",
      sub: `${data.currency} por mes, sobre cuentas señaladas`,
    },
  ];

  $("kpis").innerHTML = cards
    .map((c) => `
      <div class="kpi" style="--accent:${c.accent}">
        <div class="kpi-label">${c.label}</div>
        <div class="kpi-value">${c.value}</div>
        <div class="kpi-sub">${c.sub}</div>
      </div>`)
    .join("");
}

/* ----------------------------------------------------------- histograma */

async function loadHistogram() {
  const data = await api("/api/v1/distribution", { bins: 24 });
  const max = Math.max(...data.bins.map((b) => b.cuentas), 1);

  // Raiz cuadrada: la primera barra concentra el 95% de la base y en escala lineal
  // aplasta a las demas hasta hacerlas invisibles, que son justo las que interesan.
  const scale = (n) => (Math.sqrt(n) / Math.sqrt(max)) * 100;

  $("histogram").innerHTML = data.bins
    .map((b) => {
      const height = b.cuentas === 0 ? 1 : Math.max(3, scale(b.cuentas));
      const over = b.desde >= data.threshold ? " over" : "";
      const tip = `${num(b.cuentas)} cuentas · ${pct(b.desde)}–${pct(b.hasta)}`;
      return `<div class="hbar${over}" style="height:${height}%" data-tip="${tip}"></div>`;
    })
    .join("");

  const overThreshold = data.bins
    .filter((b) => b.desde >= data.threshold)
    .reduce((acc, b) => acc + b.cuentas, 0);
  $("histogram-legend").innerHTML =
    `En rojo, las ${num(overThreshold)} cuentas por encima del umbral de decisión ` +
    `(${pct(data.threshold, 1)}). Altura en escala de raíz cuadrada: en escala lineal ` +
    `la primera barra aplastaría al resto.`;
}

/* -------------------------------------------------- importancia global */

async function loadImportance() {
  const rows = (await api("/api/v1/importance")).slice(0, 10);
  const max = Math.max(...rows.map((r) => r.mean_abs_shap), 0.0001);

  $("global-importance").innerHTML = rows
    .map((r) => `
      <div class="imp-row" title="${r.label} · ${r.group}">
        <div class="imp-label">${r.label}</div>
        <div class="imp-track"><div class="imp-fill" style="width:${(r.mean_abs_shap / max) * 100}%"></div></div>
      </div>`)
    .join("");
}

/* ---------------------------------------------------------------- tabla */

const SEASONAL_LABELS = { null: "Estacionales: todas", true: "Solo estacionales", false: "Sin estacionales" };

function renderRiskFilters() {
  const riesgo = ["alto", "medio", "bajo", "no_churn"]
    .map((k) => `<button class="chip ${state.risk.includes(k) ? "on" : ""}" data-risk="${k}">${RISK_LABELS[k]}</button>`)
    .join("");

  // Ciclo de tres estados: todas -> solo estacionales -> sin estacionales.
  const seasonal = `<button class="chip ${state.seasonal !== null ? "on" : ""}" data-seasonal="1"
    title="Las cuentas con patrón de pausas pueden estar cerrando por temporada, no dándose de baja.">
    ${SEASONAL_LABELS[String(state.seasonal)]}</button>`;

  $("risk-filters").innerHTML = riesgo + seasonal;

  $("risk-filters").querySelectorAll("[data-risk]").forEach((chip) => {
    chip.onclick = () => {
      const key = chip.dataset.risk;
      state.risk = state.risk.includes(key)
        ? state.risk.filter((r) => r !== key)
        : [...state.risk, key];
      state.page = 1;
      renderRiskFilters();
      loadAccounts();
    };
  });

  $("risk-filters").querySelector("[data-seasonal]").onclick = () => {
    state.seasonal = state.seasonal === null ? true : state.seasonal === true ? false : null;
    state.page = 1;
    renderRiskFilters();
    loadAccounts();
  };
}

function wireControls() {
  let timer;
  $("search").oninput = (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      state.search = e.target.value.trim();
      state.page = 1;
      loadAccounts();
    }, 260);
  };

  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.onclick = () => {
      const key = th.dataset.sort;
      state.ascending = state.sortBy === key ? !state.ascending : false;
      state.sortBy = key;
      state.page = 1;
      loadAccounts();
    };
  });

  $("prev").onclick = () => { if (state.page > 1) { state.page--; loadAccounts(); } };
  $("next").onclick = () => { if (state.page < state.pages) { state.page++; loadAccounts(); } };

  $("drawer-close").onclick = closeDrawer;
  $("scrim").onclick = closeDrawer;
  document.onkeydown = (e) => { if (e.key === "Escape") closeDrawer(); };
}

async function loadAccounts() {
  const data = await api("/api/v1/accounts", {
    risk: state.risk,
    seasonal: state.seasonal === null ? undefined : state.seasonal,
    search: state.search,
    sort_by: state.sortBy,
    ascending: state.ascending,
    page: state.page,
    size: state.size,
  });

  state.pages = data.pages;
  $("page-info").textContent =
    `${num(data.total)} cuentas · página ${data.page} de ${data.pages}`;
  $("prev").disabled = data.page <= 1;
  $("next").disabled = data.page >= data.pages;

  if (!data.items.length) {
    $("accounts-body").innerHTML =
      `<tr><td colspan="6" class="empty">No hay cuentas que cumplan el filtro.</td></tr>`;
    return;
  }

  $("accounts-body").innerHTML = data.items.map(rowHtml).join("");
  $("accounts-body").querySelectorAll("tr[data-id]").forEach((tr) => {
    tr.onclick = () => openDrawer(Number(tr.dataset.id));
  });
}

function rowHtml(a) {
  const color = RISK_COLORS[a.risk_category];
  return `
    <tr data-id="${a.id}">
      <td>
        <span class="acct-name" title="${escapeHtml(a.nombre || "")}">${escapeHtml(a.nombre || "(sin nombre)")}</span>
        <span class="acct-id">#${a.id}</span>
      </td>
      <td><span class="mono">${escapeHtml(a.plan || "—")}</span></td>
      <td class="num">
        <span class="prob">
          <span class="prob-track"><span class="prob-fill" style="width:${a.churn_probability * 100}%;background:${color}"></span></span>
          ${pct(a.churn_probability, 1)}
        </span>
      </td>
      <td class="num">${money(a.monthly_revenue)}</td>
      <td class="num"><strong>${money(a.revenue_at_risk)}</strong></td>
      <td>
        <span class="tag tag-${a.risk_category}">${RISK_LABELS[a.risk_category]}</span>
        ${a.posible_estacional ? '<span class="tag tag-estacional" title="Patrón de pausas de temporada: su baja puede ser un cierre estacional.">estacional</span>' : ""}
      </td>
    </tr>`;
}

/* ------------------------------------------------------ detalle (drawer) */

async function openDrawer(accountId) {
  $("drawer").classList.add("on");
  $("scrim").classList.add("on");
  $("drawer-content").innerHTML = `
    <div class="skeleton" style="height:28px;width:60%"></div>
    <div class="skeleton" style="height:88px;margin-top:20px"></div>
    <div class="skeleton" style="height:200px;margin-top:20px"></div>`;

  try {
    const a = await api(`/api/v1/accounts/${accountId}`);
    $("drawer-content").innerHTML = detailHtml(a);
  } catch (error) {
    $("drawer-content").innerHTML = `<p class="empty">No se pudo cargar la cuenta: ${error.message}</p>`;
  }
}

function closeDrawer() {
  $("drawer").classList.remove("on");
  $("scrim").classList.remove("on");
}


/** Contexto para CX: si la cuenta ya pausó antes, su baja puede ser un cierre de
 *  temporada y no una pérdida. El modelo no lo sabe — esto viene del historial. */
function avisoEstacional(a) {
  if (!a.posible_estacional) return "";
  const mes = a.mes_de_pausa_habitual
    ? ` Suele pausar en ${MESES[a.mes_de_pausa_habitual - 1]}.`
    : "";
  const veces = a.pausas_historicas === 1 ? "una vez" : `${a.pausas_historicas} veces`;
  return `
    <div class="aviso-estacional">
      <span>◷</span>
      <div><strong>Posible negocio de temporada.</strong> Esta cuenta ya se dio de baja y
      volvió ${veces}.${mes} Su desaparición puede ser un cierre estacional y no una
      pérdida: conviene confirmarlo antes de gastar una acción de retención.</div>
    </div>`;
}

function detailHtml(a) {
  const heat = (a.top_features || []).map(heatRow).join("");
  const plan = planHtml(a);

  return `
    <div class="d-head">
      <h2>${escapeHtml(a.nombre || "(sin nombre)")}</h2>
      <p class="d-sub">
        Cuenta #${a.id} · ${escapeHtml(a.pais || "—")} · estado ${escapeHtml(a.estado || "—")}
        <span class="tag tag-${a.risk_category}" style="margin-left:8px">${RISK_LABELS[a.risk_category]}</span>
      </p>
    </div>

    <div class="d-stats">
      <div class="d-stat">
        <div class="d-stat-label">Prob. de baja</div>
        <div class="d-stat-value" style="color:${RISK_COLORS[a.risk_category]}">${pct(a.churn_probability, 1)}</div>
      </div>
      <div class="d-stat">
        <div class="d-stat-label">Revenue / mes</div>
        <div class="d-stat-value">${money(a.monthly_revenue)}</div>
      </div>
      <div class="d-stat">
        <div class="d-stat-label">En riesgo</div>
        <div class="d-stat-value">${money(a.revenue_at_risk)}</div>
      </div>
    </div>

    ${avisoEstacional(a)}
    ${a.diagnostico ? `<div class="d-narrative">${escapeHtml(a.diagnostico)}</div>` : ""}

    <div class="d-section-title">Qué explica esta predicción</div>
    <div class="heat">${heat || '<p class="empty">Sin contribuciones registradas.</p>'}</div>

    <div class="d-section-title">Plan y revenue</div>
    ${plan}`;
}

function heatRow(c) {
  const color = heatColor(c);
  const percentile = c.percentile === null || c.percentile === undefined ? null : c.percentile;
  const width = percentile === null ? 0 : percentile;
  const value = c.value === null || c.value === undefined ? "—" : c.value;

  const meta = percentile === null
    ? `valor ${value}`
    : `valor ${value} · percentil ${percentile.toFixed(0)} de la base`;

  return `
    <div class="heat-row" style="--heat:${color}">
      <div>
        <div class="heat-label">${escapeHtml(c.label)}</div>
        <div class="heat-meta">${escapeHtml(meta)} · ${c.direction} el riesgo</div>
      </div>
      <div class="heat-bar-wrap" title="La barra es el percentil de la cuenta; el número, el aporte al riesgo (SHAP).">
        <div class="heat-track"><div class="heat-fill" style="width:${width}%;background:${color}"></div></div>
        <div class="heat-val">${c.shap_value > 0 ? "+" : ""}${c.shap_value.toFixed(2)}</div>
      </div>
    </div>`;
}

function planHtml(a) {
  let items = [];
  try {
    items = JSON.parse(a.revenue_desglose || "[]");
  } catch (_) { /* el desglose es opcional */ }

  const rows = items
    .map((i) => `
      <div class="d-plan-row">
        <span>${i.kind === "plan" ? "Plan" : "Módulo"} <span class="mono">${escapeHtml(i.code)}</span></span>
        <span>${i.price === null ? "sin precio" : money(i.price)}</span>
      </div>`)
    .join("");

  return `
    <div class="d-plan">
      <div class="d-plan-row"><span>${escapeHtml(a.plan_descripcion || a.plan || "—")}</span><span></span></div>
      ${rows}
      <div class="d-plan-total">
        <span>Total mensual</span>
        <span>${money(a.monthly_revenue)} ${state.currency}</span>
      </div>
    </div>`;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = String(text ?? "");
  return div.innerHTML;
}

boot();
