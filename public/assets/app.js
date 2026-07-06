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
  var TOKEN_KEY = "xhsAgent:token";
  var SESSION_TTL_MS = 24 * 60 * 60 * 1000; // 24h
  var _org = null;

  // ---------- 会话 Token ----------
  function getToken() { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (_) { return ""; } }
  function setToken(t) { try { if (t) localStorage.setItem(TOKEN_KEY, t); } catch (_) {} }
  function clearToken() { try { localStorage.removeItem(TOKEN_KEY); } catch (_) {} }

  // 全局给所有 /api/ 请求自动带上 Authorization: Bearer <token>。
  // 覆盖所有页面(含各页自定义 fetch 与 App.api),后端据此验签鉴权,不再信任请求体身份。
  (function patchFetch() {
    var _origFetch = global.fetch ? global.fetch.bind(global) : null;
    if (!_origFetch) return;
    global.fetch = function (url, opts) {
      opts = opts || {};
      try {
        if (String(url).indexOf("/api/") !== -1) {
          var tok = getToken();
          if (tok) {
            opts.headers = Object.assign({}, opts.headers || {});
            if (!opts.headers["Authorization"] && !opts.headers["authorization"]) {
              opts.headers["Authorization"] = "Bearer " + tok;
            }
          }
        }
      } catch (_) {}
      return _origFetch(url, opts);
    };
  })();

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
  function logout() { localStorage.removeItem(USER_KEY); clearToken(); location.replace("/"); }

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

    // opts.extra:页面专属按钮(如文案页的「历史记录」),插在退出按钮之前
    var extraHtml = opts.extra || "";

    var html =
      '<a class="app-brand" href="/">' + brandInner + "</a>" +
      '<nav class="app-nav">' + navHtml + "</nav>" +
      '<div class="app-spacer"></div>' +
      userHtml + adminHtml + extraHtml +
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

  // ---------- 通用分页(后台列表统一:每页 10 行,上一页/数字页码/下一页) ----------
  var PAGE_SIZE = 10;

  // 取窗口化页码序列:总页少直接全列,多则 1 … 邻近页 … 末页('…' 为省略占位)
  function pageWindow(page, pages) {
    if (pages <= 7) return Array.from({ length: pages }, function (_, i) { return i; });
    var out = [0];
    var lo = Math.max(1, page - 1), hi = Math.min(pages - 2, page + 1);
    if (lo > 1) out.push("…");
    for (var i = lo; i <= hi; i++) out.push(i);
    if (hi < pages - 2) out.push("…");
    out.push(pages - 1);
    return out;
  }

  // 在容器里渲染分页条。page 从 0 起;onGo(newPage) 由调用方负责重渲染。
  // 单页(pages<=1)自动清空容器,不占位。
  function renderPager(el, page, pages, onGo) {
    if (typeof el === "string") el = document.getElementById(el);
    if (!el) return;
    if (!pages || pages <= 1) { el.innerHTML = ""; return; }
    var btn = function (label, target, opts) {
      opts = opts || {};
      var b = document.createElement("button");
      b.textContent = label;
      b.disabled = !!opts.disabled;
      b.style.cssText =
        "min-width:30px;padding:5px 9px;border:1px solid " + (opts.active ? "#6366f1" : "#e4e4e7") +
        ";background:" + (opts.active ? "#6366f1" : "#fff") + ";color:" + (opts.active ? "#fff" : "#3f3f46") +
        ";border-radius:7px;font-size:12.5px;cursor:" + (opts.disabled || opts.ellipsis ? "default" : "pointer") +
        ";font-family:inherit;" + (opts.disabled ? "opacity:.45;" : "") + (opts.ellipsis ? "border:none;background:none;" : "");
      if (!opts.disabled && !opts.ellipsis && !opts.active) b.onclick = function () { onGo(target); };
      return b;
    };
    el.innerHTML = "";
    el.style.display = "flex";
    el.style.flexWrap = "wrap";
    el.style.alignItems = "center";
    el.style.gap = "6px";
    el.appendChild(btn("上一页", page - 1, { disabled: page <= 0 }));
    pageWindow(page, pages).forEach(function (p) {
      if (p === "…") el.appendChild(btn("…", 0, { ellipsis: true, disabled: true }));
      else el.appendChild(btn(String(p + 1), p, { active: p === page }));
    });
    el.appendChild(btn("下一页", page + 1, { disabled: page >= pages - 1 }));
    var info = document.createElement("span");
    info.textContent = "共 " + pages + " 页";
    info.style.cssText = "font-size:12px;color:#71717a;margin-left:4px";
    el.appendChild(info);
  }

  // 客户端分页切片:返回 {rows, page, pages}(page 自动夹到合法范围)
  function paginate(list, page, per) {
    per = per || PAGE_SIZE;
    var pages = Math.max(1, Math.ceil((list || []).length / per));
    var p = Math.min(Math.max(0, page || 0), pages - 1);
    return { rows: (list || []).slice(p * per, p * per + per), page: p, pages: pages };
  }

  global.App = {
    USER_KEY: USER_KEY,
    escapeHtml: escapeHtml,
    PAGE_SIZE: PAGE_SIZE,
    paginate: paginate,
    renderPager: renderPager,
    toast: toast,
    api: api,
    getMe: getMe,
    setMe: setMe,
    getToken: getToken,
    setToken: setToken,
    clearToken: clearToken,
    isSessionValid: isSessionValid,
    requireSession: requireSession,
    logout: logout,
    roleOf: roleOf,
    loadOrg: loadOrg,
    mountTopbar: mountTopbar,
  };
})(window);
