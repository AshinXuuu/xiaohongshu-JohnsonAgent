/* ============================================================
   乔山销售助手 · 共享前端外壳(app.js)
   把每个页面重复的东西收成一处:
   - 登录态 / 会话校验 / 退出
   - 统一顶栏 + 模块导航(读组织配置,按角色显示)
   - 换肤(读组织主色)
   - toast / escapeHtml / fetch 封装
   用法见各页面底部:App.requireSession() + App.mountTopbar({active:'library'})
   ============================================================ */
(function (global) {
  "use strict";

  var USER_KEY = "xhsAgent:user";
  var SESSION_TTL_MS = 24 * 60 * 60 * 1000; // 24h
  var _org = null;

  // ---------- 工具 ----------
  function escapeHtml(s) {
    if (s == null) return "";
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  var _toastTimer;
  function toast(msg) {
    var t = document.getElementById("appToast");
    if (!t) {
      t = document.createElement("div");
      t.id = "appToast";
      t.className = "app-toast";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(function () { t.classList.remove("show"); }, 2200);
  }

  async function api(path, opts) {
    opts = opts || {};
    if (opts.body && typeof opts.body !== "string") {
      opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
      opts.body = JSON.stringify(opts.body);
    }
    var resp = await fetch(path, opts);
    var data = null;
    try { data = await resp.json(); } catch (_) { data = null; }
    return { ok: resp.ok, status: resp.status, data: data };
  }

  // ---------- 登录态 ----------
  function getMe() {
    try { return JSON.parse(localStorage.getItem(USER_KEY) || "null"); }
    catch (_) { return null; }
  }
  function setMe(u) {
    u.loginAt = Date.now();
    localStorage.setItem(USER_KEY, JSON.stringify(u));
  }
  function isSessionValid(me) {
    return !!(me && me.emp_id && me.loginAt && (Date.now() - me.loginAt) < SESSION_TTL_MS);
  }
  function logout() { localStorage.removeItem(USER_KEY); location.replace("/"); }

  // 会话无效直接跳门户;有效则返回 me
  function requireSession() {
    var me = getMe();
    if (!isSessionValid(me)) {
      if (me) localStorage.removeItem(USER_KEY);
      location.replace("/");
      return null;
    }
    return me;
  }

  // 角色:兼容老数据(is_admin=true 视为 org_admin),默认 staff
  function roleOf(me) {
    if (!me) return "staff";
    if (me.role) return me.role;
    return me.is_admin ? "org_admin" : "staff";
  }
  var ROLE_LABEL = { super_admin: "超管", org_admin: "管理员", dept_manager: "部门管理", staff: "" };

  // ---------- 组织配置 ----------
  async function loadOrg() {
    if (_org) return _org;
    var r = await api("/api/org");
    _org = (r.ok && r.data) ? r.data : { name: "销售助手", theme: {}, modules: [], admin: {} };
    applyTheme(_org);
    return _org;
  }
  function applyTheme(org) {
    var t = (org && org.theme) || {};
    var root = document.documentElement.style;
    if (t.accent) root.setProperty("--accent", t.accent);
    if (t.accent_strong) root.setProperty("--accent-strong", t.accent_strong);
    if (t.accent_soft) root.setProperty("--accent-soft", t.accent_soft);
  }

  function roleCan(roles, role) {
    if (!roles || !roles.length) return true;
    return roles.indexOf(role) !== -1;
  }

  // ---------- 统一顶栏 ----------
  // mountTopbar({active:'library'}) → 在 #appTopbar(或 body 顶部)渲染顶栏
  async function mountTopbar(opts) {
    opts = opts || {};
    var me = getMe() || {};
    var role = roleOf(me);
    var org = await loadOrg();

    var brandInner = org.logo
      ? '<img class="app-logo" src="' + escapeHtml(org.logo) + '" alt="' + escapeHtml(org.name) + '" />'
      : escapeHtml(org.name || "销售助手");

    var navHtml = (org.modules || [])
      .filter(function (m) { return m.enabled !== false && roleCan(m.roles, role); })
      .map(function (m) {
        var active = (opts.active && m.key === opts.active) ? " active" : "";
        return '<a class="app-nav-link' + active + '" href="' + escapeHtml(m.href) + '">' + escapeHtml(m.name) + "</a>";
      }).join("");

    var roleLabel = ROLE_LABEL[role] || "";
    var userHtml = me.name
      ? '<span class="app-user">' + escapeHtml((me.department || "") + (me.department ? " · " : "") + me.name) +
        (roleLabel ? '<span class="app-role">' + escapeHtml(roleLabel) + "</span>" : "") + "</span>"
      : "";

    var adminHtml = (org.admin && roleCan(org.admin.roles, role))
      ? '<a class="btn" href="' + escapeHtml(org.admin.href || "/admin.html") + '">管理后台</a>'
      : "";

    var html =
      '<a class="app-brand" href="/">' + brandInner + "</a>" +
      '<nav class="app-nav">' + navHtml + "</nav>" +
      '<div class="app-spacer"></div>' +
      userHtml + adminHtml +
      '<button class="btn" id="appLogoutBtn">退出</button>';

    var bar = document.getElementById("appTopbar");
    if (!bar) {
      bar = document.createElement("header");
      bar.id = "appTopbar";
      document.body.insertBefore(bar, document.body.firstChild);
    }
    bar.className = "app-topbar";
    bar.innerHTML = html;
    var lo = document.getElementById("appLogoutBtn");
    if (lo) lo.onclick = logout;
    return me;
  }

  global.App = {
    USER_KEY: USER_KEY,
    escapeHtml: escapeHtml,
    toast: toast,
    api: api,
    getMe: getMe,
    setMe: setMe,
    isSessionValid: isSessionValid,
    requireSession: requireSession,
    logout: logout,
    roleOf: roleOf,
    loadOrg: loadOrg,
    mountTopbar: mountTopbar,
  };
})(window);
