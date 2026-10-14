/* ProxyHub panel — vanilla JS, no framework.
   Rules: session in httpOnly cookie (never in storage); only the UI theme
   preference is persisted in localStorage. Secrets are shown once only
   (create/rotate) and never logged, cached, or written to storage. */
"use strict";
let CSRF = "", NODES = [], RULES = [], CLIENTS = [], DEFROUTE = "proxy", GW = {};
let SYS = null, REPORT = null, SINGBOX_INSTALLED = null;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const TABS = ["dash","nodes","routing","devices","sub","health","lifecycle","telemetry","connections","logs","system","settings"];

/* ---------- theme (display preference only — safe to persist) ---------- */
function initTheme() {
  let t = "dark";
  try { t = localStorage.getItem("ph_theme") || "dark"; } catch {}
  if (t !== "light" && t !== "dark") t = "dark";
  document.documentElement.dataset.theme = t;
  paintThemeBtn();
}
function paintThemeBtn() {
  const dark = document.documentElement.dataset.theme !== "light";
  $("themeBtn").textContent = dark ? "🌙" : "☀️";
  $("themeBtn").setAttribute("aria-label", dark ? "تغییر به تم روشن" : "تغییر به تم تیره");
}
$("themeBtn").onclick = () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("ph_theme", next); } catch {}
  paintThemeBtn();
};

/* ---------- API ---------- */
async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "include",
    headers: { "Content-Type": "application/json", "x-csrf-token": CSRF, ...((opts.headers) || {}) }, ...opts });
  if (res.status === 401) { showLogin("نشست منقضی شد؛ دوباره وارد شوید."); throw new Error("نشست منقضی شد؛ دوباره وارد شوید."); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || ("خطا " + res.status));
  return data;
}
function toast(msg, err = false) {
  const box = $("toasts");
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : ""); t.textContent = msg;
  box.appendChild(t); setTimeout(() => t.remove(), 3800);
}
function copyText(txt, label = "کپی شد ✓") {
  (navigator.clipboard ? navigator.clipboard.writeText(txt) : Promise.reject())
    .then(() => toast(label)).catch(() => { prompt("کپی دستی:", txt); });
}
function getCsrf() { const m = document.cookie.match(/(?:^|; )ph_csrf=([^;]+)/); return m ? decodeURIComponent(m[1]) : ""; }
/* Busy guard: disable a button while its promise runs (prevents double submit). */
async function busy(btn, fn) {
  if (!btn) return fn();
  if (btn.disabled) return;
  btn.disabled = true; btn.setAttribute("aria-busy", "true");
  try { return await fn(); } finally { btn.disabled = false; btn.removeAttribute("aria-busy"); }
}
function errBox(container, message, retry) {
  const id = "retry-" + Math.random().toString(36).slice(2, 8);
  container.innerHTML = `<div class="alert bad">${esc(message)}</div>` +
    (retry ? `<button class="btn small" id="${id}">تلاش مجدد</button>` : "");
  if (retry) { const b = $(id); if (b) b.onclick = () => retry(); }
}
function skeleton(container, rows = 2) {
  container.innerHTML = Array.from({length: rows}, () => `<div class="skel"></div>`).join("");
}
function emptyBox(text) { return `<div class="empty">${esc(text)}</div>`; }
/* Accessible confirm via native dialog (no blocking prompt for dangerous acts). */
let confirmResolve = null;
function confirmDlg(text) {
  $("confirmText").textContent = text;
  const d = $("confirmDialog");
  if (d.open) d.close();
  d.showModal();
  return new Promise((resolve) => { confirmResolve = resolve; });
}
$("confirmOk").onclick = () => { if (confirmResolve) confirmResolve(true); confirmResolve = null; };
$("confirmDialog").addEventListener("close", () => { if (confirmResolve) confirmResolve(false); confirmResolve = null; });

function showLogin(msg) {
  $("loginView").classList.remove("hidden"); $("appView").classList.add("hidden");
  $("nav").closest("#sidenav").classList.add("hidden");
  if (msg) $("loginErr").textContent = msg;
}
function showApp() {
  $("loginView").classList.add("hidden"); $("appView").classList.remove("hidden");
  $("nav").closest("#sidenav").classList.remove("hidden");
}
$("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("loginErr").textContent = "";
  const u = $("username").value.trim(), p = $("password").value;
  if (!u || !p) { $("loginErr").textContent = "نام کاربری و گذرواژه لازم است."; return; }
  await busy($("loginBtn"), async () => {
    try {
      const r = await api("/api/auth/login", { method: "POST", body: JSON.stringify({ username: u, password: p }) });
      CSRF = r.csrf || getCsrf(); $("password").value = ""; showApp(); await refreshAll();
    } catch { $("loginErr").textContent = "ورود ناموفق بود."; }
  });
});
$("logoutBtn").onclick = async () => { try { await api("/api/auth/logout", { method: "POST" }); } catch {} showLogin(); };

/* ---------- navigation: grouped sidebar + drawer + hash routing ---------- */
function showTab(name, push = true) {
  if (!TABS.includes(name)) name = "dash";
  document.querySelectorAll("#nav button[data-tab]").forEach((x) => {
    const on = x.dataset.tab === name;
    x.classList.toggle("active", on);
    if (on) x.setAttribute("aria-current", "page"); else x.removeAttribute("aria-current");
  });
  document.querySelectorAll(".tab").forEach((t) => t.classList.add("hidden"));
  $("tab-" + name).classList.remove("hidden");
  document.body.classList.remove("navopen");
  $("menuBtn").setAttribute("aria-expanded", "false");
  if (push && location.hash !== "#/" + name) history.replaceState(null, "", "#/" + name);
}
document.querySelectorAll("#nav button[data-tab]").forEach((b) => { b.onclick = () => showTab(b.dataset.tab); });
window.addEventListener("hashchange", () => showTab(location.hash.replace("#/", "") || "dash", false));
$("menuBtn").onclick = () => {
  const open = document.body.classList.toggle("navopen");
  $("menuBtn").setAttribute("aria-expanded", open ? "true" : "false");
};
$("scrim").onclick = () => { document.body.classList.remove("navopen"); };

const dot = (c) => `<span class="dot ${c}" aria-hidden="true"></span>`;
const hdot = (h) => h === "HEALTHY" ? dot("ok") : h === "UNHEALTHY" ? dot("bad") : h === "DEGRADED" ? dot("warn") : dot("off");
const FA_HEALTH = { HEALTHY: "سالم", DEGRADED: "ناپایدار", UNHEALTHY: "ناسالم", UNKNOWN: "نامشخص", DISABLED: "غیرفعال" };
const PROTO_LABEL = { vless: "VLESS", vmess: "VMess", trojan: "Trojan", ss: "Shadowsocks", hysteria2: "Hysteria2" };
const protoBadge = (p) => `<span class="badge proto-${esc(p || "unknown")}">${esc(PROTO_LABEL[p] || p || "—")}</span>`;
function fmtAgo(iso) {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "—";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return s + " ثانیه پیش";
  if (s < 3600) return Math.floor(s / 60) + " دقیقه پیش";
  if (s < 86400) return Math.floor(s / 3600) + " ساعت پیش";
  return Math.floor(s / 86400) + " روز پیش";
}

/* ---------- capability banner (truthful hy2 / gateway signals) ---------- */
function updateSysBanner() {
  const box = $("sysBanner");
  const notes = [];
  const hy2count = NODES.filter((n) => n.protocol === "hysteria2").length;
  if (SINGBOX_INSTALLED === false && hy2count > 0)
    notes.push(`<span class="chip warn">Hysteria2 غیرعملیاتی</span> ${hy2count} نود Hysteria2 وجود دارد اما sing-box روی سرور نصب نیست؛ این نودها verify نمی‌شوند و واجد چرخش/failover نیستند. راهنما: تب سیستم.`);
  if (GW.enabled && GW.bind === "0.0.0.0")
    notes.push(`<span class="chip bad">gateway عمومی</span> شنونده روی <span dir="ltr">0.0.0.0</span> باز است — فقط کلاینت‌های دارای اعتبار می‌توانند وصل شوند.`);
  if (!NODES.some((n) => n.is_active))
    notes.push(`<span class="chip warn">بدون نود فعال</span> هیچ نود فعالی وجود ندارد؛ سرویس خروجی ندارد.`);
  box.classList.toggle("hidden", notes.length === 0);
  box.innerHTML = notes.map((n) => `<div class="alert warn">${n}</div>`).join("");
}

/* ---------- dashboard ---------- */
async function loadDash() {
  const box = $("dashCards");
  try {
    const [s, rep] = await Promise.all([api("/api/status"), api("/api/report/summary?hours=48").catch(() => null)]);
    REPORT = rep;
    const a = s.active_node; GW = s.gateway || {};
    const managed = s.managed_xray || {};
    $("coreChip").innerHTML = managed.active ? dot("ok") + " هسته فعال" : dot("bad") + " هسته قطع";
    $("coreChip").className = "chip " + (managed.active ? "ok" : "bad");
    /* Warnings first: honest ops overview, no decorative metrics. */
    const alerts = [];
    if (!managed.active) alerts.push(`<div class="alert bad">${dot("bad")} هسته مدیریت‌شده فعال نیست (<span dir="ltr">${esc(managed.state || "unknown")}</span>). خروجی پروکسی کار نمی‌کند.</div>`);
    if (!a) alerts.push(`<div class="alert warn">${dot("warn")} نود فعالی وجود ندارد.</div>`);
    else if (a.egress_ok === false || a.egress_ok === 0) alerts.push(`<div class="alert warn">${dot("warn")} نود فعال «${esc(a.name)}» اتصال سرتاسری تأییدشده ندارد (process روشن ≠ اینترنت سالم).</div>`);
    if ((s.subscription || {}).last_error) alerts.push(`<div class="alert warn">${dot("warn")} آخرین خطای اشتراک: ${esc(s.subscription.last_error)}</div>`);
    const unh = (s.nodes || {}).unhealthy || 0;
    if (unh > 0) alerts.push(`<div class="alert warn">${dot("warn")} ${unh} نود ناسالم وجود دارد.</div>`);
    $("dashAlerts").innerHTML = alerts.join("");
    $("dashCards").innerHTML = `
      <div class="stat"><div class="v">${managed.active ? dot("ok") + " فعال" : dot("bad") + " قطع"}</div><div class="k">هسته مدیریت‌شده (<span dir="ltr">${esc(managed.state || "—")}</span>)</div></div>
      <div class="stat"><div class="v">${a ? esc(a.name) : "—"}</div><div class="k">نود فعال ${a ? protoBadge(a.protocol) : ""}</div></div>
      <div class="stat"><div class="v">${a && a.egress_ok ? dot("ok") + " تأییدشده" : dot("bad") + " تأییدنشده"}</div><div class="k">اتصال سرتاسری (egress)</div></div>
      <div class="stat"><div class="v" dir="ltr">${a && a.latency_ms != null ? a.latency_ms + " ms" : "—"}</div><div class="k">تأخیر نود فعال</div></div>
      <div class="stat"><div class="v" dir="ltr">${esc(s.exit_ip || "—")}</div><div class="k">IP خروجی (زنده، نه کش)</div></div>
      <div class="stat"><div class="v">${s.nodes.healthy} / ${s.nodes.total}</div><div class="k">نودهای سالم</div></div>
      <div class="stat"><div class="v">${s.subscription.last_ok_at ? "به‌روز" : "نامشخص"}</div><div class="k">اشتراک (${esc(fmtAgo(s.subscription.last_ok_at))})</div></div>
      <div class="stat"><div class="v">${GW.enabled ? dot("ok") + " روشن" : dot("off") + " خاموش"}</div><div class="k">gateway عمومی</div></div>`;
    $("dashActive").innerHTML = a
      ? `<table>
         <tr><td>نود</td><td>${esc(a.name)} ${protoBadge(a.protocol)}</td></tr>
         <tr><td>مقصد</td><td dir="ltr">${esc(a.server)}:${a.port}</td></tr>
         <tr><td>سلامت</td><td>${hdot(a.health_state || a.health)} ${esc(FA_HEALTH[a.health_state || a.health] || a.health || "—")}</td></tr>
         <tr><td>egress سرتاسری</td><td>${a.egress_ok ? '<span class="ok">تأییدشده ✓</span> (تست HTTPS واقعی)' : '<span class="bad">تأییدنشده</span> (روشن بودن پردازش کافی نیست)'}</td></tr>
         <tr><td>تأخیر</td><td dir="ltr">${a.latency_ms != null ? a.latency_ms + " ms" : "—"}</td></tr></table>
         <div class="row"><a class="btn small" href="#/nodes">جزئیات نودها</a><a class="btn small ghost" href="#/health">سلامت</a></div>`
      : emptyBox("نود فعالی وجود ندارد — از تب نودها یک نود را فعال کنید.");
    $("gwCard").innerHTML = GW.enabled
      ? `<table><tr><td>وضعیت</td><td>${dot("ok")} فعال روی <span dir="ltr">${esc(GW.bind)}:${GW.socks_port} (SOCKS) / ${GW.http_port} (HTTP)</span></td></tr>
         <tr><td>مسیر پیش‌فرض</td><td><span class="pill ${GW.default_route}">${GW.default_route}</span></td></tr></table>`
      : `<p class="muted">خاموش است (پیش‌فرض امن). فعال‌سازی از تب تنظیمات.</p>`;
    $("foCard").innerHTML = `<table>
      <tr><td>حالت</td><td>${s.failover.mode === "auto" ? "خودکار" : "دستی"}</td></tr>
      <tr><td>نود قبلی</td><td>${esc(s.failover.previous_node || "—")}</td></tr>
      <tr><td>آخرین دلیل</td><td class="wrap">${esc(s.failover.last_reason || "—")}</td></tr>
      <tr><td>زمان</td><td dir="ltr">${esc(s.failover.last_at || "—")} (${esc(fmtAgo(s.failover.last_at))})</td></tr></table>
      <div class="row"><a class="btn small ghost" href="#/lifecycle">چرخه حیات</a></div>`;
    const recent = (rep && rep.transitions && rep.transitions.recent) || [];
    $("dashRecent").innerHTML = recent.length
      ? `<table><thead><tr><th>زمان</th><th>نوع</th><th>جزئیات</th></tr></thead><tbody>${recent.slice(0, 5).map((e) =>
        `<tr><td dir="ltr">${esc(e.at || "")}</td><td>${esc(e.actor)}</td><td class="wrap">${esc(e.detail)}</td></tr>`).join("")}</tbody></table>
        <p class="muted">نرخ موفقیت deep در ۴۸ ساعت: ${rep.telemetry.deep_success_rate ?? "—"} (نمونه: ${rep.telemetry.deep_total})</p>`
      : `<p class="muted">فعالیت اخیر ثبت نشده است.</p>`;
    updateSysBanner();
  } catch (e) { errBox(box, e.message, loadDash); }
}
$("quickTest").onclick = (ev) => busy(ev.target, async () => {
  $("dashMsg").textContent = "در حال تست…";
  try {
    const ns = NODES.length ? NODES : await api("/api/nodes");
    const a = ns.find((n) => n.is_active);
    if (!a) { $("dashMsg").textContent = "نود فعالی وجود ندارد."; return; }
    const r = await api(`/api/nodes/${a.id}/test`, { method: "POST" });
    $("dashMsg").textContent = r.ok ? `سالم (${r.node.latency_ms} ms)` : `ناسالم: ${r.node.last_error}`;
    await loadDash();
  } catch (e) { $("dashMsg").textContent = e.message; }
});
$("quickUpdate").onclick = (ev) => busy(ev.target, async () => {
  $("dashMsg").textContent = "در حال به‌روزرسانی…";
  try { const r = await api("/api/subscription/update", { method: "POST" });
    $("dashMsg").textContent = `انجام شد: ${r.total} نود.`; toast("اشتراک به‌روز شد"); await refreshAll();
  } catch (e) { $("dashMsg").textContent = e.message; }
});

/* ---------- nodes ---------- */
function renderNodes() {
  const q = ($("nodeSearch").value || "").toLowerCase();
  const proto = $("nodeProto").value, hf = $("nodeHealth").value, sort = $("nodeSort").value;
  let list = NODES.filter((n) => {
    if (proto && n.protocol !== proto) return false;
    if (hf === "active" && !n.is_active) return false;
    else if (hf && hf !== "active" && (n.health_state || n.health) !== hf) return false;
    return !q || (n.name + " " + n.server + " " + n.protocol).toLowerCase().includes(q);
  });
  if (sort === "latency") list = [...list].sort((a, b) => (a.latency_ms ?? 1e9) - (b.latency_ms ?? 1e9));
  else if (sort === "name") list = [...list].sort((a, b) => a.name.localeCompare(b.name, "fa"));
  else list = [...list].sort((a, b) => ((b.is_active ? 1 : 0) - (a.is_active ? 1 : 0)) || (a.latency_ms ?? 1e9) - (b.latency_ms ?? 1e9));
  $("nodeCount").textContent = `${list.length} از ${NODES.length} نود`;
  $("nodeList").innerHTML = list.map((n) => {
    const hs = n.health_state || n.health;
    const hy2warn = n.protocol === "hysteria2" && SINGBOX_INSTALLED === false;
    return `
    <div class="node ${hs} ${n.is_active ? "active" : ""}">
      <div class="head"><strong>${n.is_active ? "⭐ " : ""}${esc(n.name)}</strong>
        <span>${protoBadge(n.protocol)} ${hdot(hs)} ${esc(FA_HEALTH[hs] || hs || "—")}</span></div>
      <div class="meta ltr">${esc(n.protocol)} · ${esc(n.server)}:${n.port} · ${esc(n.transport || "")} ${esc(n.security || "")}</div>
      <div class="meta">تأخیر: <span dir="ltr">${n.latency_ms != null ? n.latency_ms + " ms" : "—"}</span> · موفق: ${n.success_count || 0} · خطاهای پیاپی: ${n.fail_count || 0}${n.egress_ok ? ' · <span class="ok">egress ✓</span>' : ' · <span class="bad">egress تأییدنشده</span>'}${n.removed_upstream ? ' · <span class="warn">حذف‌شده از اشتراک</span>' : ""}${n.enabled ? "" : ' · <span class="warn">غیرفعال</span>'}${n.circuit_open ? ' · <span class="warn">مدار باز (موقتاً کنار گذاشته شده)</span>' : ""}</div>
      ${hy2warn ? `<div class="alert warn">این نود Hysteria2 است اما sing-box نصب نیست — تست/فعال‌سازی واقعی انجام نمی‌شود. جزئیات: تب سیستم.</div>` : ""}
      ${n.last_error ? `<div class="meta">آخرین خطا: <span class="bad">${esc(n.last_error)}</span></div>` : ""}
      <details><summary>جزئیات فنی</summary><table>
        <tr><td>امتیاز کیفیت</td><td dir="ltr">${n.quality_score ?? "—"}</td></tr>
        <tr><td>تأخیر TCP / handshake / app</td><td dir="ltr">${n.tcp_latency_ms ?? "—"} / ${n.handshake_latency_ms ?? "—"} / ${n.app_latency_ms ?? "—"} ms</td></tr>
        <tr><td>پراب‌ها (موفق/کل)</td><td dir="ltr">${n.successful_probes ?? 0} / ${n.total_probes ?? 0}</td></tr>
        <tr><td>تایم‌اوت / ریست / خطای TLS</td><td dir="ltr">${n.timeout_count ?? 0} / ${n.connection_reset_count ?? 0} / ${n.tls_failure_count ?? 0}</td></tr>
        <tr><td>رتبه</td><td dir="ltr">${n.rank ?? "—"}</td></tr>
        <tr><td>آخرین بررسی / آخرین موفق</td><td dir="ltr">${esc(n.last_checked || "—")} / ${esc(n.last_success || "—")}</td></tr>
        ${n.tags ? `<tr><td>برچسب‌ها</td><td dir="ltr">${esc(n.tags)}</td></tr>` : ""}
      </table></details>
      <div class="actions">
        ${n.is_active ? "" : `<button class="btn small" data-action="node-activate" data-id="${n.id}">فعال‌سازی</button>`}
        <button class="btn small ghost" data-action="node-test" data-id="${n.id}">تست</button>
        <button class="btn small ghost" data-action="node-toggle" data-id="${n.id}" data-enabled="${n.enabled ? 1 : 0}">${n.enabled ? "غیرفعال" : "فعال"}</button>
        <button class="btn small ghost" data-action="node-rename" data-id="${n.id}">تغییرنام</button>
        ${n.removed_upstream || !n.enabled ? `<button class="btn small ghost" data-action="node-restore" data-id="${n.id}">بازگردانی</button>` : ""}
        ${n.is_active ? "" : `<button class="btn small danger" data-action="node-delete" data-id="${n.id}">حذف</button>`}
      </div>
    </div>`; }).join("") || emptyBox("نودی با این فیلتر یافت نشد.");
}
["nodeSearch","nodeProto","nodeHealth","nodeSort"].forEach((id) => {
  $(id).addEventListener("input", renderNodes); $(id).addEventListener("change", renderNodes);
});
async function loadNodes() {
  try {
    NODES = await api("/api/nodes");
    renderNodes(); updateSysBanner();
  } catch (e) { errBox($("nodeList"), e.message, loadNodes); }
}
async function nodeAct(id, act) {
  try { await api(`/api/nodes/${id}/${act}`, { method: "POST" });
    toast(act === "activate" ? "نود فعال شد" : "تست انجام شد"); await loadNodes(); await loadDash();
  } catch (e) { toast(e.message, true); }
}
async function nodeToggle(id, en) {
  const n = NODES.find((x) => x.id === id);
  if (en && n && n.is_active) {
    if (!await confirmDlg(`نود فعال «${n.name}» غیرفعال شود؟ پس از این کار هیچ نود فعالی نخواهید داشت و خروجی قطع می‌شود.`)) return;
  }
  try { await api(`/api/nodes/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: !en }) });
    toast(!en ? "نود فعال شد" : "نود غیرفعال شد"); await loadNodes(); await loadDash();
  } catch (e) { toast(e.message, true); }
}
async function nodeRename(id) {
  const n = NODES.find((x) => x.id === id);
  const name = prompt("نام جدید:", n ? n.name : "");
  if (!name || !name.trim()) return;
  try { await api(`/api/nodes/${id}`, { method: "PATCH", body: JSON.stringify({ name: name.trim() }) }); await loadNodes(); }
  catch (e) { toast(e.message, true); }
}
async function nodeDelete(id) {
  const n = NODES.find((x) => x.id === id);
  if (n && n.is_active) { toast("نود فعال را نمی‌توان حذف کرد.", true); return; }
  if (!await confirmDlg(`نود «${n ? n.name : id}» برای همیشه حذف شود؟ این عمل قابل بازگشت نیست.`)) return;
  try { await api(`/api/nodes/${id}`, { method: "DELETE" }); toast("نود حذف شد"); await loadNodes(); await loadDash(); }
  catch (e) { toast(e.message, true); }
}
async function nodeRestore(id) {
  try { await api(`/api/nodes/${id}/restore`, { method: "POST" }); toast("نود بازگردانی شد"); await loadNodes(); }
  catch (e) { toast(e.message, true); }
}

/* ---------- traffic rules ---------- */
const FA_MT = { domain_exact: "دامنه دقیق", domain_suffix: "پسوند دامنه", domain_wildcard: "wildcard", cidr: "CIDR", ip: "IP", port: "پورت" };
async function loadRules() {
  const box = $("tab-routing").querySelector("tbody");
  try {
    const r = await api("/api/rules"); RULES = r.rules; DEFROUTE = r.default_route;
    $("defRouteSel").value = DEFROUTE;
    const names = CLIENTS.map((c) => c.username);
    $("ruleClient").innerHTML = `<option value="all">همه</option>` + names.map((u) => `<option value="${esc(u)}">${esc(u)}</option>`).join("");
    $("simClient").innerHTML = `<option value="all">همه کلاینت‌ها</option>` + names.map((u) => `<option value="${esc(u)}">${esc(u)}</option>`).join("");
    box.innerHTML = RULES.map((x) => `<tr>
      <td dir="ltr">${x.priority}</td><td><b>${esc(FA_MT[x.match_type] || x.match_type)}</b> <span dir="ltr">${esc(x.pattern)}</span>${x.port ? ` <span dir="ltr">:${esc(x.port)}</span>` : ""}${x.network !== "any" ? ` (${esc(x.network)})` : ""}${x.note ? `<br><small class="muted">${esc(x.note)}</small>` : ""}</td>
      <td><span class="pill ${x.action}">${x.action}</span></td><td dir="ltr">${esc(x.client_scope)}</td>
      <td>${x.enabled ? dot("ok") : dot("off")}</td>
      <td><button class="btn small ghost" data-action="rule-move" data-id="${x.id}" data-dir="up" aria-label="بالا">↑</button><button class="btn small ghost" data-action="rule-move" data-id="${x.id}" data-dir="down" aria-label="پایین">↓</button>
      <button class="btn small ghost" data-action="rule-edit" data-id="${x.id}">ویرایش</button><button class="btn small ghost" data-action="rule-dup" data-id="${x.id}">تکثیر</button>
      <button class="btn small danger" data-action="rule-del" data-id="${x.id}">حذف</button></td></tr>`).join("")
      || `<tr><td colspan="6">${"قانونی ثبت نشده است."}</td></tr>`;
  } catch (e) { box.innerHTML = `<tr><td colspan="6" class="bad">${esc(e.message)}</td></tr>`; }
}
function openRuleDlg(rule) {
  $("ruleDlgTitle").textContent = rule ? "ویرایش قانون" : "قانون جدید";
  $("ruleId").value = rule?.id || ""; $("ruleType").value = rule?.match_type || "domain_suffix";
  $("rulePattern").value = rule?.pattern || ""; $("rulePort").value = rule?.port || "";
  $("ruleNet").value = rule?.network || "any"; $("ruleAction").value = rule?.action || "proxy";
  $("ruleClient").value = rule?.client_scope || "all"; $("rulePrio").value = rule?.priority ?? 100;
  $("ruleNote").value = rule?.note || ""; $("ruleEnabled").checked = rule ? !!rule.enabled : true;
  $("ruleErr").textContent = ""; $("rulePattern").removeAttribute("aria-invalid");
  $("ruleDialog").showModal();
}
$("addRuleBtn").onclick = () => openRuleDlg(null);
$("ruleSave").onclick = async (e) => {
  e.preventDefault();
  const pattern = $("rulePattern").value.trim();
  if (!pattern) { $("ruleErr").textContent = "الگو لازم است."; $("rulePattern").setAttribute("aria-invalid", "true"); return; }
  const portRaw = $("rulePort").value.trim();
  if (portRaw && !/^\d+$/.test(portRaw)) { $("ruleErr").textContent = "پورت باید عدد باشد."; return; }
  const id = $("ruleId").value;
  const body = { match_type: $("ruleType").value, pattern, port: portRaw,
    network: $("ruleNet").value, action: $("ruleAction").value, client_scope: $("ruleClient").value,
    priority: +$("rulePrio").value || 100, note: $("ruleNote").value, enabled: $("ruleEnabled").checked };
  await busy($("ruleSave"), async () => {
    try { id ? await api(`/api/rules/${id}`, { method: "PUT", body: JSON.stringify(body) })
            : await api("/api/rules", { method: "POST", body: JSON.stringify(body) });
      $("ruleDialog").close(); toast("قانون ذخیره شد"); await loadRules();
    } catch (err) { $("ruleErr").textContent = err.message; }
  });
};
async function ruleEdit(id) { openRuleDlg(RULES.find((r) => r.id === id)); }
async function ruleDel(id) { if (!await confirmDlg("این قانون حذف شود؟")) return;
  try { await api(`/api/rules/${id}`, { method: "DELETE" }); toast("حذف شد"); await loadRules(); } catch (e) { toast(e.message, true); } }
async function ruleDup(id) { try { await api(`/api/rules/${id}/duplicate`, { method: "POST" }); toast("تکثیر شد (غیرفعال)"); await loadRules(); } catch (e) { toast(e.message, true); } }
async function ruleMove(id, dir) { try { await api(`/api/rules/${id}/move`, { method: "POST", body: JSON.stringify({ direction: dir }) }); await loadRules(); } catch (e) { toast(e.message, true); } }
$("defRouteBtn").onclick = (ev) => busy(ev.target, async () => {
  $("defRouteMsg").textContent = "";
  try { await api("/api/routing/default", { method: "PUT", body: JSON.stringify({ default_route: $("defRouteSel").value }) });
    toast("مسیر پیش‌فرض ذخیره شد"); await loadRules();
  } catch (e) { $("defRouteMsg").textContent = e.message; }
});
$("simBtn").onclick = (ev) => busy(ev.target, async () => {
  $("simErr").textContent = "";
  const domain = $("simDomain").value.trim();
  if (!domain) { $("simErr").textContent = "دامنه لازم است."; return; }
  const port = $("simPort").value ? +$("simPort").value : null;
  try { const r = await api("/api/routing/simulate", { method: "POST", body: JSON.stringify({
      domain, ip: $("simIp").value.trim(), port, client: $("simClient").value }) });
    $("simResult").innerHTML = `قانون منطبق: <b dir="ltr">${esc(r.matched_rule)}</b><br>تصمیم: <span class="pill ${r.decision}">${r.decision}</span> · نود فعال: ${esc(r.active_node || "—")}${r.is_default ? " · (پیش‌فرض)" : ""}`;
  } catch (e) { $("simResult").textContent = e.message; }
});
/* server-side app rules (kept, progressive disclosure) */
async function loadAppRules() {
  try {
    const r = await api("/api/routing");
    $("routeList").innerHTML = r.rules.length
      ? `<table>${r.rules.map((x) => `<tr><td class="wrap">${esc(x.app)}</td><td>${x.route === "proxy" ? "→ پروکسی" : "→ مستقیم"}</td>
        <td><button class="btn small danger" data-action="approute-del" data-id="${x.id}">حذف</button></td></tr>`).join("")}</table>`
      : `<p class="muted">قانونی ثبت نشده است.</p>`;
    $("proxyEnv").textContent = r.proxy_env;
  } catch (e) { $("routeList").innerHTML = `<p class="error">${esc(e.message)}</p>`; }
}
async function delAppRoute(id) {
  if (!await confirmDlg("این قانون سرویس حذف شود؟")) return;
  try { await api(`/api/routing/${id}`, { method: "DELETE" }); await loadAppRules(); } catch (e) { toast(e.message, true); }
}
$("addRouteBtn").onclick = (ev) => busy(ev.target, async () => {
  const app = $("newApp").value.trim(); if (!app) { toast("نام سرویس لازم است.", true); return; }
  try { await api("/api/routing", { method: "POST", body: JSON.stringify({ app, route: $("newRoute").value }) }); $("newApp").value = ""; await loadAppRules(); }
  catch (e) { toast(e.message, true); } });

/* ---------- devices ---------- */
async function loadClients() {
  try {
    CLIENTS = await api("/api/clients");
    const srvHost = location.hostname || "server";
    $("gwInfo").innerHTML = GW.enabled
      ? `gateway فعال: <span dir="ltr">SOCKS ${esc(GW.bind)}:${GW.socks_port} · HTTP ${GW.http_port}</span> — روی دستگاه: سرور <b dir="ltr">${esc(srvHost)}</b>`
      : `<span class="warn">gateway خاموش است.</span> ابتدا از تب تنظیمات فعالش کنید (برای تست لوکال روی 127.0.0.1 بماند).`;
    $("clientList").innerHTML = CLIENTS.map((c) => `
      <div class="client"><div class="head"><strong>${esc(c.name)}</strong>
        <span>${c.usable ? dot("ok") + " فعال" : dot("bad") + " " + esc(c.status_detail || "غیرفعال")}</span></div>
        <div class="meta muted">نام کاربری: <b dir="ltr">${esc(c.username)}</b> · انقضا: <span dir="ltr">${esc(c.expires_at || "—")}</span> · آخرین استفاده: <span dir="ltr">${esc(c.last_used_at || "—")}</span>${c.dedicated_port ? ` · پورت اختصاصی: <span dir="ltr">${c.dedicated_port}</span>` : ""}${c.notes ? ` · ${esc(c.notes)}` : ""}</div>
        <div class="actions row">
          <button class="btn small ghost" data-action="client-config" data-id="${c.id}">پیکربندی</button>
          <button class="btn small ghost" data-action="client-qr" data-id="${c.id}">QR</button>
          <button class="btn small ghost" data-action="client-edit" data-id="${c.id}">ویرایش</button>
          <button class="btn small ghost" data-action="client-rotate" data-id="${c.id}">چرخش گذرواژه</button>
          <button class="btn small ghost" data-action="client-toggle" data-id="${c.id}" data-enabled="${c.enabled ? 1 : 0}">${c.enabled ? "غیرفعال" : "فعال"}</button>
          <button class="btn small danger" data-action="client-del" data-id="${c.id}">حذف</button>
        </div><div id="cfg-${c.id}"></div><div id="qr-${c.id}"></div>
      </div>`).join("") || emptyBox("دستگاهی ثبت نشده است.");
  } catch (e) { errBox($("clientList"), e.message, loadClients); }
}
function clientConfig(id) {
  const c = CLIENTS.find((x) => x.id === id);
  if (!c) return;
  const srvHost = location.hostname || "server";
  const cfg = `SOCKS5\nServer: ${srvHost}\nPort: ${GW.socks_port || "?"}\nUsername: ${c.username}\nPassword: (فقط لحظه‌ی ساخت/چرخش)\n\nHTTP\nServer: ${srvHost}\nPort: ${GW.http_port || "?"}\nUsername: ${c.username}`;
  const summary = "socks5://" + c.username + "@" + srvHost + ":" + (GW.socks_port || "?");
  $("cfg-" + id).innerHTML = `<pre dir="ltr">${esc(cfg)}</pre><button class="btn small" data-action="copy-summary" data-text="${esc(summary)}">کپی خلاصه (بدون گذرواژه)</button>`;
}
function clientQR(id) {
  const box = $("qr-" + id);
  box.innerHTML = `<span class="qr"><img src="/api/clients/${id}/qr.png" alt="QR اتصال" width="180"></span>`;
  const img = box.querySelector("img");
  if (img) img.addEventListener("error", () => {
    box.innerHTML = `<p class="error">QR در دسترس نیست (کلاینت غیرفعال یا خطای سرور).</p>`;
  });
}
async function clientRotate(id) {
  if (!await confirmDlg("گذرواژه‌ی جدید تولید و قبلی باطل شود؟ دستگاه با گذرواژه قبلی قطع می‌شود.")) return;
  try { const r = await api(`/api/clients/${id}/rotate`, { method: "POST" }); showSecret(r.password_once); await loadClients(); }
  catch (e) { toast(e.message, true); }
}
async function clientToggle(id, en) {
  try { await api(`/api/clients/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: !en }) }); await loadClients(); } catch (e) { toast(e.message, true); }
}
async function clientDel(id) {
  const c = CLIENTS.find((x) => x.id === id);
  if (!await confirmDlg(`دستگاه «${c ? c.name : id}» حذف شود؟ دسترسی آن بلافاصله قطع می‌شود.`)) return;
  try { await api(`/api/clients/${id}`, { method: "DELETE" }); toast("حذف شد"); await loadClients(); await loadRules(); } catch (e) { toast(e.message, true); }
}
function openClientDlg(c) {
  $("clientDlgTitle").textContent = c ? "ویرایش دستگاه" : "دستگاه جدید";
  $("clientId").value = c?.id || "";
  $("clientName").value = c?.name || "";
  $("clientUser").value = "";
  $("clientUser").placeholder = c ? "تغییر نام کاربری پشتیبانی نمی‌شود" : "laptop_x7";
  $("clientUser").disabled = !!c;
  $("clientPass").value = "";
  $("clientPass").disabled = !!c;
  $("clientPass").placeholder = c ? "تغییر گذرواژه فقط با «چرخش گذرواژه»" : "خالی = تولید خودکار";
  $("clientExp").value = c?.expires_at ? String(c.expires_at).slice(0, 10) : "";
  $("clientNote").value = c?.notes || "";
  $("clientSave").textContent = c ? "ذخیره" : "ساخت";
  $("clientErr").textContent = "";
  $("clientDialog").showModal();
}
$("addClientBtn").onclick = () => openClientDlg(null);
function clientEdit(id) { openClientDlg(CLIENTS.find((x) => x.id === id)); }
$("clientSave").onclick = (e) => {
  e.preventDefault();
  const name = $("clientName").value.trim();
  if (!name) { $("clientErr").textContent = "نام دستگاه لازم است."; return; }
  const id = $("clientId").value;
  busy($("clientSave"), async () => {
    try {
      if (id) {
        /* Backend clears expiry on falsy value, so always send it (allows clearing). */
        const body = { name, notes: $("clientNote").value, expires_at: $("clientExp").value || "" };
        await api(`/api/clients/${id}`, { method: "PATCH", body: JSON.stringify(body) });
        $("clientDialog").close(); toast("ذخیره شد"); await loadClients();
      } else {
        const body = { name };
        if ($("clientUser").value.trim()) body.username = $("clientUser").value.trim();
        if ($("clientPass").value) body.password = $("clientPass").value;
        if ($("clientExp").value) body.expires_at = $("clientExp").value;
        if ($("clientNote").value.trim()) body.notes = $("clientNote").value.trim();
        const r = await api("/api/clients", { method: "POST", body: JSON.stringify(body) });
        $("clientDialog").close(); showSecret(r.password_once); await loadClients(); await loadRules();
      }
    } catch (err) { $("clientErr").textContent = err.message; }
  });
};
function showSecret(pw) { $("secretOnce").textContent = pw || "(خالی)"; $("secretDialog").showModal(); }
$("secretCopy").onclick = (e) => { e.preventDefault(); copyText($("secretOnce").textContent, "گذرواژه کپی شد"); };

/* ---------- subscription ---------- */
async function loadSub() {
  try {
    const s = await api("/api/subscription"), st = s.last_stats || {};
    $("subInfo").innerHTML = `<table>
      <tr><td>وضعیت</td><td>${s.configured ? "تنظیم شده" : "<span class='bad'>تنظیم نشده</span>"}</td></tr>
      <tr><td>URL (ماسک‌شده)</td><td dir="ltr">${esc(s.url_masked || "—")}</td></tr>
      <tr><td>آخرین موفق</td><td dir="ltr">${esc(s.last_ok_at || "—")} (${esc(fmtAgo(s.last_ok_at))})</td></tr>
      <tr><td>آخرین تلاش</td><td dir="ltr">${esc(s.last_attempt_at || "—")}</td></tr>
      <tr><td>به‌روزرسانی بعدی</td><td dir="ltr">${esc(s.next_update_at || "—")}</td></tr>
      <tr><td>آخرین خطا</td><td class="wrap">${esc(s.last_error || "—")}</td></tr>
      <tr><td>نودها</td><td>کل: ${s.node_count} (جدید: ${st.added ?? "—"} · به‌روز: ${st.updated ?? "—"} · حذف‌شده: ${st.removed ?? "—"} · تکراری: ${st.duplicates ?? "—"} · پشتیبانی‌نشده: ${st.unsupported ?? "—"} · خطا: ${st.errors ?? "—"})</td></tr>
      <tr><td>فاصله به‌روزرسانی</td><td dir="ltr">${s.update_interval}s</td></tr></table>`;
  } catch (e) { errBox($("subInfo"), e.message, loadSub); }
}
$("subUpdateBtn").onclick = (ev) => busy(ev.target, async () => {
  try { const r = await api("/api/subscription/update", { method: "POST" }); toast(`انجام شد: ${r.total} نود`); await refreshAll(); }
  catch (e) { toast(e.message, true); } });
$("subSaveBtn").onclick = (ev) => busy(ev.target, async () => {
  const url = $("subUrl").value.trim();
  $("subUrlErr").textContent = "";
  if (!url) { $("subUrlErr").textContent = "URL لازم است."; return; }
  if (!/^https:\/\//i.test(url)) { $("subUrlErr").textContent = "URL اشتراک باید با https:// شروع شود."; return; }
  try { await api("/api/subscription", { method: "PUT", body: JSON.stringify({ url }) }); $("subUrl").value = ""; await loadSub(); toast("ذخیره شد"); }
  catch (e) { $("subUrlErr").textContent = e.message; } });

/* ---------- health ---------- */
async function loadHealth() {
  try {
    const ns = NODES.length ? NODES : await api("/api/nodes");
    if (!ns.length) { $("healthList").innerHTML = emptyBox("نودی ثبت نشده است."); return; }
    $("healthList").innerHTML = `<div class="tablewrap"><table><thead><tr><th>نود</th><th>وضعیت</th><th>تأخیر</th><th>خطای پیاپی</th><th>آخرین موفق</th><th>آخرین بررسی</th><th></th></tr></thead><tbody>
      ${ns.map((n) => { const hs = n.health_state || n.health; return `<tr><td>${n.is_active ? "⭐ " : ""}${esc(n.name)} ${protoBadge(n.protocol)}</td><td>${hdot(hs)} ${esc(FA_HEALTH[hs] || hs || "—")}</td>
      <td dir="ltr">${n.latency_ms != null ? n.latency_ms + " ms" : "—"}</td><td dir="ltr">${n.fail_count || 0}</td>
      <td dir="ltr">${esc(n.last_success || "—")}</td><td dir="ltr">${esc(n.last_checked || "—")}</td>
      <td><button class="btn small ghost" data-action="node-test" data-id="${n.id}">تست</button></td></tr>`; }).join("")}</tbody></table></div>`;
  } catch (e) { errBox($("healthList"), e.message, loadHealth); }
}
async function loadHealthCycles() {
  try {
    const c = await api("/api/health/cycles");
    const row = (name, d) => `<tr><td>${name}</td><td dir="ltr">${esc(d.last_cycle || "—")}</td><td>${fmtAgo(d.last_cycle)}</td><td dir="ltr">${d.duration_ms != null ? Math.round(d.duration_ms) + " ms" : "—"}</td><td dir="ltr">${d.seconds_since_last != null ? Math.round(d.seconds_since_last) + "s" : "—"}</td></tr>`;
    $("healthCycles").innerHTML = `<div class="tablewrap"><table><thead><tr><th>چرخه</th><th>آخرین اجرا</th><th>قدمت</th><th>مدت</th><th>از آخرین</th></tr></thead><tbody>
      ${row("fast", c.fast_loop)}${row("active", c.active_loop)}${row("deep", c.deep_loop)}${row("ranking", c.ranking_loop)}
      </tbody></table></div><p class="muted">خطاهای پیاپی چرخه: ${c.cycle_failure_count} · در حال اجرا: ${c.running ? "بله" : "خیر"}</p>`;
  } catch (e) { errBox($("healthCycles"), e.message, loadHealthCycles); }
}

/* ---------- lifecycle ---------- */
async function loadLifecycle() {
  try {
    const s = await api("/api/lifecycle/status");
    const a = s.active_node;
    $("lifecycleStatus").innerHTML = a
      ? `<table><tr><td>نود فعال</td><td>${esc(a.name)} (#${a.id}) ${protoBadge(a.protocol)}</td></tr>
         <tr><td>مدت فعالیت</td><td dir="ltr">${a.duration_hours}h / ${a.max_duration_hours}h (باقی‌مانده ${a.remaining_hours}h)</td></tr>
         <tr><td>سیاست</td><td>فعال: ${s.enabled ? "بله" : "خیر"} · dry-run: ${s.dry_run ? "بله" : "خیر"}</td></tr>
         <tr><td>آخرین چرخش</td><td dir="ltr">${esc(s.last_rotation_at || "—")}${s.last_rotation_at ? ` (${esc(fmtAgo(s.last_rotation_at))})` : ""}</td></tr>
         ${s.last_rotation_reason ? `<tr><td>دلیل آخرین چرخش</td><td class="wrap">${esc(s.last_rotation_reason)}</td></tr>` : ""}</table>`
      : emptyBox("نود فعالی وجود ندارد.");
    const rows = await api("/api/audit?action=node.activate&limit=20");
    const rel = rows.filter((e) => /rotation|failover/.test(e.detail || ""));
    $("rotationHistory").innerHTML = rel.length
      ? `<div class="tablewrap"><table><thead><tr><th>زمان</th><th>عامل</th><th>جزئیات</th></tr></thead><tbody>${rel.map((e) => `<tr><td dir="ltr">${esc(e.at || "")}</td><td>${esc(e.actor)}</td><td class="wrap">${esc(e.detail)}</td></tr>`).join("")}</tbody></table></div>`
      : `<p class="muted">تاریخچه چرخش/failover یافت نشد (خرابی واقعی همیشه بر زمان‌بندی اولویت دارد).</p>`;
  } catch (e) { errBox($("lifecycleStatus"), e.message, loadLifecycle); }
}
const rotateBtn = $("rotateBtn");
if (rotateBtn) rotateBtn.onclick = (ev) => busy(ev.target, async () => {
  if (!await confirmDlg("چرخش کنترل‌شده به بهترین نود سالم واجدشرایط انجام شود؟ اتصال فعلی جابه‌جا می‌شود.")) return;
  const msg = $("rotateMsg"); if (msg) msg.textContent = "در حال چرخش…";
  try {
    const r = await api("/api/lifecycle/rotate", { method: "POST" });
    if (msg) msg.textContent = r.switched ? ("انجام شد: " + (r.reason || "")) : ("انجام نشد: " + (r.reason || ""));
    toast(r.switched ? "چرخش انجام شد" : "چرخش انجام نشد", !r.switched); await refreshAll();
  } catch (e) { if (msg) msg.textContent = e.message; }
});

/* ---------- telemetry ---------- */
function drawChart(canvasId, points, fmt) {
  const cv = $(canvasId); if (!cv) return;
  const ctx = cv.getContext("2d"); const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--muted") || "#93a1c0";
  ctx.font = "11px sans-serif";
  if (!points.length) { ctx.fillText("داده‌ای نیست", 10, 20); return; }
  const padL = 44, padB = 22, padT = 14;
  const mx = Math.max(...points.map((p) => p.v), 1);
  const iw = W - padL - 8, ih = H - padB - padT;
  ctx.strokeStyle = "rgba(148,163,184,.35)"; ctx.beginPath();
  for (let g = 0; g <= 4; g++) { const y = padT + (ih * g) / 4; ctx.moveTo(padL, y); ctx.lineTo(W - 8, y); }
  ctx.stroke();
  ctx.fillText(fmt(mx), 4, padT + 8);
  ctx.fillText(fmt(0), 4, padT + ih);
  const bw = iw / points.length;
  ctx.fillStyle = "#38bdf8";
  points.forEach((p, i) => { const h = (p.v / mx) * ih; ctx.fillRect(padL + i * bw + 2, padT + ih - h, Math.max(1, bw - 4), h); });
  const lbl = (p) => (p.label || "").slice(11, 16);
  ctx.fillText(lbl(points[0]), padL, H - 6);
  ctx.fillText(lbl(points[Math.floor(points.length / 2)]), padL + iw / 2 - 12, H - 6);
  ctx.fillText(lbl(points[points.length - 1]), W - 44, H - 6);
}
async function loadTelemetry() {
  try {
    const node = $("telNode").value, type = $("telType").value, oc = $("telOutcome").value, hours = +$("telHours").value || 24;
    if ($("telNode").options.length <= 1) {
      $("telNode").innerHTML = `<option value="">همه نودها</option>` + NODES.map((n) => `<option value="${n.id}">${esc(n.name)}</option>`).join("");
    }
    const q = new URLSearchParams({ hours: String(hours), bucket_minutes: "60", limit: "200" });
    if (node) q.set("node_id", node);
    if (type) q.set("probe_type", type);
    const [sum, ser] = await Promise.all([
      api("/api/telemetry/summary?" + new URLSearchParams({ hours: String(hours), ...(node ? { node_id: node } : {}), ...(type ? { probe_type: type } : {}) })),
      api("/api/telemetry/series?" + q.toString()),
    ]);
    const rows = ser.rows.filter((r) => !oc || (oc === "success" ? r.ok : !r.ok));
    $("telSummary").innerHTML = sum.total
      ? `<table><tr><td>بازه</td><td dir="ltr">${sum.window_hours}h (${esc(sum.from)} تا ${esc(sum.to)})</td></tr>
         <tr><td>کل نمونه‌ها</td><td dir="ltr">${sum.total} (موفق ${sum.ok} · نرخ ${sum.success_rate ?? "—"})</td></tr>
         <tr><td>تأخیر p50/p95/p99</td><td dir="ltr">${sum.p50 ?? "—"} / ${sum.p95 ?? "—"} / ${sum.p99 ?? "—"} ms (نمونه ${sum.latency_samples})</td></tr>
         <tr><td>کفایت نمونه</td><td>${sum.sufficient_for_percentiles ? "کافی (≥20)" : "ناکافی — درصدها معتبر نیست"}</td></tr></table>`
      : emptyBox("داده‌ای در این بازه نیست (صفر نمونه ≠ صفر خطا).");
    drawChart("telLatencyChart", ser.series.map((s) => ({ v: s.avg_latency || 0, label: s.bucket })), (v) => Math.round(v) + "ms");
    $("telLatencyNote").textContent = ser.series.length ? `میانگین هر بازه؛ ${ser.series.length} بازه.` : "";
    drawChart("telSuccessChart", ser.series.map((s) => ({ v: Math.round((s.success_rate ?? 0) * 100), label: s.bucket })), (v) => v + "٪");
    $("telTable").querySelector("tbody").innerHTML = rows.slice(0, 100).map((r) => `<tr><td dir="ltr">${esc(r.at || "")}</td><td>${esc(r.node_name)}</td><td>${esc(r.probe_type)}</td><td>${r.ok ? dot("ok") + " موفق" : dot("bad") + " ناموفق"}</td><td dir="ltr">${r.latency_ms ?? "—"}</td><td class="wrap">${esc(r.error_type || "")}</td></tr>`).join("")
      || `<tr><td colspan="6">ردیفی با این فیلتر نیست.</td></tr>`;
  } catch (e) { errBox($("telSummary"), e.message, loadTelemetry); }
}
$("telFilterBtn").onclick = loadTelemetry;
$("telResetBtn").onclick = () => { $("telNode").value = ""; $("telType").value = ""; $("telOutcome").value = ""; $("telHours").value = "24"; loadTelemetry(); };

/* ---------- connections (columns match the API contract) ---------- */
async function loadConnections() {
  try {
    const c = await api("/api/connections?limit=100");
    const L = c.listeners || {};
    const lip = (k, label) => `<tr><td>${label}</td><td>${L[k] ? dot("ok") + " باز" : dot("off") + " بسته/نامشخص"}</td></tr>`;
    $("connInfo").innerHTML = `<table>
      ${lip("http", "شنونده SOCKS/HTTP لوکال (1081/2081)")}
      ${lip("socks", "شنونده SOCKS لوکال")}
      ${lip("gw-http", "شنونده gateway HTTP (8389)")}
      ${lip("gw-socks", "شنونده gateway SOCKS (8388)")}
      <tr><td>خروجی فعال</td><td>${c.active_outbound ? esc(c.active_outbound.name) + " " + protoBadge(c.active_outbound.protocol) : "—"}</td></tr></table>
      <p class="muted">${esc(c.limitation || "")}</p>${c.error ? `<p class="warn">خطای جزئی منبع: ${esc(c.error)}</p>` : ""}`;
    const rows = c.recent || [];
    $("connTable").querySelector("tbody").innerHTML = rows.map((r) => `<tr><td dir="ltr">${esc(r.at || "")}</td><td dir="ltr">${esc(r.client || "")}</td><td dir="ltr">${esc(r.dest || "")}</td><td dir="ltr">${r.port ?? "—"}</td><td dir="ltr">${esc(r.inbound || "")}</td><td dir="ltr">${esc(r.outbound || "")}</td></tr>`).join("")
      || `<tr><td colspan="6">اتصال اخیری ثبت نشده است.</td></tr>`;
  } catch (e) { errBox($("connInfo"), e.message, loadConnections); }
}
$("connReloadBtn").onclick = (ev) => busy(ev.target, loadConnections);

/* ---------- logs ---------- */
const LOG_ACTIONS = ["login.ok", "login.failed", "node.activate", "subscription.update", "rule.create", "rule.update", "rule.delete", "client.create", "client.rotate", "settings.update", "routing.create"];
$("logAction").innerHTML = `<option value="">همه رویدادها</option>` + LOG_ACTIONS.map((a) => `<option>${a}</option>`).join("");
async function loadLogs() {
  try {
    const q = new URLSearchParams({ limit: 100 });
    if ($("logAction").value) q.set("action", $("logAction").value);
    if ($("logActor").value.trim()) q.set("actor", $("logActor").value.trim());
    const rows = await api("/api/audit?" + q.toString());
    $("eventList").innerHTML = rows.length
      ? `<div class="tablewrap"><table><thead><tr><th>زمان</th><th>عامل</th><th>رویداد</th><th>جزئیات</th></tr></thead><tbody>
        ${rows.map((e) => `<tr><td dir="ltr">${esc(e.at || "")}</td><td>${esc(e.actor)}</td><td dir="ltr">${esc(e.action)}</td><td class="wrap">${esc(e.detail)}</td></tr>`).join("")}</tbody></table></div>`
      : emptyBox("رویدادی با این فیلتر نیست.");
  } catch (e) { errBox($("eventList"), e.message, loadLogs); }
}
$("logBtn").onclick = (ev) => busy(ev.target, loadLogs);

/* ---------- system & settings ---------- */
async function loadSystem() {
  try {
    const s = await api("/api/system");
    SYS = s;
    SINGBOX_INSTALLED = !!(s.cores && s.cores.singbox && s.cores.singbox.installed);
    $("sysInfo").innerHTML = `<table>
      <tr><td>Xray سیستمی</td><td dir="ltr">${esc(s.services.system_xray.state)}</td></tr>
      <tr><td>Xray مدیریت‌شده</td><td dir="ltr">${esc(s.services.managed_xray.state)}</td></tr>
      <tr><td>API</td><td dir="ltr">${esc(s.listeners.api)}</td></tr>
      <tr><td>SOCKS مدیریت‌شده</td><td dir="ltr">${esc(s.listeners.managed_socks)}</td></tr>
      <tr><td>HTTP مدیریت‌شده</td><td dir="ltr">${esc(s.listeners.managed_http)}</td></tr></table>`;
    $("coreInfo").innerHTML = `<table>
      <tr><td>باینری Xray</td><td dir="ltr">${esc(s.cores.xray.bin)} (${s.cores.xray.present ? "موجود" : "یافت نشد"})</td></tr>
      <tr><td>باینری sing-box</td><td dir="ltr">${esc(s.cores.singbox.bin)} (${SINGBOX_INSTALLED ? "نصب شده" : "نصب نشده"})</td></tr></table>
      ${SINGBOX_INSTALLED ? `<p class="muted">sing-box برای اجرای Hysteria2 لازم است و نصب است.</p>`
        : `<div class="alert warn">sing-box نصب نیست: نودهای Hysteria2 فقط parse می‌شوند، verify سرتاسری نمی‌شوند و واجد چرخش/failover نیستند. تا نصب نشود Hysteria2 را عملیاتی فرض نکنید. (مستندات: <code>docs/PROXY_CORES.md</code>)</div>`}`;
    updateSysBanner();
  } catch (e) { errBox($("sysInfo"), e.message, loadSystem); }
}
async function loadSettings() {
  try {
    const s = await api("/api/settings");
    $("settingsForm").innerHTML = `
      <label>فاصله اشتراک (ثانیه)<input id="s_sub" type="number" dir="ltr" value="${s.sub_update_interval}" min="1"></label>
      <label>فاصله سلامت (ثانیه)<input id="s_health" type="number" dir="ltr" value="${s.health_interval}" min="1"></label>
      <label>آدرس تست سلامت<input id="s_url" dir="ltr" value="${esc(s.health_url)}"></label>
      <label>حالت failover<select id="s_mode"><option value="auto">خودکار</option><option value="manual">دستی</option></select></label>
      <label>آستانه خطا<input id="s_thr" type="number" dir="ltr" value="${s.failover_threshold}" min="1"></label>
      <label>کول‌داون (ثانیه)<input id="s_cd" type="number" dir="ltr" value="${s.failover_cooldown}" min="1"></label>`;
    $("s_mode").value = s.failover_mode;
    $("rotationForm").innerHTML = `
      <label class="check"><input type="checkbox" id="r_en" ${s.rotation_enabled ? "checked" : ""}> چرخش زمان‌بندی‌شده فعال</label>
      <label>حداکثر ماندگاری هر نود (ساعت)<input id="r_max" type="number" dir="ltr" value="${s.rotation_max_duration_hours}" min="1" max="168"></label>
      <label>پنجره عدم تکرار (ساعت)<input id="r_excl" type="number" dir="ltr" value="${s.rotation_exclusion_hours}" min="1" max="168"></label>
      <label>فاصله بررسی (ثانیه)<input id="r_int" type="number" dir="ltr" value="${s.rotation_check_interval}" min="1"></label>
      <label class="check"><input type="checkbox" id="r_dry" ${s.rotation_dry_run ? "checked" : ""}> حالت dry-run (فقط گزارش، بدون جابه‌جایی)</label>`;
    $("gwForm").innerHTML = `
      <label class="check"><input type="checkbox" id="g_en" ${s.gateway_enabled ? "checked" : ""}> فعال‌سازی gateway</label>
      <label>bind<select id="g_bind"><option value="127.0.0.1">127.0.0.1 (لوکال/تست)</option><option value="0.0.0.0">0.0.0.0 (عمومی)</option></select></label>
      <label>پورت SOCKS<input id="g_sp" type="number" dir="ltr" value="${s.public_socks_port}" min="1024" max="65535"></label>
      <label>پورت HTTP<input id="g_hp" type="number" dir="ltr" value="${s.public_http_port}" min="1024" max="65535"></label>
      <label>مسیر پیش‌فرض<select id="g_def"><option value="proxy">PROXY</option><option value="direct">DIRECT</option><option value="block">BLOCK</option></select></label>`;
    $("g_bind").value = s.gateway_bind; $("g_def").value = s.default_route;
    window._gwOrig = { en: s.gateway_enabled, bind: s.gateway_bind };
  } catch (e) { errBox($("settingsForm"), e.message, loadSettings); }
}
$("saveSettingsBtn").onclick = (ev) => busy(ev.target, async () => {
  try { await api("/api/settings", { method: "PUT", body: JSON.stringify({
      sub_update_interval: +$("s_sub").value, health_interval: +$("s_health").value, health_url: $("s_url").value,
      failover_mode: $("s_mode").value, failover_threshold: +$("s_thr").value, failover_cooldown: +$("s_cd").value }) });
    $("settingsMsg").textContent = "ذخیره شد."; toast("تنظیمات ذخیره شد");
  } catch (e) { $("settingsMsg").textContent = e.message; }
});
$("saveRotationBtn").onclick = (ev) => busy(ev.target, async () => {
  try { await api("/api/settings", { method: "PUT", body: JSON.stringify({
      rotation_enabled: $("r_en").checked, rotation_max_duration_hours: +$("r_max").value,
      rotation_exclusion_hours: +$("r_excl").value, rotation_check_interval: +$("r_int").value,
      rotation_dry_run: $("r_dry").checked }) });
    $("rotationMsg").textContent = "ذخیره شد."; toast("چرخه حیات ذخیره شد");
  } catch (e) { $("rotationMsg").textContent = e.message; }
});
$("saveGwBtn").onclick = (ev) => busy(ev.target, async () => {
  const en = $("g_en").checked, bind = $("g_bind").value;
  if (en && bind === "0.0.0.0" && !(window._gwOrig.en && window._gwOrig.bind === "0.0.0.0")) {
    if (!await confirmDlg("هشدار: gateway روی 0.0.0.0 عمومی می‌شود. فقط کلاینت‌های دارای اعتبار می‌توانند وصل شوند. ادامه می‌دهید؟")) return;
  }
  try { await api("/api/settings", { method: "PUT", body: JSON.stringify({ gateway_enabled: en, gateway_bind: bind,
      public_socks_port: +$("g_sp").value, public_http_port: +$("g_hp").value, default_route: $("g_def").value }) });
    $("gwMsg").textContent = "ذخیره و بازسازی شد."; toast("gateway به‌روز شد"); await loadDash();
  } catch (e) { $("gwMsg").textContent = e.message; }
});

/* ---------- refresh: parallel with per-section isolation ---------- */
async function refreshAll() {
  showTab(location.hash.replace("#/", "") || "dash", false);
  skeleton($("dashCards"), 3);
  /* Order matters only for GW-dependent renders; system first for sing-box flag. */
  await loadSystem().catch(() => {});
  await Promise.allSettled([
    loadDash(), loadNodes(), loadClients().then(() => loadRules()).catch(() => loadRules()),
    loadAppRules(), loadSub(), loadHealth(), loadHealthCycles(),
    loadLifecycle(), loadTelemetry(), loadConnections(), loadLogs(), loadSettings(),
  ]);
}
async function nodeActSafe(id, act) {
  if (act === "activate") {
    const n = NODES.find((x) => x.id === id);
    if (n && n.protocol === "hysteria2" && SINGBOX_INSTALLED === false) {
      toast("این نود Hysteria2 است اما sing-box نصب نیست؛ فعال‌سازی واقعی انجام نمی‌شود.", true); return;
    }
    if (!await confirmDlg(`نود «${n ? n.name : id}» فعال شود؟ اتصال فعلی جابه‌جا می‌شود.`)) return;
  }
  await nodeAct(id, act);
}
/* Delegated handler for all dynamically-generated buttons (CSP: no inline onclick). */
document.addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-action]");
  if (!el || el.disabled) return;
  const act = el.dataset.action, id = +(el.dataset.id || 0);
  switch (act) {
    case "node-activate": nodeActSafe(id, "activate"); break;
    case "node-test": busy(el, () => nodeAct(id, "test")); break;
    case "node-toggle": nodeToggle(id, el.dataset.enabled === "1"); break;
    case "node-rename": nodeRename(id); break;
    case "node-delete": nodeDelete(id); break;
    case "node-restore": nodeRestore(id); break;
    case "rule-move": ruleMove(id, el.dataset.dir); break;
    case "rule-edit": ruleEdit(id); break;
    case "rule-dup": ruleDup(id); break;
    case "rule-del": ruleDel(id); break;
    case "approute-del": delAppRoute(id); break;
    case "client-config": clientConfig(id); break;
    case "client-qr": clientQR(id); break;
    case "client-edit": clientEdit(id); break;
    case "client-rotate": clientRotate(id); break;
    case "client-toggle": clientToggle(id, el.dataset.enabled === "1"); break;
    case "client-del": clientDel(id); break;
    case "copy-summary": copyText(el.dataset.text || ""); break;
  }
});
(async () => { initTheme(); CSRF = getCsrf();
  try { await api("/api/auth/me"); showApp(); await refreshAll(); } catch { showLogin(); } })();
