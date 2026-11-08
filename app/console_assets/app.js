"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  csrf: "",
  snapshot: null,
  models: [],
  rows: [],
  page: 1,
  total: 0,
  view: "overview",
  busy: false,
  loginTool: null,
  job: null,
};
const names = {
  overview: "总览",
  requests: "调用记录",
  models: "模型目录",
  accounts: "账户管理",
  keys: "API 密钥",
  mail: "邮件告警",
  settings: "设置",
};
const fmt = (value) =>
  value === null || value === undefined
    ? "—"
    : Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 6 });
const time = (value) => {
  if (!value) return "—";
  const d = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(d.getTime())
    ? String(value)
    : new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }).format(d);
};
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") node.textContent = v;
    else if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of [].concat(children)) {
    if (child !== null && child !== undefined)
      node.append(
        typeof child === "string" ? document.createTextNode(child) : child,
      );
  }
  return node;
}
function set(id, value) {
  $(id).textContent = value;
}
function toast(message) {
  set("notice", message);
  $("notice").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => ($("notice").hidden = true), 5000);
}
function loginView() {
  state.csrf = "";
  $("workspace").hidden = true;
  $("login-view").hidden = false;
}
async function api(path, data) {
  const options = {
    credentials: "same-origin",
    cache: "no-store",
    headers: {},
  };
  if (data !== undefined) {
    options.method = "POST";
    options.headers = {
      "Content-Type": "application/json",
      "X-Console-CSRF": state.csrf,
    };
    options.body = JSON.stringify(data);
  }
  let response;
  try {
    response = await fetch("/console/api/" + path, options);
  } catch (e) {
    throw new Error("连接失败，请检查网络后重试");
  }
  let result;
  try {
    result = await response.json();
  } catch (e) {
    throw new Error("服务器响应异常，请稍后重试");
  }
  if (response.status === 401 && path !== "login") {
    loginView();
    throw new Error("登录已失效，请重新登录");
  }
  if (!response.ok) throw new Error(result.error || "操作失败");
  return result;
}
function badge(text, type = "") {
  return el("span", { class: "badge " + type, text });
}
function empty(target, text) {
  target.replaceChildren(el("div", { class: "empty", text }));
}
function actionButton(text, action, extra = {}) {
  return el("button", {
    class: "button",
    text,
    onclick: async (event) => {
      const b = event.currentTarget;
      if (extra.confirm && !confirm(extra.confirm)) return;
      b.disabled = true;
      try {
        await api("action", action);
        toast("操作完成");
        await refresh();
      } catch (e) {
        toast(e.message);
      } finally {
        b.disabled = false;
      }
    },
  });
}
async function establish() {
  try {
    const me = await api("me");
    state.csrf = me.csrf;
    $("login-view").hidden = true;
    $("workspace").hidden = false;
    await refresh();
  } catch (e) {
    if (state.csrf) toast(e.message);
    else loginView();
  }
}
function navigate(view) {
  state.view = view;
  for (const node of document.querySelectorAll(".view"))
    node.hidden = node.id !== "view-" + view;
  for (const node of document.querySelectorAll("[data-view]"))
    node.classList.toggle("active", node.dataset.view === view);
  set("breadcrumb", names[view]);
  if (view === "requests") loadRequests().catch((e) => toast(e.message));
  if (view === "keys") loadKeys().catch((e) => toast(e.message));
  if (view === "mail") loadMail().catch((e) => toast(e.message));
}
for (const node of document.querySelectorAll("[data-view]"))
  node.addEventListener("click", () => navigate(node.dataset.view));
for (const node of document.querySelectorAll("[data-open]"))
  node.addEventListener("click", () => navigate(node.dataset.open));
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget,
    button = form.querySelector("button");
  button.disabled = true;
  set("login-error", "");
  try {
    await api("login", {
      username: form.elements.username.value.trim(),
      password: form.elements.password.value,
    });
    form.elements.password.value = "";
    await establish();
  } catch (e) {
    set("login-error", e.message);
  } finally {
    button.disabled = false;
  }
});
$("logout").addEventListener("click", async () => {
  try {
    await api("logout", {});
    loginView();
  } catch (e) {
    toast(e.message);
  }
});
$("refresh").addEventListener("click", () =>
  refresh().catch((e) => toast(e.message)),
);
$("copy-url").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(
      state.snapshot?.base_url || location.origin + "/v1",
    );
    toast("API 地址已复制");
  } catch (e) {
    toast("请手动复制 API 地址");
  }
});
async function refresh() {
  if (state.busy || !state.csrf) return;
  state.busy = true;
  $("refresh").disabled = true;
  try {
    const [snapshot, models, requests] = await Promise.all([
      api("snapshot"),
      api("models"),
      api("requests?page=1"),
    ]);
    state.snapshot = snapshot;
    state.models = models.models;
    state.catalog = models.catalog;
    state.benchmark_updated_at = models.benchmark_updated_at;
    state.rows = requests.rows;
    renderOverview();
    renderAccounts();
    renderModels();
    renderRequests($("recent-requests"), state.rows.slice(0, 5), true);
    set("freshness", "更新于 " + time(snapshot.updated_at).slice(-8));
    if (state.view === "requests") await loadRequests();
  } catch (e) {
    set("freshness", "刷新失败");
    throw e;
  } finally {
    state.busy = false;
    $("refresh").disabled = false;
  }
}
function renderOverview() {
  const s = state.snapshot,
    u = s.usage,
    accounts = s.accounts || [];
  set("stat-requests", fmt(u.requests));
  set(
    "stat-success",
    u.requests
      ? `成功 ${fmt(u.successful)} 次 · ${Math.round((u.successful / u.requests) * 100)}%`
      : "今天还没有调用",
  );
  set("stat-credit", fmt(u.credit));
  set(
    "stat-unknown",
    u.unknown_attempts
      ? `${u.unknown_attempts} 次尝试积分未知`
      : `缓存命中 ${u.cache_hit_ratio === null || u.cache_hit_ratio === undefined ? "未知" : (u.cache_hit_ratio * 100).toFixed(1) + "%"}`,
  );
  const known = accounts.filter(
    (a) => a.credits !== null && a.credits !== undefined,
  );
  set(
    "stat-balance",
    known.length ? fmt(known.reduce((v, a) => v + Number(a.credits), 0)) : "—",
  );
  set("stat-models", fmt(state.models.length));
  set(
    "stat-accounts",
    `${accounts.length} 个账户 · API ${accounts.filter((a) => a.api_enabled).length} / 签到 ${accounts.filter((a) => a.checkin_enabled).length}`,
  );
  $("ledger-warning").hidden = !u.write_error;
  set("base-url", s.base_url);
  set("online-badge", "● 统一账户服务");
  $("online-badge").className = "badge";
  const batches = accounts
    .flatMap((a) =>
      (a.expiry_batches || []).map((b) => ({ ...b, name: a.nickname || a.id })),
    )
    .filter((b) => Number(b.expires_at || b.expiry) > Date.now() / 1000)
    .sort(
      (a, b) =>
        Number(a.expires_at || a.expiry) - Number(b.expires_at || b.expiry),
    );
  const exp = accounts.filter(
    (a) =>
      a.credits_expiring_7d !== null && a.credits_expiring_7d !== undefined,
  );
  set(
    "expiry-amount",
    exp.length
      ? fmt(exp.reduce((sum, a) => sum + Number(a.credits_expiring_7d), 0)) +
          " 积分"
      : "待查询",
  );
  const earliest = accounts
    .filter((a) => a.credits_earliest_expiry)
    .sort(
      (a, b) =>
        new Date(a.credits_earliest_expiry) -
        new Date(b.credits_earliest_expiry),
    )[0];
  set(
    "expiry-date",
    batches.length
      ? time(Number(batches[0].expires_at || batches[0].expiry))
      : earliest
        ? time(earliest.credits_earliest_expiry)
        : "暂无已知到期批次",
  );
  set(
    "expiry-detail",
    batches.length
      ? `${batches.length} 个批次 · 最早 ${fmt(batches[0].amount)} 积分`
      : "在账户页刷新余额查询",
  );
  const list = $("service-list");
  list.replaceChildren();
  for (const [name, unit] of [
    ["模型 API / 账户池", "workbuddy-python.service"],
    ["自动签到计划", "workbuddy-go-checkin.timer"],
    ["公网连接", "cloudflared-workbuddy.service"],
  ]) {
    const active = s.units[unit]?.ActiveState === "active";
    list.append(
      el("div", { class: "service-row" }, [
        el("span", { text: name }),
        badge(active ? "在线" : "待检查", active ? "" : "warn"),
      ]),
    );
  }
  const timer = s.units["workbuddy-go-checkin.timer"] || {},
    check = s.units["workbuddy-go-checkin.service"] || {};
  set(
    "last-checkin",
    check.ExecMainExitTimestamp
      ? `${time(check.ExecMainExitTimestamp)} · ${check.Result === "success" && check.ExecMainStatus === "0" ? "执行成功" : "请检查结果"}`
      : "暂无定时执行记录",
  );
  set(
    "next-checkin",
    timer.ActiveState === "active"
      ? time(timer.NextElapseUSecRealtime)
      : "定时任务未启用",
  );
  set(
    "ledger-note",
    `账本始于 ${time(u.started_at)}，保留最近 ${u.retention_days} 天。新记录包含账户、客户端密钥、思考档位和上游缓存统计；历史缺失字段显示未知。`,
  );
}
function accountName(id) {
  const a = state.snapshot?.accounts?.find((a) => a.id === id);
  return a?.nickname || id?.slice(-10) || "未知账户";
}
function renderRequests(target, rows, compact = false) {
  if (!rows.length) {
    empty(target, "尚无调用记录");
    return;
  }
  const wrap = el("div", { class: "table-wrap" }),
    table = el("table"),
    head = el("tr");
  for (const text of compact
    ? ["时间", "模型 / 账户", "结果", "缓存命中", "积分"]
    : [
        "时间 / 请求ID",
        "模型 / 账户 / 客户端",
        "思考档位",
        "结果",
        "Token / 缓存",
        "积分",
        "上游尝试",
      ])
    head.append(el("th", { text }));
  table.append(el("thead", {}, [head]));
  const tbody = el("tbody");
  for (const r of rows) {
    const tr = el("tr"),
      attempts = r.attempts || [],
      ok = r.status >= 200 && r.status < 300 && r.outcome === "completed";
    const date = el("td", { text: time(r.ts) });
    if (!compact)
      date.append(
        el("div", { class: "subtle mono", text: r.request_id.slice(0, 12) }),
      );
    const model = el("td", {}, [
      el("span", { class: "mono", text: r.model || "未识别" }),
      el("div", {
        class: "subtle",
        text:
          [...new Set(attempts.map((a) => accountName(a.account)))].join(
            " / ",
          ) || "未请求上游",
      }),
    ]);
    if (!compact)
      model.append(
        el("div", {
          class: "subtle",
          text: [
            ...new Set(
              attempts.map((a) => a.client_key_name || "历史密钥未知"),
            ),
          ].join(" / "),
        }),
      );
    tr.append(date, model);
    if (!compact)
      tr.append(
        el("td", {
          text:
            [
              ...new Set(
                attempts.map((a) =>
                  a.reasoning_effort
                    ? `${a.reasoning_effort}${a.reasoning_source === "default" ? "（默认）" : ""}`
                    : "未知",
                ),
              ),
            ].join(" / ") || "未知",
        }),
      );
    tr.append(
      el("td", {}, [
        badge(
          ok
            ? "成功"
            : r.outcome === "incomplete"
              ? "未完整结束"
              : `失败 ${r.status}`,
          ok ? "" : "warn",
        ),
      ]),
    );
    const ratio = r.cache_hit_ratio;
    const tokens = el("td", {
      text:
        ratio === null || ratio === undefined
          ? "未知"
          : (ratio * 100).toFixed(1) + "%",
    });
    tokens.append(
      el("div", {
        class: "subtle",
        text: `命中 ${fmt(r.cache_hit_tokens)} / 输入 ${fmt(r.prompt_tokens)}`,
      }),
    );
    if (!compact)
      tokens.append(
        el("div", {
          class: "subtle",
          text: `输出 ${fmt(r.completion_tokens)} / 未命中 ${fmt(r.cache_miss_tokens)}`,
        }),
      );
    tr.append(tokens);
    const credit = el("td", {
      class: "number " + (r.credit === null ? "unknown" : "credit"),
      text: r.credit === null ? "未知" : fmt(r.credit),
    });
    if (r.unknown_attempts)
      credit.append(
        el("div", { class: "subtle", text: `${r.unknown_attempts} 次未知` }),
      );
    tr.append(credit);
    if (!compact) {
      const detail = el("details", {}, [
        el("summary", {
          text: `${r.attempt_count} 次 · ${fmt(r.duration_ms / 1000)}s`,
        }),
      ]);
      for (const a of attempts)
        detail.append(
          el("div", {
            class: "attempts",
            text: `${accountName(a.account)} / ${a.model} / ${a.reasoning_effort || "思考未知"} · HTTP ${a.status} · ${fmt(a.duration_ms / 1000)}s · ${a.complete ? "完整" : "未完整"} · ${a.credit === null ? "积分未知" : fmt(a.credit) + " 积分"} · 命中 ${fmt(a.cache_hit_tokens)} Token`,
          }),
        );
      tr.append(el("td", {}, [detail]));
    }
    tbody.append(tr);
  }
  table.append(tbody);
  wrap.append(table);
  target.replaceChildren(wrap);
}
async function loadRequests() {
  const data = await api(
    "requests?page=" +
      state.page +
      "&model=" +
      encodeURIComponent($("request-model").value),
  );
  state.total = data.total;
  renderRequests($("request-table"), data.rows);
  set("request-total", `共 ${fmt(data.total)} 次调用 · 最近 30 天`);
  set(
    "page-number",
    `${state.page} / ${Math.max(1, Math.ceil(data.total / 50))}`,
  );
  $("prev-page").disabled = state.page <= 1;
  $("next-page").disabled = state.page * 50 >= data.total;
}
$("prev-page").addEventListener("click", () => {
  state.page = Math.max(1, state.page - 1);
  loadRequests().catch((e) => toast(e.message));
});
$("next-page").addEventListener("click", () => {
  state.page++;
  loadRequests().catch((e) => toast(e.message));
});
$("request-model").addEventListener("change", () => {
  state.page = 1;
  loadRequests().catch((e) => toast(e.message));
});
function renderModels() {
  renderCatalogSource();
  const query = $("model-search").value.toLowerCase();
  let models = state.models.filter((m) =>
    (m.id + " " + m.display_name + " " + m.description)
      .toLowerCase()
      .includes(query),
  );
  const sort = $("model-sort").value;
  if (sort === "general")
    models.sort(
      (a, b) => (b.benchmark?.general ?? -1) - (a.benchmark?.general ?? -1),
    );
  if (sort === "value")
    models.sort((a, b) => (b.value_score ?? -1) - (a.value_score ?? -1));
  if (sort === "name") models.sort((a, b) => a.id.localeCompare(b.id));
  set("model-total", `${models.length} / ${state.models.length} 个模型`);
  const wrap = el("div", { class: "table-wrap" }),
    table = el("table"),
    head = el("tr");
  for (const text of [
    "模型 / ID",
    "思考强度",
    "积分 / 实测单价",
    "编程 / 代理评分",
    "上下文 / 输入",
    "",
  ])
    head.append(el("th", { text }));
  table.append(el("thead", {}, [head]));
  const body = el("tbody");
  for (const m of models) {
    const row = el("tr");
    const info = el("td", {}, [
      el("div", { text: m.display_name || m.id }),
      el("div", { class: "model-id", text: m.id }),
      el("p", { class: "model-description", text: m.description || "" }),
    ]);
    const effort = el("td", {
      text:
        (m.supported_reasoning_levels || []).map((x) => x.effort).join(" / ") ||
        "目录未注明",
    });
    if (m.default_reasoning_level)
      effort.append(
        el("div", {
          class: "subtle",
          text: "默认 " + m.default_reasoning_level,
        }),
      );
    const cost = el("td", { text: m.multiplier || "倍率未知" });
    cost.append(
      el("div", {
        class: "subtle",
        text: m.cost_observation
          ? `${fmt(m.cost_observation.credits_per_million_tokens)} 积分 / 百万 Token · ${m.cost_observation.samples} 次`
          : "实测样本不足3次",
      }),
    );
    const b = m.benchmark;
    const scores = el("td", {
      text: b ? `${fmt(b.coding)} / ${fmt(b.agentic)}` : "暂无精确匹配",
    });
    if (b) {
      scores.append(
        el("a", {
          class: "subtle",
          href: b.source_url,
          target: "_blank",
          rel: "noopener noreferrer",
          text: `${b.source} · ${b.date}`,
        }),
        el("div", { class: "subtle", text: b.reasoning || "思考档位未注明" }),
      );
    }
    row.append(
      info,
      effort,
      cost,
      scores,
      el("td", {
        class: "number",
        text: `${fmt(m.context_window)} · ${(m.input_modalities || ["text"]).map((x) => (x === "image" ? "图像" : "文本")).join("/")}`,
      }),
      el("td", {}, [
        el("button", {
          class: "button small",
          text: "复制 ID",
          onclick: () => copy(m.id, "模型ID已复制"),
        }),
      ]),
    );
    body.append(row);
  }
  table.append(body);
  wrap.append(table);
  $("model-table").replaceChildren(wrap);
  const select = $("request-model"),
    old = select.value;
  select.replaceChildren(el("option", { value: "", text: "全部模型" }));
  for (const m of state.models)
    select.append(el("option", { value: m.id, text: m.id }));
  select.value = old;
}
$("model-search").addEventListener("input", renderModels);
function renderAccounts() {
  const s = state.snapshot,
    accounts = s.accounts || [],
    target = $("unified-accounts");
  target.replaceChildren();
  if (!accounts.length) empty(target, "还没有账户，点击上方按钮关联。");
  for (const a of accounts) {
    const checked = a.checkin?.checked === true;
    const meta = el("div", {}, [
      el("div", { class: "account-name" }, [
        el("span", { text: a.nickname || "WorkBuddy 账户" }),
        badge(
          a.cooling ? "模型冷却中" : checked ? "今日已签" : "已关联",
          a.cooling ? "warn" : "",
        ),
      ]),
      el("p", {
        class: "account-meta",
        text: `${a.backend === "global" ? "国际" : "国内"} · 余额 ${fmt(a.credits)} 积分 · 在途 ${a.in_flight || 0} · 到期 ${time(a.credits_earliest_expiry)}`,
      }),
    ]);
    const actions = el("div", { class: "account-actions" });
    for (const [flag, label] of [
      ["api_enabled", "API"],
      ["checkin_enabled", "签到"],
    ]) {
      const input = el("input", { type: "checkbox" });
      input.checked = !!a[flag];
      input.addEventListener("change", async () => {
        input.disabled = true;
        try {
          await api("action", {
            action: "flags",
            id: a.id,
            [flag]: input.checked,
          });
          toast("账户开关已更新");
          await refresh();
        } catch (e) {
          input.checked = !input.checked;
          toast(e.message);
        } finally {
          input.disabled = false;
        }
      });
      actions.append(el("label", { class: "account-toggle" }, [input, label]));
    }
    actions.append(
      actionButton("刷新余额", { action: "refresh_balance", id: a.id }),
    );
    if (a.checkin_enabled && a.backend !== "global")
      actions.append(
        actionButton(checked ? "核对签到" : "立即签到", {
          action: "checkin",
          id: a.id,
        }),
      );
    if (!a.active)
      actions.append(
        actionButton("设为当前", { action: "activate", id: a.id }),
      );
    if (!a.active)
      actions.append(
        actionButton(
          "移除",
          { action: "delete", id: a.id },
          { confirm: "移除此账户及关联开关？" },
        ),
      );
    target.append(el("div", { class: "account-row" }, [meta, actions]));
  }
  const form = $("routing-form");
  form.elements.routing_strategy.value =
    s.routing?.routing_strategy || "round_robin";
  form.elements.auto_switch_on_429.checked =
    s.routing?.auto_switch_on_429 !== false;
  const select = $("fixed-account");
  select.replaceChildren(el("option", { value: "", text: "选择账户" }));
  for (const a of accounts)
    select.append(
      el("option", { value: a.id, text: a.nickname || a.id.slice(-10) }),
    );
  select.value = s.routing?.fixed_account_id || "";
}
$("routing-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const f = event.currentTarget;
  try {
    await api("action", {
      action: "settings",
      routing_strategy: f.elements.routing_strategy.value,
      fixed_account_id: f.elements.fixed_account_id.value || null,
      auto_switch_on_429: f.elements.auto_switch_on_429.checked,
    });
    toast("策略已保存");
    await refresh();
  } catch (e) {
    toast(e.message);
  }
});
function openLogin(tool) {
  state.loginTool = tool;
  state.job = null;
  clearTimeout(openLogin.timer);
  set("authorization-description", "完成一次官方授权后，可独立开启API或签到。");
  set("authorization-status", "");
  $("authorization-link").hidden = true;
  $("begin-authorization").disabled = false;
  $("login-region").disabled = false;
  $("login-dialog").showModal();
}
$("add-account").addEventListener("click", () => openLogin("python"));
$("close-dialog").addEventListener("click", () => $("login-dialog").close());
$("login-dialog").addEventListener("close", () => {
  clearTimeout(openLogin.timer);
  const id = state.job,
    tool = state.loginTool;
  state.job = null;
  if (id) api("action", { tool, action: "login-cancel", id }).catch(() => {});
});
function updateJob(job) {
  if (!$("login-dialog").open) return;
  if (job.url) {
    $("authorization-link").href = job.url;
    $("authorization-link").hidden = false;
  }
  if (job.status === "succeeded") {
    set("authorization-status", "关联成功，账户已保存。");
    state.job = null;
    refresh().catch((e) => toast(e.message));
    return;
  }
  if (["failed", "cancelled"].includes(job.status)) {
    set("authorization-status", "授权未完成，请重新生成链接。");
    $("begin-authorization").disabled = false;
    state.job = null;
    return;
  }
  set(
    "authorization-status",
    job.url ? "请打开官方页面完成授权，正在等待确认…" : "正在准备官方授权链接…",
  );
  openLogin.timer = setTimeout(pollJob, 3000);
}
async function pollJob() {
  if (!state.job || !$("login-dialog").open) return;
  const id = state.job,
    tool = state.loginTool;
  try {
    const job = await api("action", { tool, action: "login-poll", id });
    if (state.job === id && state.loginTool === tool) updateJob(job);
  } catch (e) {
    if (state.job === id) {
      set("authorization-status", e.message);
      $("begin-authorization").disabled = false;
    }
  }
}
$("begin-authorization").addEventListener("click", async () => {
  $("begin-authorization").disabled = true;
  $("login-region").disabled = true;
  set("authorization-status", "正在发起授权…");
  try {
    const job = await api("action", {
      tool: state.loginTool,
      action: "login-start",
      region: $("login-region").value,
    });
    state.job = job.id;
    updateJob(job);
  } catch (e) {
    set("authorization-status", e.message);
    $("begin-authorization").disabled = false;
    $("login-region").disabled = false;
  }
});
$("password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const f = event.currentTarget;
  if (f.elements.password.value !== f.elements.confirm.value) {
    toast("两次新密码不一致");
    return;
  }
  const b = f.querySelector("button");
  b.disabled = true;
  try {
    await api("password", {
      current: f.elements.current.value,
      password: f.elements.password.value,
    });
    f.reset();
    loginView();
    toast("密码已更新，请重新登录");
  } catch (e) {
    toast(e.message);
  } finally {
    b.disabled = false;
  }
});
$("model-sort").addEventListener("change", renderModels);
setInterval(() => {
  if (state.csrf && !document.hidden && !$("login-dialog").open)
    refresh().catch(() => {});
}, 30000);
establish();

function renderCatalogSource() {
  set(
    "benchmark-date",
    state.benchmark_updated_at
      ? "评分更新 " + time(state.benchmark_updated_at)
      : "尚未导入评分",
  );
  const c = state.catalog || {},
    source = c.source === "official_v3" ? "官方动态目录" : "安装包备用目录",
    selection =
      c.selection === "official_cli" ? "当前账户 CLI 可选模型" : "对话模型目录";
  set(
    "catalog-source",
    `${source} · ${selection} · 最近成功同步 ${time(c.fetched_at)}`,
  );
  $("catalog-warning").hidden = !c.stale;
  if (c.stale)
    set("catalog-warning", c.warning || "当前展示备用目录，请稍后重新同步。");
}
$("sync-models").addEventListener("click", async () => {
  const button = $("sync-models");
  button.disabled = true;
  try {
    const result = await api("catalog-refresh", {});
    state.models = result.models;
    state.catalog = result.catalog;
    renderModels();
    if (state.snapshot) renderOverview();
    toast(
      result.catalog.stale ? "同步未成功，已保留缓存目录" : "官方目录已同步",
    );
  } catch (e) {
    toast(e.message);
  } finally {
    button.disabled = false;
  }
});

async function copy(value, message) {
  try {
    await navigator.clipboard.writeText(value);
    toast(message || "已复制");
  } catch (e) {
    toast("剪贴板不可用，请手动复制");
  }
}
async function loadKeys() {
  const result = await api("keys"),
    target = $("key-list");
  target.replaceChildren();
  for (const k of result.keys) {
    const label = el("div", {}, [
      el("div", { class: "account-name" }, [
        el("span", { text: k.name }),
        badge(
          k.active ? "有效" : k.expired ? "已到期" : "已停用",
          k.active ? "" : "warn",
        ),
      ]),
      el("p", {
        class: "account-meta",
        text: `${k.prefix}… · 到期 ${k.expires_at ? time(k.expires_at) : "不限"} · 最近使用 ${time(k.last_used_at)}`,
      }),
    ]);
    const buttons = el("div", { class: "account-actions" });
    if (k.active)
      buttons.append(
        el("button", {
          class: "button",
          text: "撤销",
          onclick: async () => {
            if (!confirm("撤销此密钥？使用它的客户端将无法继续请求。")) return;
            try {
              await api("key-revoke", { id: k.id });
              await loadKeys();
              toast("密钥已撤销");
            } catch (e) {
              toast(e.message);
            }
          },
        }),
      );
    target.append(el("div", { class: "account-row" }, [label, buttons]));
  }
}
$("key-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const f = event.currentTarget;
  try {
    const r = await api("keys", {
      name: f.elements.name.value,
      expires_at: f.elements.expires.value
        ? new Date(f.elements.expires.value).toISOString()
        : null,
    });
    $("new-key-value").value = r.key;
    $("key-dialog").showModal();
    f.reset();
    await loadKeys();
  } catch (e) {
    toast(e.message);
  }
});
$("copy-new-key").addEventListener("click", () =>
  copy($("new-key-value").value, "新密钥已复制"),
);
$("close-key-dialog").addEventListener("click", () => $("key-dialog").close());
$("key-dialog").addEventListener(
  "close",
  () => ($("new-key-value").value = ""),
);
$("copy-legacy").addEventListener("click", async () => {
  try {
    const r = await api("legacy-key", {});
    await copy(r.key, "原有密钥已复制");
  } catch (e) {
    toast(e.message);
  }
});
async function loadMail() {
  const m = await api("mail"),
    f = $("mail-form");
  for (const k of ["host", "port", "security", "username", "from_email"])
    f.elements[k].value = m[k] ?? "";
  f.elements.enabled.checked = !!m.enabled;
  f.elements.recipients.value = (m.to_emails || []).join(", ");
  f.elements.password.value = "";
  f.elements.password.placeholder = m.password_set
    ? "已保存，留空保留"
    : "填写SMTP授权码";
  set(
    "mail-status",
    m.delivery?.status === "failed"
      ? "最近发送失败，请检查邮件设置"
      : m.delivery?.status === "sent"
        ? "最近一封邮件已提交SMTP服务器"
        : "授权码仅保存在服务器；界面不会回显。",
  );
}
$("mail-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const f = event.currentTarget;
  const data = {
    enabled: f.elements.enabled.checked,
    port: Number(f.elements.port.value),
    to_emails: f.elements.recipients.value
      .split(/[,;，]/)
      .map((x) => x.trim())
      .filter(Boolean),
  };
  for (const k of ["host", "security", "username", "password", "from_email"])
    data[k] = f.elements[k].value;
  try {
    await api("mail", data);
    toast("邮件设置已保存");
    await loadMail();
  } catch (e) {
    toast(e.message);
  }
});
$("mail-test").addEventListener("click", async () => {
  const b = $("mail-test");
  b.disabled = true;
  try {
    const result = await api("mail-test", {});
    toast(result.ok ? "测试邮件已提交SMTP" : "发送失败，请检查设置");
  } catch (e) {
    toast(e.message);
  } finally {
    b.disabled = false;
  }
});
$("update-benchmarks").addEventListener("click", async () => {
  const b = $("update-benchmarks");
  b.disabled = true;
  try {
    await api("benchmark-update", {});
    toast("官方评分快照已手动更新");
    await refresh();
  } catch (e) {
    toast(e.message);
  } finally {
    b.disabled = false;
  }
});
$("benchmark-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  if (file.size > 500000) {
    toast("评分文件过大");
    return;
  }
  try {
    const data = JSON.parse(await file.text());
    await api("benchmarks", data);
    toast("评分已导入");
    await refresh();
  } catch (e) {
    toast(e.message);
  } finally {
    event.target.value = "";
  }
});
