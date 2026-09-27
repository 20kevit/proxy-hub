/* ProxyHub panel — vanilla JS, no secrets stored (session lives in httpOnly cookie). */
let CSRF = "";
let NODES = [];

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", "x-csrf-token": CSRF, ...(opts.headers || {}) },
    ...opts,
  });
  if (res.status === 401) { showLogin(); throw new Error("unauthenticated"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || ("خطا " + res.status));
  return data;
}

function getCsrfFromCookie() {
  const m = document.cookie.match(/(?:^|; )ph_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

function showLogin() {
  $("loginView").classList.remove("hidden");
  $("appView").classList.add("hidden");
  $("nav").classList.add("hidden");
}
function showApp() {
  $("loginView").classList.add("hidden");
  $("appView").classList.remove("hidden");
  $("nav").classList.remove("hidden");
}

$("loginBtn").onclick = async () => {
  $("loginErr").textContent = "";
  try {
    const r = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username: $("username").value, password: $("password").value }),
    });
    CSRF = r.csrf || getCsrfFromCookie();
    $("password").value = "";
    showApp();
    await refreshAll();
  } catch (e) { $("loginErr").textContent = "ورود ناموفق بود."; }
};
$("logoutBtn").onclick = async () => { try { await api("/api/auth/logout", { method: "POST" }); } catch {} showLogin(); };

document.querySelectorAll("#nav button[data-tab]").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("#nav button[data-tab]").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    document.querySelectorAll(".tab").forEach((t) => t.classList.add("hidden"));
    $("tab-" + b.dataset.tab).classList.remove("hidden");
  };
});

function dot(cls) { return `<span class="dot ${cls}"></span>`; }
function hdot(h) { return h === "healthy" ? dot("ok") : h === "unhealthy" ? dot("bad") : dot("warn"); }

async function loadDash() {
  const s = await api("/api/status");
  const a = s.active_node;
  $("dashCards").innerHTML = `
    <div class="stat"><div class="v">${s.running ? dot("ok") + " فعال" : dot("bad") + " قطع"}</div><div class="k">وضعیت ProxyHub</div></div>
    <div class="stat"><div class="v">${a ? a.name : "—"}</div><div class="k">نود فعال (${s.active_core || "—"})</div></div>
    <div class="stat"><div class="v" dir="ltr">${a && a.latency_ms != null ? a.latency_ms + " ms" : "—"}</div><div class="k">تأخیر</div></div>
    <div class="stat"><div class="v" dir="ltr">${s.exit_ip || "—"}</div><div class="k">IP خروجی</div></div>
    <div class="stat"><div class="v">${s.nodes.healthy} / ${s.nodes.total}</div><div class="k">نودهای سالم</div></div>
    <div class="stat"><div class="v">${s.subscription.last_ok_at ? "به‌روز" : "نامشخص"}</div><div class="k">اشتراک</div></div>
    <div class="stat"><div class="v" dir="ltr">${s.resources.cpu_pct}% / ${s.resources.mem_pct}%</div><div class="k">CPU / RAM</div></div>
    <div class="stat"><div class="v">${s.system_xray.active ? dot("ok") : dot("bad")}</div><div class="k">Xray سیستمی</div></div>`;
}
$("quickTest").onclick = async () => {
  $("dashMsg").textContent = "در حال تست…";
  try {
    const nodes = await api("/api/nodes");
    const a = nodes.find((n) => n.is_active);
    if (!a) { $("dashMsg").textContent = "نود فعالی وجود ندارد."; return; }
    const r = await api(`/api/nodes/${a.id}/test`, { method: "POST" });
    $("dashMsg").textContent = r.ok ? `سالم (${r.node.latency_ms} ms)` : `ناسالم: ${r.node.last_error}`;
    await loadDash();
  } catch (e) { $("dashMsg").textContent = e.message; }
};
$("quickUpdate").onclick = async () => {
  $("dashMsg").textContent = "در حال به‌روزرسانی اشتراک…";
  try { const r = await api("/api/subscription/update", { method: "POST" }); $("dashMsg").textContent = `انجام شد: ${r.total} نود.`; await refreshAll(); }
  catch (e) { $("dashMsg").textContent = e.message; }
};
$("quickHealth").onclick = async () => { $("dashMsg").textContent = "بررسی سلامت توسط زمان‌بند انجام می‌شود؛ برای تست تکی از تب نودها استفاده کنید."; };

function renderNodes() {
  const q = ($("nodeSearch").value || "").toLowerCase();
  const f = $("nodeFilter").value;
  const list = NODES.filter((n) => {
    if (f === "healthy" && n.health !== "healthy") return false;
    if (f === "unhealthy" && n.health !== "unhealthy") return false;
    if (f === "active" && !n.is_active) return false;
    if ((f === "vless" || f === "hysteria2") && n.protocol !== f) return false;
    if (q && !(n.name + " " + n.server + " " + n.protocol).toLowerCase().includes(q)) return false;
    return true;
  });
  $("nodeList").innerHTML = list.map((n) => `
    <div class="node ${n.health}">
      <div class="head"><strong>${n.is_active ? "⭐ " : ""}${escapeHtml(n.name)}</strong>${hdot(n.health)}</div>
      <div class="meta" dir="ltr">${n.protocol} · ${escapeHtml(n.server)}:${n.port} · ${n.transport || ""} ${n.security || ""}</div>
      <div class="meta">تأخیر: <span dir="ltr">${n.latency_ms != null ? n.latency_ms + " ms" : "—"}</span> · آخرین خطا: ${escapeHtml(n.last_error || "—")} ${n.removed_upstream ? "· <span class='warn'>حذف‌شده از اشتراک</span>" : ""} ${n.enabled ? "" : "· <span class='warn'>غیرفعال</span>"}</div>
      <div class="actions">
        <button onclick="nodeAct(${n.id},'activate')">فعال‌سازی</button>
        <button onclick="nodeAct(${n.id},'test')">تست</button>
        <button onclick="nodeToggle(${n.id},${n.enabled})">${n.enabled ? "غیرفعال" : "فعال"}</button>
        <button onclick="nodeRename(${n.id})">تغییرنام</button>
      </div>
    </div>`).join("") || `<div class="card muted">نودی یافت نشد.</div>`;
}
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
$("nodeSearch").oninput = renderNodes; $("nodeFilter").onchange = renderNodes;

async function loadNodes() { NODES = await api("/api/nodes"); renderNodes(); }
async function nodeAct(id, act) {
  try { await api(`/api/nodes/${id}/${act}`, { method: "POST" }); await loadNodes(); await loadDash(); }
  catch (e) { alert(e.message); }
}
async function nodeToggle(id, enabled) {
  try { await api(`/api/nodes/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: !enabled }) }); await loadNodes(); }
  catch (e) { alert(e.message); }
}
async function nodeRename(id) {
  const name = prompt("نام جدید:");
  if (!name) return;
  try { await api(`/api/nodes/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }); await loadNodes(); }
  catch (e) { alert(e.message); }
}

async function loadSub() {
  const s = await api("/api/subscription");
  $("subInfo").innerHTML = `
    <table>
      <tr><td>وضعیت</td><td>${s.configured ? "تنظیم شده" : "<span class='bad'>تنظیم نشده</span>"}</td></tr>
      <tr><td>URL (ماسک‌شده)</td><td dir="ltr">${escapeHtml(s.url_masked || "—")}</td></tr>
      <tr><td>آخرین به‌روزرسانی موفق</td><td dir="ltr">${s.last_ok_at || "—"}</td></tr>
      <tr><td>آخرین خطا</td><td>${escapeHtml(s.last_error || "—")}</td></tr>
      <tr><td>تعداد نودها</td><td>${s.node_count}</td></tr>
      <tr><td>فاصله به‌روزرسانی</td><td dir="ltr">${s.update_interval}s</td></tr>
    </table>`;
}
$("subUpdateBtn").onclick = async () => {
  try { const r = await api("/api/subscription/update", { method: "POST" }); alert(`انجام شد: ${r.total} نود`); await refreshAll(); }
  catch (e) { alert(e.message); }
};
$("subSaveBtn").onclick = async () => {
  const url = $("subUrl").value.trim();
  if (!url) return;
  try { await api("/api/subscription", { method: "PUT", body: JSON.stringify({ url }) }); $("subUrl").value = ""; await loadSub(); alert("ذخیره شد."); }
  catch (e) { alert(e.message); }
};

async function loadRoutes() {
  const r = await api("/api/routing");
  $("routeList").innerHTML = `<table>${r.rules.map((x) =>
    `<tr><td>${escapeHtml(x.app)}</td><td>${x.route === "proxy" ? "→ پروکسی" : "→ مستقیم"}</td>
     <td><button onclick="delRoute(${x.id})">حذف</button></td></tr>`).join("")}</table>`;
  $("proxyEnv").textContent = r.proxy_env;
}
async function delRoute(id) { try { await api(`/api/routing/${id}`, { method: "DELETE" }); await loadRoutes(); } catch (e) { alert(e.message); } }
$("addRouteBtn").onclick = async () => {
  const app = $("newApp").value.trim();
  if (!app) return;
  try { await api("/api/routing", { method: "POST", body: JSON.stringify({ app, route: $("newRoute").value }) }); $("newApp").value = ""; await loadRoutes(); }
  catch (e) { alert(e.message); }
};

async function loadSystem() {
  const s = await api("/api/system");
  $("sysInfo").innerHTML = `<table>
    <tr><td>Xray سیستمی</td><td dir="ltr">${s.services.system_xray.state}</td></tr>
    <tr><td>Xray مدیریت‌شده</td><td dir="ltr">${s.services.managed_xray.state}</td></tr>
    <tr><td>API</td><td dir="ltr">${escapeHtml(s.listeners.api)}</td></tr>
    <tr><td>SOCKS مدیریت‌شده</td><td dir="ltr">${escapeHtml(s.listeners.managed_socks)}</td></tr>
    <tr><td>HTTP مدیریت‌شده</td><td dir="ltr">${escapeHtml(s.listeners.managed_http)}</td></tr>
    <tr><td>sing-box</td><td>${s.cores["singbox"].installed ? "نصب شده" : "نصب نشده (فقط برای Hysteria2 لازم است)"}</td></tr>
  </table>`;
  $("eventList").innerHTML = `<table>${s.recent_events.map((e) =>
    `<tr><td dir="ltr">${e.at || ""}</td><td>${escapeHtml(e.actor)}</td><td>${escapeHtml(e.action)}</td><td>${escapeHtml(e.detail)}</td></tr>`).join("")}</table>`;
}

async function loadSettings() {
  const s = await api("/api/settings");
  $("settingsForm").innerHTML = `
    <label>فاصله به‌روزرسانی اشتراک (ثانیه)<input id="s_sub" type="number" dir="ltr" value="${s.sub_update_interval}"></label>
    <label>فاصله بررسی سلامت (ثانیه)<input id="s_health" type="number" dir="ltr" value="${s.health_interval}"></label>
    <label>آدرس تست سلامت<input id="s_url" dir="ltr" value="${escapeHtml(s.health_url)}"></label>
    <label>حالت failover<select id="s_mode"><option value="auto">خودکار</option><option value="manual">دستی</option></select></label>
    <label>آستانه خطا<input id="s_thr" type="number" dir="ltr" value="${s.failover_threshold}"></label>
    <label>کول‌داون (ثانیه)<input id="s_cd" type="number" dir="ltr" value="${s.failover_cooldown}"></label>`;
  $("s_mode").value = s.failover_mode;
}
$("saveSettingsBtn").onclick = async () => {
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify({
      sub_update_interval: +$("s_sub").value, health_interval: +$("s_health").value,
      health_url: $("s_url").value, failover_mode: $("s_mode").value,
      failover_threshold: +$("s_thr").value, failover_cooldown: +$("s_cd").value,
    })});
    $("settingsMsg").textContent = "ذخیره شد.";
  } catch (e) { $("settingsMsg").textContent = e.message; }
};

async function refreshAll() { await loadDash(); await loadNodes(); await loadSub(); await loadRoutes(); await loadSystem(); await loadSettings(); }

(async () => {
  CSRF = getCsrfFromCookie();
  try { await api("/api/auth/me"); showApp(); await refreshAll(); }
  catch { showLogin(); }
})();
