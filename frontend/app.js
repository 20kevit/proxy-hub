/* ProxyHub panel — vanilla JS. No secrets in storage; session in httpOnly cookie. */
let CSRF = "", NODES = [], RULES = [], CLIENTS = [], DEFROUTE = "proxy", GW = {};
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "include",
    headers: { "Content-Type": "application/json", "x-csrf-token": CSRF, ...((opts.headers) || {}) }, ...opts });
  if (res.status === 401) { showLogin(); throw new Error("نشست منقضی شد؛ دوباره وارد شوید."); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || ("خطا " + res.status));
  return data;
}
function toast(msg, err = false) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : ""); t.textContent = msg;
  $("toasts").appendChild(t); setTimeout(() => t.remove(), 3500);
}
function copyText(txt, label = "کپی شد ✓") {
  (navigator.clipboard ? navigator.clipboard.writeText(txt) : Promise.reject())
    .then(() => toast(label)).catch(() => { prompt("کپی دستی:", txt); });
}
function getCsrf() { const m = document.cookie.match(/(?:^|; )ph_csrf=([^;]+)/); return m ? decodeURIComponent(m[1]) : ""; }
function showLogin() { $("loginView").classList.remove("hidden"); $("appView").classList.add("hidden"); $("nav").classList.add("hidden"); }
function showApp() { $("loginView").classList.add("hidden"); $("appView").classList.remove("hidden"); $("nav").classList.remove("hidden"); }

$("loginBtn").onclick = async () => {
  $("loginErr").textContent = "";
  try {
    const r = await api("/api/auth/login", { method: "POST",
      body: JSON.stringify({ username: $("username").value, password: $("password").value }) });
    CSRF = r.csrf || getCsrf(); $("password").value = ""; showApp(); await refreshAll();
  } catch (e) { $("loginErr").textContent = "ورود ناموفق بود."; }
};
$("logoutBtn").onclick = async () => { try { await api("/api/auth/logout", { method: "POST" }); } catch {} showLogin(); };
document.querySelectorAll("#nav button[data-tab]").forEach((b) => { b.onclick = () => {
  document.querySelectorAll("#nav button[data-tab]").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  document.querySelectorAll(".tab").forEach((t) => t.classList.add("hidden"));
  $("tab-" + b.dataset.tab).classList.remove("hidden"); }; });

const dot = (c) => `<span class="dot ${c}"></span>`;
const hdot = (h) => h === "HEALTHY" ? dot("ok") : h === "UNHEALTHY" ? dot("bad") : h === "DEGRADED" ? dot("warn") : dot("off");
const FA_HEALTH = { HEALTHY: "سالم", DEGRADED: "ناپایدار", UNHEALTHY: "ناسالم", UNKNOWN: "نامشخص", DISABLED: "غیرفعال" };

/* ---------- dashboard ---------- */
async function loadDash() {
  const s = await api("/api/status");
  const a = s.active_node; GW = s.gateway || {};
  $("dashCards").innerHTML = `
    <div class="stat"><div class="v">${s.running ? dot("ok") + " فعال" : dot("bad") + " قطع"}</div><div class="k">ProxyHub</div></div>
    <div class="stat"><div class="v">${a ? esc(a.name) : "—"}</div><div class="k">نود فعال (${esc(s.active_core || "—")})</div></div>
    <div class="stat"><div class="v" dir="ltr">${a && a.latency_ms != null ? a.latency_ms + " ms" : "—"}</div><div class="k">تأخیر</div></div>
    <div class="stat"><div class="v" dir="ltr">${esc(s.exit_ip || "—")}</div><div class="k">IP خروجی</div></div>
    <div class="stat"><div class="v">${s.nodes.healthy} / ${s.nodes.total}</div><div class="k">نودهای سالم</div></div>
    <div class="stat"><div class="v">${s.subscription.last_ok_at ? "به‌روز" : "نامشخص"}</div><div class="k">اشتراک</div></div>
    <div class="stat"><div class="v">${GW.enabled ? dot("ok") + " روشن" : dot("off") + " خاموش"}</div><div class="k">gateway عمومی</div></div>
    <div class="stat"><div class="v" dir="ltr">${s.resources.cpu_pct}%</div><div class="k">CPU / RAM ${s.resources.mem_pct}%</div></div>
    <div class="stat"><div class="v">${s.system_xray.active ? dot("ok") : dot("bad")}</div><div class="k">Xray سیستمی</div></div>`;
  $("gwCard").innerHTML = GW.enabled
    ? `<table><tr><td>وضعیت</td><td>${dot("ok")} فعال روی <span dir="ltr">${esc(GW.bind)}:${GW.socks_port} (SOCKS) / ${GW.http_port} (HTTP)</span></td></tr>
       <tr><td>مسیر پیش‌فرض</td><td><span class="pill ${GW.default_route}">${GW.default_route}</span></td></tr></table>`
    : `<p class="muted">خاموش است (پیش‌فرض امن). فعال‌سازی از تب تنظیمات.</p>`;
  $("foCard").innerHTML = `<table>
    <tr><td>حالت</td><td>${s.failover.mode === "auto" ? "خودکار" : "دستی"}</td></tr>
    <tr><td>نود قبلی</td><td>${esc(s.failover.previous_node || "—")}</td></tr>
    <tr><td>آخرین دلیل</td><td>${esc(s.failover.last_reason || "—")}</td></tr>
    <tr><td>زمان</td><td dir="ltr">${esc(s.failover.last_at || "—")}</td></tr></table>`;
}
$("quickTest").onclick = async () => {
  $("dashMsg").textContent = "در حال تست…";
  try { const ns = await api("/api/nodes"); const a = ns.find((n) => n.is_active);
    if (!a) { $("dashMsg").textContent = "نود فعالی وجود ندارد."; return; }
    const r = await api(`/api/nodes/${a.id}/test`, { method: "POST" });
    $("dashMsg").textContent = r.ok ? `سالم (${r.node.latency_ms} ms)` : `ناسالم: ${r.node.last_error}`;
    await loadDash();
  } catch (e) { $("dashMsg").textContent = e.message; }
};
$("quickUpdate").onclick = async () => {
  $("dashMsg").textContent = "در حال به‌روزرسانی…";
  try { const r = await api("/api/subscription/update", { method: "POST" });
    $("dashMsg").textContent = `انجام شد: ${r.total} نود.`; toast("اشتراک به‌روز شد"); await refreshAll();
  } catch (e) { $("dashMsg").textContent = e.message; }
};

/* ---------- nodes ---------- */
function renderNodes() {
  const q = ($("nodeSearch").value || "").toLowerCase(), f = $("nodeFilter").value, sort = $("nodeSort").value;
  let list = NODES.filter((n) => {
    if (f === "active" && !n.is_active) return false;
    else if (f && f !== "active" && n.protocol !== f && (n.health_state || n.health) !== f) return false;
    return !q || (n.name + " " + n.server + " " + n.protocol).toLowerCase().includes(q);
  });
  if (sort === "latency") list = [...list].sort((a, b) => (a.latency_ms ?? 1e9) - (b.latency_ms ?? 1e9));
  else if (sort === "name") list = [...list].sort((a, b) => a.name.localeCompare(b.name, "fa"));
  else list = [...list].sort((a, b) => (b.is_active - a.is_active) || (a.latency_ms ?? 1e9) - (b.latency_ms ?? 1e9));
  $("nodeList").innerHTML = list.map((n) => { const hs = n.health_state || n.health; return `
    <div class="node ${hs} ${n.is_active ? "active" : ""}">
      <div class="head"><strong>${n.is_active ? "⭐ " : ""}${esc(n.name)}</strong><span>${hdot(hs)} ${FA_HEALTH[hs] || hs}</span></div>
      <div class="meta" dir="ltr">${esc(n.protocol)} · ${esc(n.server)}:${n.port} · ${esc(n.transport || "")} ${esc(n.security || "")}</div>
      <div class="meta">تأخیر: <span dir="ltr">${n.latency_ms != null ? n.latency_ms + " ms" : "—"}</span> · خطاهای پیاپی: ${n.fail_count || 0} · موفق: ${n.success_count || 0} · آخرین خطا: ${esc(n.last_error || "—")}${n.egress_ok ? ' · <span class="ok">egress ✓</span>' : ""}${n.removed_upstream ? ' · <span class="warn">حذف‌شده از اشتراک</span>' : ""}${n.enabled ? "" : ' · <span class="warn">غیرفعال</span>'}</div>
      <div class="actions"><button onclick="nodeAct(${n.id},'activate')">فعال‌سازی</button><button onclick="nodeAct(${n.id},'test')">تست</button>
      <button onclick="nodeToggle(${n.id},${n.enabled})">${n.enabled ? "غیرفعال" : "فعال"}</button>
      <button onclick="nodeRename(${n.id})">تغییرنام</button></div>
    </div>`; }).join("") || `<div class="card muted">نودی یافت نشد.</div>`;
}
$("nodeSearch").oninput = renderNodes; $("nodeFilter").onchange = renderNodes; $("nodeSort").onchange = renderNodes;
async function loadNodes() { NODES = await api("/api/nodes"); renderNodes(); }
async function nodeAct(id, act) { try { await api(`/api/nodes/${id}/${act}`, { method: "POST" }); toast(act === "activate" ? "نود فعال شد" : "تست انجام شد"); await loadNodes(); await loadDash(); } catch (e) { toast(e.message, true); } }
async function nodeToggle(id, en) { try { await api(`/api/nodes/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: !en }) }); await loadNodes(); } catch (e) { toast(e.message, true); } }
async function nodeRename(id) { const name = prompt("نام جدید:"); if (!name) return;
  try { await api(`/api/nodes/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }); await loadNodes(); } catch (e) { toast(e.message, true); } }

/* ---------- traffic rules ---------- */
const FA_MT = { domain_exact: "دامنه دقیق", domain_suffix: "پسوند دامنه", domain_wildcard: "wildcard", cidr: "CIDR", ip: "IP", port: "پورت" };
async function loadRules() {
  const r = await api("/api/rules"); RULES = r.rules; DEFROUTE = r.default_route;
  $("defRoute").innerHTML = `<span class="pill ${DEFROUTE}">${DEFROUTE}</span>`;
  const names = CLIENTS.map((c) => c.username);
  $("ruleClient").innerHTML = `<option value="all">همه</option>` + names.map((u) => `<option value="${esc(u)}">${esc(u)}</option>`).join("");
  $("simClient").innerHTML = `<option value="all">همه کلاینت‌ها</option>` + names.map((u) => `<option value="${esc(u)}">${esc(u)}</option>`).join("");
  $("tab-routing tbody").innerHTML = RULES.map((x) => `<tr>
    <td>${x.priority}</td><td><b>${FA_MT[x.match_type] || x.match_type}</b> <span dir="ltr">${esc(x.pattern)}</span>${x.port ? ` <span dir="ltr">:${esc(x.port)}</span>` : ""}${x.network !== "any" ? ` (${x.network})` : ""}${x.note ? `<br><small class="muted">${esc(x.note)}</small>` : ""}</td>
    <td><span class="pill ${x.action}">${x.action}</span></td><td>${esc(x.client_scope)}</td>
    <td>${x.enabled ? dot("ok") : dot("off")}</td>
    <td><button class="small" onclick="ruleMove(${x.id},'up')">↑</button><button class="small" onclick="ruleMove(${x.id},'down')">↓</button>
    <button class="small" onclick="ruleEdit(${x.id})">ویرایش</button><button class="small" onclick="ruleDup(${x.id})">تکثیر</button>
    <button class="small danger" onclick="ruleDel(${x.id})">حذف</button></td></tr>`).join("");
}
function openRuleDlg(rule) {
  $("ruleDlgTitle").textContent = rule ? "ویرایش قانون" : "قانون جدید";
  $("ruleId").value = rule?.id || ""; $("ruleType").value = rule?.match_type || "domain_suffix";
  $("rulePattern").value = rule?.pattern || ""; $("rulePort").value = rule?.port || "";
  $("ruleNet").value = rule?.network || "any"; $("ruleAction").value = rule?.action || "proxy";
  $("ruleClient").value = rule?.client_scope || "all"; $("rulePrio").value = rule?.priority ?? 100;
  $("ruleNote").value = rule?.note || ""; $("ruleEnabled").checked = rule ? !!rule.enabled : true;
  $("ruleErr").textContent = ""; $("ruleDialog").showModal();
}
$("addRuleBtn").onclick = () => openRuleDlg(null);
$("ruleSave").onclick = async (e) => {
  e.preventDefault();
  const id = $("ruleId").value;
  const body = { match_type: $("ruleType").value, pattern: $("rulePattern").value, port: $("rulePort").value,
    network: $("ruleNet").value, action: $("ruleAction").value, client_scope: $("ruleClient").value,
    priority: +$("rulePrio").value || 100, note: $("ruleNote").value, enabled: $("ruleEnabled").checked };
  try { id ? await api(`/api/rules/${id}`, { method: "PUT", body: JSON.stringify(body) })
          : await api("/api/rules", { method: "POST", body: JSON.stringify(body) });
    $("ruleDialog").close(); toast("قانون ذخیره شد"); await loadRules();
  } catch (err) { $("ruleErr").textContent = err.message; }
};
async function ruleEdit(id) { openRuleDlg(RULES.find((r) => r.id === id)); }
async function ruleDel(id) { if (!confirm("این قانون حذف شود؟")) return;
  try { await api(`/api/rules/${id}`, { method: "DELETE" }); toast("حذف شد"); await loadRules(); } catch (e) { toast(e.message, true); } }
async function ruleDup(id) { try { await api(`/api/rules/${id}/duplicate`, { method: "POST" }); toast("تکثیر شد (غیرفعال)"); await loadRules(); } catch (e) { toast(e.message, true); } }
async function ruleMove(id, dir) { try { await api(`/api/rules/${id}/move`, { method: "POST", body: JSON.stringify({ direction: dir }) }); await loadRules(); } catch (e) { toast(e.message, true); } }
$("defRouteBtn").onclick = async () => {
  const v = prompt("مسیر پیش‌فرض (direct/proxy/block):", DEFROUTE);
  if (!v) return;
  try { await api("/api/routing/default", { method: "PUT", body: JSON.stringify({ default_route: v.trim().toLowerCase() }) }); toast("مسیر پیش‌فرض ذخیره شد"); await loadRules(); }
  catch (e) { toast(e.message, true); }
};
$("simBtn").onclick = async () => {
  const port = $("simPort").value ? +$("simPort").value : null;
  try { const r = await api("/api/routing/simulate", { method: "POST", body: JSON.stringify({
      domain: $("simDomain").value, ip: $("simIp").value, port, client: $("simClient").value }) });
    $("simResult").innerHTML = `قانون منطبق: <b dir="ltr">${esc(r.matched_rule)}</b><br>تصمیم: <span class="pill ${r.decision}">${r.decision}</span> · نود فعال: ${esc(r.active_node || "—")}${r.is_default ? " · (پیش‌فرض)" : ""}`;
  } catch (e) { $("simResult").textContent = e.message; }
};
/* server-side app rules (legacy, kept) */
async function loadAppRules() {
  const r = await api("/api/routing");
  $("routeList").innerHTML = `<table>${r.rules.map((x) => `<tr><td>${esc(x.app)}</td><td>${x.route === "proxy" ? "→ پروکسی" : "→ مستقیم"}</td>
    <td><button class="small danger" onclick="delAppRoute(${x.id})">حذف</button></td></tr>`).join("")}</table>`;
  $("proxyEnv").textContent = r.proxy_env;
}
async function delAppRoute(id) { try { await api(`/api/routing/${id}`, { method: "DELETE" }); await loadAppRules(); } catch (e) { toast(e.message, true); } }
$("addRouteBtn").onclick = async () => { const app = $("newApp").value.trim(); if (!app) return;
  try { await api("/api/routing", { method: "POST", body: JSON.stringify({ app, route: $("newRoute").value }) }); $("newApp").value = ""; await loadAppRules(); }
  catch (e) { toast(e.message, true); } };

/* ---------- devices ---------- */
async function loadClients() {
  CLIENTS = await api("/api/clients");
  const s = await api("/api/status"); GW = s.gateway || {};
  $("gwInfo").innerHTML = GW.enabled
    ? `gateway فعال: <span dir="ltr">SOCKS ${esc(GW.bind)}:${GW.socks_port} · HTTP ${GW.http_port}</span> — روی دستگاه: سرور <b dir="ltr">194.60.231.172</b>`
    : `<span class="warn">gateway خاموش است.</span> ابتدا از تب تنظیمات فعالش کنید (برای تست لوکال روی 127.0.0.1 بماند).`;
  $("clientList").innerHTML = CLIENTS.map((c) => `
    <div class="client"><div class="head"><strong>${esc(c.name)}</strong>
      <span>${c.usable ? dot("ok") + " فعال" : dot("bad") + " " + esc(c.status_detail || "غیرفعال")}</span></div>
      <div class="meta muted">نام کاربری: <b dir="ltr">${esc(c.username)}</b> · انقضا: <span dir="ltr">${esc(c.expires_at || "—")}</span> · آخرین استفاده: <span dir="ltr">${esc(c.last_used_at || "—")}</span>${c.dedicated_port ? ` · پورت اختصاصی: <span dir="ltr">${c.dedicated_port}</span>` : ""}</div>
      <div class="actions row">
        <button class="small" onclick="clientConfig(${c.id})">پیکربندی</button>
        <button class="small" onclick="clientQR(${c.id})">QR</button>
        <button class="small" onclick="clientRotate(${c.id})">چرخش گذرواژه</button>
        <button class="small" onclick="clientToggle(${c.id},${c.enabled})">${c.enabled ? "غیرفعال" : "فعال"}</button>
        <button class="small danger" onclick="clientDel(${c.id})">حذف</button>
      </div><div id="cfg-${c.id}"></div><div id="qr-${c.id}"></div>
    </div>`).join("") || `<p class="muted">دستگاهی ثبت نشده است.</p>`;
}
function clientConfig(id) {
  const c = CLIENTS.find((x) => x.id === id);
  const cfg = `SOCKS5\nServer: 194.60.231.172\nPort: ${GW.socks_port || "?"}\nUsername: ${c.username}\nPassword: (فقط لحظه‌ی ساخت/چرخش)\n\nHTTP\nServer: 194.60.231.172\nPort: ${GW.http_port || "?"}\nUsername: ${c.username}`;
  $("cfg-" + id).innerHTML = `<pre dir="ltr">${esc(cfg)}</pre><button class="small" onclick="copyText(${JSON.stringify("socks5://" + c.username + "@194.60.231.172:" + (GW.socks_port || "?"))})">کپی خلاصه</button>`;
}
function clientQR(id) { $("qr-" + id).innerHTML = `<span class="qr"><img src="/api/clients/${id}/qr.png" alt="QR" width="180"></span>`; }
async function clientRotate(id) { if (!confirm("گذرواژه‌ی جدید تولید و قبلی باطل شود؟")) return;
  try { const r = await api(`/api/clients/${id}/rotate`, { method: "POST" }); showSecret(r.password_once); await loadClients(); }
  catch (e) { toast(e.message, true); } }
async function clientToggle(id, en) { try { await api(`/api/clients/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: !en }) }); await loadClients(); } catch (e) { toast(e.message, true); } }
async function clientDel(id) { if (!confirm("این دستگاه حذف شود؟")) return;
  try { await api(`/api/clients/${id}`, { method: "DELETE" }); toast("حذف شد"); await loadClients(); await loadRules(); } catch (e) { toast(e.message, true); } }
$("addClientBtn").onclick = () => { $("clientName").value = ""; $("clientUser").value = ""; $("clientPass").value = ""; $("clientExp").value = ""; $("clientErr").textContent = ""; $("clientDialog").showModal(); };
$("clientSave").onclick = async (e) => {
  e.preventDefault();
  const body = { name: $("clientName").value };
  if ($("clientUser").value) body.username = $("clientUser").value;
  if ($("clientPass").value) body.password = $("clientPass").value;
  if ($("clientExp").value) body.expires_at = $("clientExp").value;
  try { const r = await api("/api/clients", { method: "POST", body: JSON.stringify(body) });
    $("clientDialog").close(); showSecret(r.password_once); await loadClients(); await loadRules();
  } catch (err) { $("clientErr").textContent = err.message; }
};
function showSecret(pw) { $("secretOnce").textContent = pw; $("secretDialog").showModal(); }
$("secretCopy").onclick = (e) => { e.preventDefault(); copyText($("secretOnce").textContent, "گذرواژه کپی شد"); };

/* ---------- subscription ---------- */
async function loadSub() {
  const s = await api("/api/subscription"), st = s.last_stats || {};
  $("subInfo").innerHTML = `<table>
    <tr><td>وضعیت</td><td>${s.configured ? "تنظیم شده" : "<span class='bad'>تنظیم نشده</span>"}</td></tr>
    <tr><td>URL (ماسک‌شده)</td><td dir="ltr">${esc(s.url_masked || "—")}</td></tr>
    <tr><td>آخرین موفق</td><td dir="ltr">${esc(s.last_ok_at || "—")}</td></tr>
    <tr><td>به‌روزرسانی بعدی</td><td dir="ltr">${esc(s.next_update_at || "—")}</td></tr>
    <tr><td>آخرین خطا</td><td>${esc(s.last_error || "—")}</td></tr>
    <tr><td>نودها</td><td>${s.node_count} (جدید: ${st.added ?? "—"} · حذف‌شده: ${st.removed ?? "—"} · تکراری: ${st.duplicates ?? "—"} · خطا: ${st.errors ?? "—"})</td></tr>
    <tr><td>فاصله به‌روزرسانی</td><td dir="ltr">${s.update_interval}s</td></tr></table>`;
}
$("subUpdateBtn").onclick = async () => { try { const r = await api("/api/subscription/update", { method: "POST" }); toast(`انجام شد: ${r.total} نود`); await refreshAll(); } catch (e) { toast(e.message, true); } };
$("subSaveBtn").onclick = async () => { const url = $("subUrl").value.trim(); if (!url) return;
  try { await api("/api/subscription", { method: "PUT", body: JSON.stringify({ url }) }); $("subUrl").value = ""; await loadSub(); toast("ذخیره شد"); } catch (e) { toast(e.message, true); } };

/* ---------- health ---------- */
async function loadHealth() {
  const ns = NODES.length ? NODES : await api("/api/nodes");
  $("healthList").innerHTML = `<table><thead><tr><th>نود</th><th>وضعیت</th><th>تأخیر</th><th>خطای پیاپی</th><th>آخرین موفق</th><th>آخرین بررسی</th><th></th></tr></thead><tbody>
    ${ns.map((n) => { const hs = n.health_state || n.health; return `<tr><td>${n.is_active ? "⭐ " : ""}${esc(n.name)}</td><td>${hdot(hs)} ${FA_HEALTH[hs] || hs}</td>
    <td dir="ltr">${n.latency_ms != null ? n.latency_ms + " ms" : "—"}</td><td>${n.fail_count || 0}</td>
    <td dir="ltr">${esc(n.last_success || "—")}</td><td dir="ltr">${esc(n.last_checked || "—")}</td>
    <td><button class="small" onclick="nodeAct(${n.id},'test')">تست</button></td></tr>`; }).join("")}</tbody></table>`;
}

/* ---------- logs ---------- */
const LOG_ACTIONS = ["login.ok", "login.failed", "node.activate", "subscription.update", "rule.create", "rule.update", "rule.delete", "client.create", "client.rotate", "settings.update", "routing.create"];
$("logAction").innerHTML = `<option value="">همه رویدادها</option>` + LOG_ACTIONS.map((a) => `<option>${a}</option>`).join("");
async function loadLogs() {
  const q = new URLSearchParams({ limit: 100 });
  if ($("logAction").value) q.set("action", $("logAction").value);
  if ($("logActor").value) q.set("actor", $("logActor").value.trim());
  const rows = await api("/api/audit?" + q.toString());
  $("eventList").innerHTML = `<table><thead><tr><th>زمان</th><th>عامل</th><th>رویداد</th><th>جزئیات</th></tr></thead><tbody>
    ${rows.map((e) => `<tr><td dir="ltr">${esc(e.at || "")}</td><td>${esc(e.actor)}</td><td>${esc(e.action)}</td><td>${esc(e.detail)}</td></tr>`).join("")}</tbody></table>` || `<p class="muted">رویدادی نیست.</p>`;
}
$("logBtn").onclick = loadLogs;

/* ---------- system & settings ---------- */
async function loadSystem() {
  const s = await api("/api/system");
  $("sysInfo").innerHTML = `<table>
    <tr><td>Xray سیستمی</td><td dir="ltr">${esc(s.services.system_xray.state)}</td></tr>
    <tr><td>Xray مدیریت‌شده</td><td dir="ltr">${esc(s.services.managed_xray.state)}</td></tr>
    <tr><td>API</td><td dir="ltr">${esc(s.listeners.api)}</td></tr>
    <tr><td>SOCKS مدیریت‌شده</td><td dir="ltr">${esc(s.listeners.managed_socks)}</td></tr>
    <tr><td>HTTP مدیریت‌شده</td><td dir="ltr">${esc(s.listeners.managed_http)}</td></tr>
    <tr><td>sing-box</td><td>${s.cores["singbox"].installed ? "نصب شده" : "نصب نشده (فقط Hysteria2)"}</td></tr></table>`;
}
async function loadSettings() {
  const s = await api("/api/settings");
  $("settingsForm").innerHTML = `
    <label>فاصله اشتراک (ثانیه)<input id="s_sub" type="number" dir="ltr" value="${s.sub_update_interval}"></label>
    <label>فاصله سلامت (ثانیه)<input id="s_health" type="number" dir="ltr" value="${s.health_interval}"></label>
    <label>آدرس تست سلامت<input id="s_url" dir="ltr" value="${esc(s.health_url)}"></label>
    <label>حالت failover<select id="s_mode"><option value="auto">خودکار</option><option value="manual">دستی</option></select></label>
    <label>آستانه خطا<input id="s_thr" type="number" dir="ltr" value="${s.failover_threshold}"></label>
    <label>کول‌داون (ثانیه)<input id="s_cd" type="number" dir="ltr" value="${s.failover_cooldown}"></label>`;
  $("s_mode").value = s.failover_mode;
  $("gwForm").innerHTML = `
    <label class="check"><input type="checkbox" id="g_en" ${s.gateway_enabled ? "checked" : ""}> فعال‌سازی gateway</label>
    <label>bind<select id="g_bind"><option value="127.0.0.1">127.0.0.1 (لوکال/تست)</option><option value="0.0.0.0">0.0.0.0 (عمومی)</option></select></label>
    <label>پورت SOCKS<input id="g_sp" type="number" dir="ltr" value="${s.public_socks_port}"></label>
    <label>پورت HTTP<input id="g_hp" type="number" dir="ltr" value="${s.public_http_port}"></label>
    <label>مسیر پیش‌فرض<select id="g_def"><option value="proxy">PROXY</option><option value="direct">DIRECT</option><option value="block">BLOCK</option></select></label>`;
  $("g_bind").value = s.gateway_bind; $("g_def").value = s.default_route;
  window._gwOrig = { en: s.gateway_enabled, bind: s.gateway_bind };
}
$("saveSettingsBtn").onclick = async () => {
  try { await api("/api/settings", { method: "PUT", body: JSON.stringify({
    sub_update_interval: +$("s_sub").value, health_interval: +$("s_health").value, health_url: $("s_url").value,
    failover_mode: $("s_mode").value, failover_threshold: +$("s_thr").value, failover_cooldown: +$("s_cd").value }) });
    $("settingsMsg").textContent = "ذخیره شد."; toast("تنظیمات ذخیره شد");
  } catch (e) { $("settingsMsg").textContent = e.message; }
};
$("saveGwBtn").onclick = async () => {
  const en = $("g_en").checked, bind = $("g_bind").value;
  if (en && bind === "0.0.0.0" && !(window._gwOrig.en && window._gwOrig.bind === "0.0.0.0")) {
    if (!confirm("هشدار: gateway روی 0.0.0.0 عمومی می‌شود. فقط کلاینت‌های دارای اعتبار می‌توانند وصل شوند. ادامه؟")) return;
  }
  try { await api("/api/settings", { method: "PUT", body: JSON.stringify({ gateway_enabled: en, gateway_bind: bind,
    public_socks_port: +$("g_sp").value, public_http_port: +$("g_hp").value, default_route: $("g_def").value }) });
    $("gwMsg").textContent = "ذخیره و بازسازی شد."; toast("gateway به‌روز شد"); await loadDash();
  } catch (e) { $("gwMsg").textContent = e.message; }
};

async function refreshAll() { await loadDash(); await loadNodes(); await loadClients(); await loadRules(); await loadSub(); await loadHealth(); await loadLogs(); await loadSystem(); await loadSettings(); }
(async () => { CSRF = getCsrf();
  try { await api("/api/auth/me"); showApp(); await refreshAll(); } catch { showLogin(); } })();
