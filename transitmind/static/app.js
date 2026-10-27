"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  raw: null, record: null, view: "query", step: 0, historyId: null, busy: false, conversationId: null,
  followLatest: true, progressOpen: false, openProcessSteps: new Set(), processRounds: new Map(),
};
const viewport = $("workspace-body");
const historyState = { records: [], expanded: new Set() };
const statisticsState = { data: null, loading: false };
const schemaState = {
  snapshot: null, selectedTable: null, loading: false, editing: false, saving: false,
  generating: false, suggestion: null,
};
const toolNames = {
  schema_retrieve: "检索 Schema", schema_expand: "扩展关联表",
  describe_table: "读取表结构", run_sql: "执行 SQL",
};
const toolIcons = { schema_retrieve: "search", schema_expand: "network", describe_table: "table-2", run_sql: "database" };
const scoreNames = { rrf: "RRF 分数", cosine_similarity: "余弦相似度", graph_proximity: "图距离分数", keyword_hits: "关键词命中" };
const modeNames = { keyword: "关键词", vector: "向量", graph: "图检索", hybrid: "混合 · RRF", expand: "定向扩展", off: "实时表结构" };
const stopNotices = {
  model_turn_limit: "已达到轮次上限，本次没有生成最终回答。",
  tool_call_limit: "已达到工具调用上限，后续工具未执行，本次没有生成最终回答。",
  sql_attempt_limit: "已达到 SQL 尝试上限，后续 SQL 未执行，本次没有生成最终回答。",
  sql_recovery_stopped: "SQL 遇到当前无法自动修正的错误，本次运行已停止。请查看失败查询的诊断和处理说明。",
  repeated_failed_sql: "模型重复提交了本次运行中已经失败的 SQL 工具参数，重复请求未执行，本次运行已停止。请查看原失败诊断。",
};
let toastTimer;
let schemaStatusRequest = 0;

function el(tag, className = "", text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text ?? "");
  return node;
}

function icon(name, className = "") {
  const node = el("i", className);
  node.dataset.lucide = name;
  node.setAttribute("aria-hidden", "true");
  return node;
}

function refreshIcons() {
  window.lucide?.createIcons();
}

function iconButton(name, title, action) {
  const button = el("button", "icon-button");
  button.type = "button";
  button.title = title;
  button.setAttribute("aria-label", title);
  button.append(icon(name));
  button.addEventListener("click", action);
  return button;
}

function parseJson(value) {
  if (typeof value !== "string") return value;
  try { return JSON.parse(value); } catch { return null; }
}

function pretty(value) {
  if (typeof value !== "string") return JSON.stringify(value, null, 2) ?? "";
  const parsed = parseJson(value);
  return parsed === null ? value : JSON.stringify(parsed, null, 2);
}

function formatDuration(value) {
  if (value == null) return "未记录";
  return value < 1000 ? `${value.toLocaleString("zh-CN", { maximumFractionDigits: 3 })} ms` : `${(value / 1000).toFixed(2)} s`;
}

function formatTokens(value) {
  return value == null ? "未记录" : value.toLocaleString("zh-CN");
}

function statusInfo(value) {
  if (value === "running") return { label: "运行中", style: "neutral", icon: "loader-circle" };
  if (value === "completed") return { label: "已完成", style: "success", icon: "circle-check" };
  if (value === "error") return { label: "失败", style: "error", icon: "circle-alert" };
  if (value === "model_turn_limit") return { label: "轮次耗尽", style: "warning", icon: "circle-alert" };
  if (value === "tool_call_limit") return { label: "工具额度耗尽", style: "warning", icon: "circle-alert" };
  if (value === "sql_attempt_limit") return { label: "SQL 额度耗尽", style: "warning", icon: "circle-alert" };
  if (value === "sql_recovery_stopped") return { label: "SQL 恢复停止", style: "error", icon: "circle-alert" };
  if (value === "repeated_failed_sql") return { label: "重复失败请求", style: "warning", icon: "circle-alert" };
  return { label: "已载入", style: "neutral", icon: "file-json" };
}

function localTime(value, withDate = false) {
  if (!value || Number.isNaN(new Date(value).getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", ...(withDate ? { month: "2-digit", day: "2-digit" } : {}),
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(new Date(value));
}

function showNotice(message, warning = false) {
  $("notice").hidden = !message;
  $("notice").className = warning ? "notice warning" : "notice";
  $("notice").textContent = message || "";
}

function toast(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 2200);
}

async function copy(value) {
  try { await navigator.clipboard.writeText(value); toast("已复制"); }
  catch { toast("复制失败"); }
}

async function apiResponse(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const payload = await response.json();
    const detail = payload.detail;
    const message = Array.isArray(detail) ? detail.map((item) => item.msg).join("；") : detail;
    throw new Error(message || `请求失败（${response.status}）`);
  }
  return response;
}

async function api(path, options) {
  // JSON 与 SSE 共用 HTTP 错误处理，成功响应再按各自格式读取。
  return (await apiResponse(path, options)).json();
}

// 用 call_id 把工具请求、结果事件和 tool 消息对应起来，不让模型重新解释日志。
function normalize(raw) {
  const independent = Array.isArray(raw.matches) && raw.schema_version && !Array.isArray(raw.events);
  if (!independent && (!Array.isArray(raw.events) || typeof raw.question !== "string")) {
    throw new Error("这份文件不是 Agent trace 或 Schema 检索结果。");
  }
  const calls = [];
  const steps = [];
  const events = raw.events || [];
  const messages = raw.messages || [];
  // 历史仍参与模型输入展示，但工具结果只从本次提问之后的消息中配对。
  // 不同提问可能使用相同的 call_id，不能让历史工具结果混入本次运行。
  const firstRequest = events.find((event) => event.kind === "model_request");
  const currentMessages = firstRequest ? messages.slice(firstRequest.details.message_count - 1) : messages;
  const toolMessages = new Map();
  for (const message of currentMessages) {
    if (message.role !== "tool") continue;
    const envelope = parseJson(message.content) || {};
    toolMessages.set(message.tool_call_id, {
      envelope, body: parseJson(envelope.content), text: envelope.content || message.content || "",
    });
  }
  const results = new Map(events.filter((event) => event.kind === "tool_result").map((event) => [event.details.call_id, event.details]));
  if (independent) {
    const call = {
      kind: "tool", name: raw.retrieval_mode === "expand" ? "schema_expand" : "schema_retrieve",
      id: null, independent: true, turn: 0, arguments: raw.seed_tables ? { seed_tables: raw.seed_tables } : {},
      success: true, metadata: raw, body: raw, text: "", errorCode: null,
    };
    calls.push(call);
    steps.push(call);
  } else {
    for (const event of events) {
      if (event.kind === "model_request") {
        const response = events.find((item) => ["model_response", "model_error"].includes(item.kind) && item.model_turn === event.model_turn);
        steps.push({
          kind: "model", turn: event.model_turn, messageCount: event.details.message_count,
          toolCount: response?.details.tool_call_count, input: messages.slice(0, event.details.message_count),
          durationMs: response?.details.duration_ms, usage: response?.details.usage,
          pending: !response && raw.status === "running",
          error: response?.kind === "model_error" ? response.details.error : null,
          output: response?.kind === "model_response" ? messages[event.details.message_count] : null,
        });
      }
      if (event.kind === "tool_request") {
        const request = event.details;
        const result = results.get(request.call_id);
        const message = toolMessages.get(request.call_id);
        const call = {
          kind: "tool", name: request.name, id: request.call_id, turn: event.model_turn,
          arguments: request.arguments || {}, success: result?.success ?? message?.envelope.success,
          errorCode: result?.error_code ?? message?.envelope.error_code,
          durationMs: result?.duration_ms,
          pending: !result && !message && raw.status === "running",
          recovery: result?.recovery ?? message?.envelope.recovery,
          metadata: result?.metadata || {}, body: message?.body, text: message?.text || "",
        };
        calls.push(call);
        steps.push(call);
      }
      if (event.kind === "run_stopped") steps.push({ kind: "stop", turn: event.model_turn, ...event.details });
    }
  }
  if (raw.error && !events.some((event) => event.kind === "model_error")) steps.push({ kind: "error", turn: 0, error: raw.error });
  return {
    raw, independent, calls, steps, sqlCalls: calls.filter((call) => call.name === "run_sql"),
    question: raw.question || `Schema · ${(raw.seed_tables || []).join(", ") || modeNames[raw.retrieval_mode] || "检索结果"}`,
    status: raw.status || raw.stop_reason || "loaded",
    rounds: Math.max(0, ...events.map((event) => event.model_turn || 0)),
  };
}

function setView(view) {
  const previousView = state.view;
  state.view = view;
  const schemaView = view === "schema";
  const statisticsView = view === "statistics";
  const catalogView = schemaView || statisticsView;
  for (const button of document.querySelectorAll(".view-tab")) {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  }
  $("query-panel").hidden = !state.record || view !== "query";
  $("debug-panel").hidden = !state.record || view !== "debug";
  $("schema-panel").hidden = !schemaView;
  $("statistics-panel").hidden = !statisticsView;
  $("query-composer").hidden = catalogView;
  $("query-heading").hidden = catalogView;
  $("scroll-latest").hidden = catalogView || !state.record || state.followLatest;
  $("run-overview").hidden = catalogView || (!state.record && !state.busy);
  $("usage-summary").hidden = catalogView || !state.record || state.record.independent || state.record.status === "running";
  $("conversation-context").hidden = catalogView || !state.record?.raw.conversation_id || state.record.status === "running";
  $("empty-state").hidden = catalogView || Boolean(state.record);
  $("notice").hidden = catalogView || !$("notice").textContent;
  $("open-record").hidden = catalogView;
  $("download-record").hidden = catalogView;
  if (schemaView && !schemaState.snapshot) loadSchemaCatalog();
  if (statisticsView && previousView !== view) loadStatistics();
}


async function loadStatistics() {
  if (statisticsState.loading) return;
  statisticsState.loading = true;
  $("refresh-statistics").disabled = true;
  $("statistics-notice").hidden = true;
  if (!statisticsState.data) $("statistics-source").textContent = "正在读取本地运行记录";
  try {
    statisticsState.data = await api("/api/statistics");
    renderStatistics();
  } catch (error) {
    $("statistics-notice").textContent = error.message;
    $("statistics-notice").hidden = false;
    if (!statisticsState.data) $("statistics-source").textContent = "统计未载入";
  } finally {
    statisticsState.loading = false;
    $("refresh-statistics").disabled = false;
  }
}

function renderStatistics() {
  const data = statisticsState.data;
  if (!data) return;
  const group = data.groups[$("statistics-mode").value];
  const panel = $("statistics-content"); panel.replaceChildren();
  $("statistics-source").textContent = `${data.source} · ${localTime(data.generated_at, true)} · 排除 ${data.schema_records_excluded} 份独立 Schema 记录 · 去重 ${data.duplicate_runs_excluded} 份同 ID 记录`;
  if (!group.run_count) {
    panel.append(el("div", "muted-empty", "此模式下暂无运行记录"));
    return;
  }
  const overview = el("dl", "usage-values statistics-values");
  for (const [label, value] of [
    ["运行次数", formatTokens(group.run_count)], ["正常完成次数", formatTokens(group.completed_runs)],
    ["正常完成率（非答案正确率）", `${(group.completion_rate * 100).toFixed(1)}%`],
  ]) {
    const item = el("div"); item.append(el("dt", "", label), el("dd", "", value)); overview.append(item);
  }
  panel.append(overview);
  const performance = el("section", "data-section");
  performance.append(heading("耗时与调用", "timer"));
  const rows = [
    ["model_calls", "模型请求次数", false], ["tool_calls", "工具尝试次数", false],
    ["sql_attempts", "SQL 尝试次数", false], ["duration_ms", "总耗时", true],
    ["model_duration_ms", "模型请求耗时", true], ["tool_duration_ms", "工具执行耗时", true],
  ].map(([key, label, duration]) => {
    const metric = group.metrics[key];
    const format = duration ? formatDuration : formatTokens;
    return [label, format(metric.total), format(metric.mean), `${metric.recorded_count} / ${group.run_count}`];
  });
  performance.append(renderTable(["指标", "已记录合计", "每次运行均值", "已记录运行 / 总运行"], rows));
  panel.append(performance);

  const tokens = el("section", "data-section");
  tokens.append(heading("Token 用量", "hash"));
  const values = el("dl", "usage-values statistics-values");
  for (const [key, label] of [["prompt_tokens", "已记录输入"], ["completion_tokens", "已记录输出"], ["total_tokens", "已记录合计"]]) {
    const item = el("div"); item.append(el("dt", "", label), el("dd", "", formatTokens(group.token_usage[key]))); values.append(item);
  }
  const coverage = group.usage_coverage;
  tokens.append(values, el("div", "subtle", `用量覆盖 ${coverage.recorded_runs} / ${group.run_count} 次运行 · 完整用量 ${coverage.complete_runs} 次 · 已记录用量 ${coverage.recorded_model_calls} 次模型请求`));
  panel.append(tokens);

  const tools = el("section", "data-section"); tools.append(heading("工具调用", "wrench"));
  if (!group.tools.length) tools.append(el("div", "subtle", "暂无工具执行事件"));
  else tools.append(renderTable(
    ["工具", "尝试", "成功", "失败", "结果未记录", "平均耗时", "计时样本"],
    group.tools.map((tool) => [tool.name, tool.calls, tool.successes, tool.failures, tool.unknown_results,
      formatDuration(tool.duration_ms.mean), `${tool.duration_ms.recorded_count} / ${tool.calls}`]),
  ));
  panel.append(tools);
  const stops = el("section", "data-section"); stops.append(heading("停止原因", "circle-stop"));
  stops.append(renderTable(["停止原因", "次数"], group.stop_reasons.map((item) => [
    `${statusInfo(item.reason).label === "已载入" ? "未分类" : statusInfo(item.reason).label} · ${item.reason}`, item.count,
  ])));
  panel.append(stops);
  const modes = el("section", "data-section"); modes.append(heading("运行模式", "layers"));
  modes.append(renderTable(["模式", "次数"], group.modes.map((item) => [item.mode, item.count])));
  panel.append(modes);
  refreshIcons();
}


// 网页拿到 Schema 后，怎么把它变成左侧目录和右侧字段。
async function loadSchemaCatalog() {
  if (schemaState.loading || schemaState.editing || schemaState.generating) return;
  schemaState.loading = true;
  $("refresh-schema").disabled = true;
  $("schema-notice").hidden = false;
  $("schema-notice").className = "notice warning";
  $("schema-notice").textContent = "正在读取 Schema 快照…";
  try {
    // 这里只取保存好的元数据。表目录的搜索也在浏览器完成，不请求 DeepSeek、BGE 或 Neo4j。
    const snapshot = await api("/api/schema");
    schemaState.snapshot = snapshot;
    renderSchemaCatalogMeta();
    $("schema-notice").hidden = true;
    $("schema-catalog-layout").hidden = false;
    renderSchemaCatalogList();
    refreshSchemaIndexStatus();
  } catch (error) {
    schemaState.snapshot = null;
    $("schema-catalog-layout").hidden = true;
    $("schema-catalog-meta").textContent = "Schema 快照不可用";
    $("schema-notice").className = "notice";
    $("schema-notice").textContent = error.message;
  } finally {
    schemaState.loading = false;
    $("refresh-schema").disabled = false;
  }
}

function renderSchemaCatalogMeta() {
  const snapshot = schemaState.snapshot;
  $("schema-catalog-meta").replaceChildren(
    el("span", "", "本地快照"), el("code", "", `${snapshot.database_id}.${snapshot.schema}`),
    el("span", "", `版本 ${snapshot.version}`), el("span", "", `结构同步 ${localTime(snapshot.synced_at, true)}`),
  );
  if (snapshot.annotations_updated_at) {
    $("schema-catalog-meta").append(el("span", "", `说明更新 ${localTime(snapshot.annotations_updated_at, true)}`));
  }
  $("snapshot-label").textContent = `Schema · ${snapshot.tables.length} 张表`;
  $("snapshot-label").title = snapshot.version;
}

async function refreshSchemaIndexStatus() {
  const requestId = ++schemaStatusRequest;
  $("refresh-schema-status").disabled = true;
  $("schema-status-error").hidden = true;
  $("schema-status-version").textContent = "正在读取本地索引记录…";
  try {
    const data = await api("/api/schema/status");
    // 保存说明后可能又发起一次刷新，旧响应不能覆盖新版本状态。
    if (requestId !== schemaStatusRequest) return;
    $("schema-status-version").textContent = `目标版本 ${data.snapshot_version}`;
    const labels = {
      matched: ["版本一致", "success"], recorded: ["有同版本同步记录", "neutral"],
      outdated: ["记录版本不一致", "warning"], missing: ["未找到索引", "warning"],
      unrecorded: ["无本地同步记录", "neutral"], unreadable: ["记录不可读取", "error"],
    };
    const rows = $("schema-status-rows"); rows.replaceChildren();
    for (const [name, index] of Object.entries(data.indexes)) {
      const [label, style] = labels[index.status];
      const row = el("tr");
      const status = el("td"); status.append(el("span", `status-badge ${style}`, label));
      if (name === "graph") status.append(el("span", "schema-status-source", "本地记录 · 未探测 Neo4j"));
      const version = el("td"); version.append(el("code", "", index.schema_version || "—"));
      const recorded = el("td", "", localTime(index.recorded_at, true) || "—");
      recorded.title = index.recorded_at || "";
      const action = el("td"); action.append(iconButton("copy", name === "vector" ? "复制向量重建命令" : "复制图同步命令", () => copy(index.command)));
      row.append(el("td", "", name === "vector" ? "向量" : "Neo4j 图"), status, version, recorded, action);
      rows.append(row);
    }
    if (schemaState.snapshot && schemaState.snapshot.version !== data.snapshot_version) {
      $("schema-status-error").hidden = false;
      $("schema-status-error").textContent = "页面快照与当前版本不同。";
    }
    refreshIcons();
  } catch (error) {
    if (requestId !== schemaStatusRequest) return;
    $("schema-status-version").textContent = "索引状态不可用";
    $("schema-status-error").hidden = false; $("schema-status-error").textContent = error.message;
    const row = el("tr"); const cell = el("td", "", "无法读取索引记录"); cell.colSpan = 5; row.append(cell);
    $("schema-status-rows").replaceChildren(row);
  } finally {
    if (requestId === schemaStatusRequest) $("refresh-schema-status").disabled = false;
  }
}

function setSchemaEditing(editing) {
  schemaState.editing = editing;
  if (!editing) schemaState.suggestion = null;
  // 编辑时不重新筛选或加载目录；切到查询页再回来，草稿仍在浏览器内。
  const locked = editing || schemaState.generating;
  $("schema-search").disabled = locked;
  $("refresh-schema").disabled = locked || schemaState.loading;
  for (const button of $("schema-table-list").querySelectorAll("button")) button.disabled = locked;
}

// 请求并接收草稿
async function generateSchemaDraft(table) {
  if (schemaState.generating || schemaState.editing || schemaState.saving) return;
  schemaState.generating = true;
  setSchemaEditing(false); renderSchemaCatalogTable(table);
  $("schema-notice").hidden = false;
  $("schema-notice").className = "notice warning";
  $("schema-notice").textContent = "正在生成 AI 草稿，未保存任何修改。";
  try {
    const result = await api(`/api/schema/tables/${encodeURIComponent(table.table)}/enrich`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_version: schemaState.snapshot.version }),
    });
    if (result.schema_version !== schemaState.snapshot.version || result.table !== table.table) {
      throw new Error("草稿与页面的 Schema 版本或表不一致，未应用。请刷新后重试。");
    }
    // 草稿只填进编辑器；snapshot 仍是原内容，直到用户明确提交保存。
    schemaState.suggestion = result;
    schemaState.editing = true;
    $("schema-notice").hidden = true;
    toast("AI 草稿已生成，尚未保存");
  } catch (error) {
    $("schema-notice").hidden = false;
    $("schema-notice").className = "notice";
    $("schema-notice").textContent = error.message;
  } finally {
    schemaState.generating = false;
    setSchemaEditing(schemaState.editing); renderSchemaCatalogTable(table);
  }
}

function renderSchemaCatalogList() {
  if (!schemaState.snapshot) return;
  const query = $("schema-search").value.trim().toLocaleLowerCase();
  const tables = schemaState.snapshot.tables.filter((table) => [
    table.table, table.description, ...(table.keywords || []),
    ...(table.column_details || []).flatMap((column) => [column.name, column.description]),
  ].some((value) => String(value || "").toLocaleLowerCase().includes(query)));
  if (!tables.some((table) => table.table === schemaState.selectedTable)) {
    schemaState.selectedTable = tables[0]?.table || null;
  }
  $("schema-table-count").textContent = `${tables.length} / ${schemaState.snapshot.tables.length} 张表`;
  const list = $("schema-table-list"); list.replaceChildren();
  for (const table of tables) {
    const selected = table.table === schemaState.selectedTable;
    const button = el("button", `schema-table-button${selected ? " active" : ""}`);
    button.type = "button"; button.dataset.table = table.table;
    button.setAttribute("aria-pressed", String(selected));
    const title = el("span", "schema-table-title");
    title.append(icon("table-2"), el("code", "", table.table), el("span", "subtle", table.columns.length));
    button.append(title, el("span", "schema-table-description", table.description || "—"));
    button.addEventListener("click", () => {
      schemaState.selectedTable = table.table;
      for (const item of list.querySelectorAll("button")) {
        const active = item.dataset.table === table.table;
        item.classList.toggle("active", active); item.setAttribute("aria-pressed", String(active));
      }
      renderSchemaCatalogTable(table);
    });
    list.append(button);
  }
  if (!tables.length) list.append(el("div", "muted-empty", "没有匹配的表"));
  renderSchemaCatalogTable(tables.find((table) => table.table === schemaState.selectedTable));
  refreshIcons();
}

function renderSchemaCatalogTable(table) {
  const target = $("schema-table-detail"); target.replaceChildren();
  if (!table) { target.append(el("div", "muted-empty", "没有可显示的表结构")); return; }
  const title = heading(table.table, "table-2");
  const actions = el("div", "schema-table-actions");
  actions.append(el("span", "subtle", `${table.columns.length} 个字段`));
  title.append(actions); target.append(title);
  // renderSchemaEditor()：把草稿填进编辑框
  if (schemaState.editing) { renderSchemaEditor(table, target, actions); return; }
  const edit = iconButton("pencil", "编辑中文说明与关键词", () => {
    $("schema-notice").hidden = true;
    setSchemaEditing(true); renderSchemaCatalogTable(table);
  });
  edit.disabled = schemaState.generating;
  const generate = el("button", "schema-ai-button");
  generate.type = "button"; generate.title = "AI 起草说明（调用 DeepSeek）";
  generate.disabled = schemaState.generating;
  generate.append(icon(schemaState.generating ? "loader-circle" : "sparkles", schemaState.generating ? "spinning" : ""),
    el("span", "", schemaState.generating ? "起草中" : "AI 起草"));
  generate.addEventListener("click", () => generateSchemaDraft(table));
  actions.append(edit, generate);
  target.append(el("p", "schema-table-summary", table.description || "—"));
  target.append(keyValues([["关键词", table.keywords?.length ? table.keywords : "—"], ["主键", table.primary_key?.length ? table.primary_key : "—"]]));
  const columns = table.column_details || table.columns.map((name) => ({ name }));
  const rows = columns.map((column) => [
    column.name, column.data_type || "—", column.nullable === false ? "否" : column.nullable === true ? "是" : "—",
    [
      (table.primary_key || []).includes(column.name) ? "PK" : "",
      ...(table.foreign_keys || []).filter((key) => key.column === column.name).map((key) => `FK → ${key.referenced_table}.${key.referenced_column}`),
    ].filter(Boolean).join(" / ") || "—", column.description || "—",
  ]);
  target.append(renderTable(["字段", "类型", "可空", "约束", "中文说明"], rows));

  // 同时列出这张表指向别人的外键，以及其他表指回它的外键；这里没有重新查询图数据库。
  const relationships = schemaState.snapshot.tables.flatMap((source) => (source.foreign_keys || [])
    .filter((key) => source.table === table.table || (key.referenced_table === table.table && key.referenced_schema === schemaState.snapshot.schema))
    .map((key) => ({ source: source.table, ...key })));
  const section = detailSection("外键关系");
  if (!relationships.length) section.append(el("div", "muted-empty", "快照未记录相关外键"));
  for (const key of relationships) {
    const row = el("div", "relationship"); row.title = key.constraint;
    row.append(el("code", "", `${key.source}.${key.column}`), icon("arrow-right"),
      el("code", "", `${key.referenced_schema}.${key.referenced_table}.${key.referenced_column}`));
    section.append(row);
  }
  target.append(section);
  refreshIcons();
}

function renderSchemaEditor(table, target, actions) {
  const proposal = schemaState.suggestion;
  const draft = proposal?.suggestion;
  if (draft) {
    const review = el("section", "schema-ai-review");
    const title = el("div", "schema-ai-review-heading");
    title.append(icon("sparkles"), el("span", "status-badge warning", "AI 草稿 · 未保存"));
    review.append(title, keyValues([
      ["模型", proposal.model], ["生成时间", localTime(proposal.generated_at, true)], ["建议依据", draft.basis || "—"],
    ]));
    if (draft.warnings.length) {
      const warnings = el("ul", "schema-ai-warnings");
      for (const warning of draft.warnings) warnings.append(el("li", "", warning));
      review.append(warnings);
    }
    target.append(review);
  }
  const form = el("form", "schema-edit-form");
  const description = el("textarea"); description.rows = 3; description.maxLength = 2000;
  description.value = draft?.description ?? table.description ?? "";
  const keywords = el("input"); keywords.type = "text"; keywords.value = (draft?.keywords ?? table.keywords ?? []).join(", ");
  for (const [title, input, original] of [
    ["表说明", description, table.description || ""], ["关键词", keywords, (table.keywords || []).join(", ")],
  ]) {
    const label = el("label", "schema-edit-field");
    label.append(el("span", "", title), input);
    if (draft && input.value !== original) label.append(el("span", "schema-ai-original", `原值：${original || "（空）"}`));
    form.append(label);
  }
  const scroll = el("div", "table-scroll");
  const fieldTable = el("table", "data-table schema-edit-table");
  const thead = el("thead"); const header = el("tr");
  for (const label of ["字段", "类型", "中文说明"]) header.append(el("th", "", label));
  thead.append(header);
  const tbody = el("tbody");
  const columns = table.column_details || table.columns.map((name) => ({ name }));
  for (const column of columns) {
    const row = el("tr");
    const input = el("textarea", "schema-column-input");
    input.rows = 2; input.maxLength = 1000;
    input.value = draft?.column_descriptions[column.name] ?? column.description ?? "";
    input.dataset.column = column.name; input.setAttribute("aria-label", `${column.name} 的中文说明`);
    const cell = el("td"); cell.append(input);
    if (draft && input.value !== (column.description || "")) {
      cell.append(el("span", "schema-ai-original", `原值：${column.description || "（空）"}`));
    }
    row.append(el("td", "", column.name), el("td", "", column.data_type || "—"), cell);
    tbody.append(row);
  }
  fieldTable.append(thead, tbody); scroll.append(fieldTable); form.append(scroll);
  const save = iconButton("save", draft ? "确认保存 AI 草稿" : "保存说明", () => form.requestSubmit());
  const cancel = iconButton("x", draft ? "放弃 AI 草稿" : "取消编辑", () => {
    setSchemaEditing(false); renderSchemaCatalogTable(table);
  });
  actions.append(save, cancel); target.append(form);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (schemaState.saving) return;
    const request = {
      expected_version: schemaState.snapshot.version,
      description: description.value,
      keywords: keywords.value.split(/[,，\n]/).map((word) => word.trim()).filter(Boolean),
      column_descriptions: Object.fromEntries([...form.querySelectorAll("[data-column]")].map((input) => [input.dataset.column, input.value])),
    };
    schemaState.saving = true;
    const controls = [...form.querySelectorAll("input, textarea"), save, cancel];
    for (const control of controls) control.disabled = true;
    try {
      // 保存的是说明，不提交真实列类型和主外键；后端返回新快照再更新浏览器副本。
      const result = await api(`/api/schema/tables/${encodeURIComponent(table.table)}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request),
      });
      schemaState.snapshot = result.snapshot;
      setSchemaEditing(false); renderSchemaCatalogMeta(); renderSchemaCatalogList();
      refreshSchemaIndexStatus();
      $("schema-notice").hidden = !result.changed;
      if (result.changed) {
        $("schema-notice").className = "notice warning";
        $("schema-notice").textContent = "已保存新版本；本次保存未更新向量索引或 Neo4j。";
      }
      toast(result.changed ? "说明已保存" : "没有变更，版本未更新");
    } catch (error) {
      // 保存失败时保留表单输入，便于修正或重试。
      $("schema-notice").hidden = false;
      $("schema-notice").className = "notice"; $("schema-notice").textContent = error.message;
    } finally {
      schemaState.saving = false;
      for (const control of controls) control.disabled = false;
    }
  });
  refreshIcons(); description.focus();
}

function heading(title, symbol, suffix = "") {
  const row = el("div", "content-heading");
  const titleNode = el("h3");
  if (symbol) titleNode.append(icon(symbol));
  titleNode.append(document.createTextNode(title));
  row.append(titleNode);
  if (suffix) row.append(el("span", "subtle", suffix));
  return row;
}

function renderMarkdown(text, target) {
  // 模型文本、导入文件和数据库值都按数据处理；Markdown 经清理后才进入页面。
  if (window.marked && window.DOMPurify) {
    target.innerHTML = DOMPurify.sanitize(marked.parse(String(text), { gfm: true, breaks: false }));
    for (const link of target.querySelectorAll("a")) { link.target = "_blank"; link.rel = "noopener noreferrer"; }
  } else {
    target.textContent = text;
  }
}

function renderTable(columns, rows, numbered = false) {
  const scroll = el("div", "table-scroll");
  const table = el("table", "data-table");
  const head = el("tr");
  if (numbered) head.append(el("th", "row-number", "#"));
  for (const column of columns) head.append(el("th", "", column));
  const thead = el("thead");
  thead.append(head);
  const tbody = el("tbody");
  rows.forEach((row, index) => {
    const tr = el("tr");
    if (numbered) tr.append(el("td", "row-number", index + 1));
    columns.forEach((column, columnIndex) => {
      const value = Array.isArray(row) ? row[columnIndex] : row[column];
      const td = el("td");
      if (value == null) td.append(el("span", "null-value", "NULL"));
      else td.textContent = typeof value === "object" ? JSON.stringify(value) : String(value);
      tr.append(td);
    });
    tbody.append(tr);
  });
  table.append(thead, tbody);
  scroll.append(table);
  return scroll;
}

function highlightSql(text, target) {
  target.replaceChildren();
  const tokenPattern = /'(?:''|[^'])*'|\b(?:SELECT|FROM|LEFT|INNER|OUTER|JOIN|ON|WHERE|GROUP|BY|ORDER|ASC|DESC|LIMIT|AS|AND|OR|NOT|NULL|IS|COUNT|DISTINCT|HAVING|IN|LIKE|ILIKE|BETWEEN|LOWER|UPPER)\b|\b\d+(?:\.\d+)?\b/gi;
  let previous = 0;
  for (const match of text.matchAll(tokenPattern)) {
    target.append(document.createTextNode(text.slice(previous, match.index)));
    const style = match[0].startsWith("'") ? "sql-string" : /^\d/.test(match[0]) ? "sql-number" : "sql-keyword";
    target.append(el("span", style, match[0]));
    previous = match.index + match[0].length;
  }
  target.append(document.createTextNode(text.slice(previous)));
}

function sqlSurface(call) {
  const metadata = call.metadata;
  const choices = [];
  const original = metadata.original_sql || call.arguments.sql;
  const executed = metadata.executed_sql || (call.success ? call.body?.sql : null);
  if (executed) choices.push(["实际执行 SQL", executed]);
  else if (metadata.prepared_sql) choices.push(["准备执行 SQL", metadata.prepared_sql]);
  if (original) choices.push(["模型生成 SQL", original]);
  const surface = el("div", "sql-surface");
  const toolbar = el("div", "sql-toolbar");
  const select = el("select");
  select.setAttribute("aria-label", "SQL 来源");
  for (const [label, text] of choices) { const option = el("option", "", label); option.value = text; select.append(option); }
  const code = el("pre", "sql-code");
  const showCode = () => highlightSql(select.value, code);
  select.addEventListener("change", showCode);
  toolbar.append(select, iconButton("copy", "复制 SQL", () => copy(select.value)));
  surface.append(toolbar, code);
  showCode();
  return surface;
}

function renderSql(call, target, index = 0) {
  const section = el("section", "data-section");
  const metadata = call.metadata;
  const body = call.body || {};
  const columns = metadata.columns || body.columns || [];
  const rows = metadata.rows || body.rows || [];
  const retained = metadata.row_count ?? body.row_count ?? rows.length;
  const status = call.success === true ? `显示 ${rows.length} 行 · 保留 ${retained} 行`
    : call.pending ? "执行中" : call.success === false ? "执行失败" : "结果未记录";
  section.append(heading(index ? `查询结果 ${index + 1}` : "查询结果", "table-2", status));
  section.append(sqlSurface(call));
  if (call.success === false) section.append(el("div", "inline-error", [call.errorCode, call.text].filter(Boolean).join("\n") || "SQL 执行失败"));
  if (call.recovery) {
    const action = call.recovery.action === "correct_sql" ? "允许修正 SQL" : "停止本次运行";
    section.append(keyValues([["错误处理", action], ["处理说明", call.recovery.instruction]]));
  }
  if (metadata.truncated || body.truncated) {
    const note = el("div", "inline-note"); note.append(icon("circle-alert"), document.createTextNode(`查询结果超过 ${metadata.row_limit || body.row_limit || 100} 行上限，当前为保留结果。`)); section.append(note);
  }
  if (rows.length < retained) section.append(el("div", "inline-note", `这份记录只包含 ${rows.length} 行预览，保留行数为 ${retained}。`));
  if (columns.length) section.append(renderTable(columns, rows, true));
  if (call.success && !rows.length) section.append(el("div", "muted-empty", "查询结果为空"));
  target.append(section);
}

function keyValues(entries) {
  const list = el("dl", "key-values");
  for (const [key, value] of entries) {
    if (value === undefined || value === null || value === "") continue;
    list.append(el("dt", "", key), el("dd", "", Array.isArray(value) ? value.join(", ") : value));
  }
  return list;
}

function syncConversationControls() {
  const enabled = $("conversation-mode").checked;
  $("new-run-label").textContent = enabled ? "新会话" : "新查询";
  const label = $("active-conversation");
  label.replaceChildren();
  if (enabled) label.append(document.createTextNode("当前会话 · "), el("code", "", state.conversationId));
  else label.textContent = "独立查询";
  $("run-button").replaceChildren(
    icon(state.busy ? "loader-circle" : "arrow-up", state.busy ? "spinning" : ""),
    el("span", "sr-only", state.busy ? "运行中" : "发送问题"),
  );
  $("run-button").title = state.busy ? "运行中" : "发送问题";
  $("run-button").setAttribute("aria-label", $("run-button").title);
  refreshIcons();
}

function setConversation(conversationId) {
  state.conversationId = conversationId;
  $("conversation-mode").checked = Boolean(conversationId);
  syncConversationControls();
}

function clearQuery() {
  state.raw = null; state.record = null; state.historyId = null;
  state.followLatest = true; state.progressOpen = false;
  state.openProcessSteps.clear();
  state.processRounds.clear();
  $("question").value = ""; $("run-overview").hidden = true; $("download-record").disabled = true;
  $("empty-state").querySelector("h2").textContent = "等待查询";
  $("footer-status").textContent = state.conversationId ? "会话已就绪" : "独立查询";
  renderHistory();
  showNotice(""); setView("query"); closeSidebar(); $("question").focus();
  viewport.scrollTop = 0;
}

function renderConversationContext(record) {
  const panel = $("conversation-context"); panel.replaceChildren();
  const raw = record.raw;
  if (!raw.conversation_id) return;
  const title = heading("本次会话上下文", "messages-square");
  // 只有本机服务保存的真实 SQL 运行可以续接；打开外来 JSON 仍只作回看。
  if (state.historyId && raw.mode === "deepseek_live_postgres_sql") {
    const resume = el("button", "conversation-resume");
    resume.id = "resume-conversation"; resume.type = "button"; resume.disabled = state.busy;
    resume.title = "读取此会话最新保存的历史，继续提问";
    resume.append(icon("message-square"), document.createTextNode("继续此会话"));
    resume.addEventListener("click", () => {
      if (state.busy) return;
      setConversation(raw.conversation_id);
      clearQuery();
    });
    title.append(resume);
  }
  const count = raw.history_message_count;
  // messages 的第 0 条是本次系统规则，紧随其后的 count 条才是实际携带的历史。
  const history = Number.isInteger(count) ? (raw.messages || []).slice(1, 1 + count) : [];
  const turns = [];
  for (const message of history) {
    if (message.role === "user") turns.push({ question: message.content, answers: [] });
    else if (message.role === "assistant" && !message.tool_calls?.length && message.content && turns.length) {
      turns[turns.length - 1].answers.push(message.content);
    }
  }
  panel.append(title, keyValues([
    ["会话 ID", raw.conversation_id], ["运行 ID", raw.run_id || state.historyId || "未记录"],
    ["携带历史", Number.isInteger(count) ? `${turns.length} 次提问 · ${count} 条消息` : "未记录"],
    ["历史窗口", raw.history_turn_limit == null ? "未记录" : `最近 ${raw.history_turn_limit} 次完整提问`],
    ["本轮会话保存", raw.conversation_saved === true ? "已追加" : raw.conversation_saved === false ? "未追加" : "未记录"],
  ]));
  if (!history.length) {
    panel.append(el("div", "subtle", count === 0 ? "本次未携带历史消息" : "本次历史消息未记录"));
    return;
  }
  const questions = el("details", "conversation-questions"); questions.open = true;
  questions.append(el("summary", "", `携带的历史问答 · ${turns.length} 次提问`));
  for (const [index, turn] of turns.entries()) {
    const article = el("article", "conversation-turn");
    article.append(el("p", "conversation-question", `${index + 1}. ${turn.question}`));
    for (const content of turn.answers) {
      const answer = el("div", "markdown"); renderMarkdown(content, answer); article.append(answer);
    }
    questions.append(article);
  }
  const messages = el("details");
  messages.append(el("summary", "", `历史原始消息 · ${count} 条`));
  const content = el("div", "conversation-messages"); renderMessages(history, content); messages.append(content);
  panel.append(questions, messages);
}

function detailSection(title) {
  const section = el("section", "detail-section"); section.append(el("h3", "", title)); return section;
}

function schemaData(call) {
  const body = call.body || {};
  if (Array.isArray(body.matches)) return body;
  if (call.name === "describe_table" && body.table) return { ...body, matches: [body] };
  const matches = (call.metadata.matched_tables || []).map((table) => ({ table }));
  return { ...call.metadata, matches };
}

function renderSchema(call, target) {
  if (call.name === "describe_table" && state.record.status === "running") {
    target.append(keyValues([["表", call.arguments.table]]), el("p", "subtle", "表结构已返回，字段详情将在运行结束后载入。"));
    return;
  }
  const data = schemaData(call);
  target.append(keyValues([
    ["检索方式", modeNames[data.retrieval_mode || call.metadata.retrieval_mode]],
    ["入口表", data.seed_tables || call.metadata.seed_tables],
    ["Schema 版本", data.schema_version || call.metadata.schema_version],
    ["分数类型", scoreNames[data.score_type || call.metadata.score_type]],
    ["Embedding", data.embedding_model || call.metadata.embedding_model],
  ]));
  if (!(data.matches || []).length) target.append(el("div", "muted-empty", "本次没有返回候选表"));
  for (const match of data.matches || []) {
    const details = el("details", "schema-item");
    details.open = call.name === "describe_table";
    const summary = el("summary");
    const info = el("div");
    const name = el("span", "schema-name");
    name.append(icon("chevron-right"), el("code", "", match.table));
    if (match.is_seed) name.append(el("span", "schema-key foreign", "入口"));
    info.append(name);
    if (match.description) info.append(el("span", "schema-description", match.description));
    let score = match.score == null ? "" : Number(match.score).toLocaleString("en-US", { maximumFractionDigits: 6 });
    if (match.hop_count != null) score += ` · ${match.hop_count} 跳`;
    summary.append(info, el("span", "schema-score", score));
    details.append(summary);
    const columns = match.column_details || (match.columns || []).map((column) => ({ name: column }));
    if (columns.length) {
      const rows = columns.map((column) => [
        column.name, column.data_type || "", column.nullable === false ? "否" : column.nullable === true ? "是" : "",
        [
          (match.primary_key || []).includes(column.name) ? "主键" : "",
          ...(match.foreign_keys || []).filter((key) => key.column === column.name).map((key) => `外键 → ${key.referenced_table}.${key.referenced_column}`),
        ].filter(Boolean).join(" / "), column.description || "未提供",
      ]);
      details.append(renderTable(["字段", "类型", "可空", "约束", "说明"], rows));
    }
    if (match.source_ranks) details.append(keyValues(Object.entries(match.source_ranks).map(([source, rank]) => [source === "vector" ? "向量名次" : "图名次", rank])));
    if (match.matched_keywords?.length) details.append(keyValues([["命中词", match.matched_keywords]]));
    target.append(details);
  }
  const relationships = data.relationships || call.metadata.relationships || [];
  if (relationships.length) {
    const section = detailSection("外键连接");
    for (const relationship of relationships) {
      const from = `${relationship.from_table} (${(relationship.columns || []).join(", ")})`;
      const to = `${relationship.to_table} (${(relationship.referenced_columns || []).join(", ")})`;
      const row = el("div", "relationship");
      row.title = relationship.constraint || "";
      row.append(el("code", "", from), icon("arrow-right"), el("code", "", to));
      section.append(row);
    }
    target.append(section);
  }
  const rankings = call.metadata.rankings || data.rankings;
  if (rankings) {
    const section = detailSection("融合前排名");
    const rows = Object.entries(rankings).flatMap(([source, items]) => items.map((item) => [source, item.rank, item.table, item.score]));
    section.append(renderTable(["来源", "名次", "表", "原始分数"], rows));
    target.append(section);
  }
}

// 把已有数据显示到页面上。
function renderQuery() {
  const panel = $("query-panel"); panel.replaceChildren();
  const record = state.record;
  if (record.raw.final_answer) {
    const section = el("section", "answer-section");
    const title = heading("回答", "message-square");
    title.append(iconButton("copy", "复制回答", () => copy(record.raw.final_answer)));
    const answer = el("div", "markdown"); renderMarkdown(record.raw.final_answer, answer);
    section.append(title, answer); panel.append(section);
  }
  if (!record.independent && (record.status === "running" || record.steps.length)) {
    const progress = el("details", "live-progress");
    progress.setAttribute("aria-label", "执行进度");
    progress.open = record.status === "running" || state.progressOpen;
    const summary = el("summary");
    summary.title = "展开或收起本次执行过程";
    const title = el("span", "live-progress-heading");
    title.append(icon("chevron-right"), document.createTextNode("执行过程"));
    summary.append(title, el("span", "subtle", `${record.steps.length} 步 · ${statusInfo(record.status).label}`));
    progress.append(summary);
    progress.addEventListener("toggle", () => {
      if (progress.isConnected && record.status !== "running") state.progressOpen = progress.open;
    });
    // 同一轮的模型请求与工具执行归在一起；这里只整理原 trace，不改变执行顺序。
    const rounds = new Map();
    for (const step of record.steps) {
      if (!rounds.has(step.turn)) rounds.set(step.turn, []);
      rounds.get(step.turn).push(step);
    }
    for (const [turn, steps] of rounds) {
      const round = el("details", "process-round");
      // 默认展开最新一轮；用户手动开合后，SSE 更新继续保留其选择。
      round.open = state.processRounds.get(turn) ?? turn === record.rounds;
      const roundHeader = el("summary", "process-round-heading");
      const roundText = el("span", "live-step-text");
      const toolCount = steps.filter((step) => step.kind === "tool").length;
      roundText.append(
        el("span", "process-round-title", turn ? `第 ${turn} 轮` : "运行记录"),
        el("span", "subtle", `${steps.length} 步${toolCount ? ` · ${toolCount} 次工具执行` : ""}`),
      );
      roundHeader.append(icon("chevron-right"), roundText);
      roundHeader.addEventListener("click", () => state.processRounds.set(turn, !round.open));
      const list = el("ol", "live-steps");
      for (const step of steps) {
        const info = stepInfo(step);
        const failed = step.error || step.success === false || (step.kind === "stop" && step.reason !== "completed");
        const row = el("li", `live-step${failed ? " failed" : ""}`);
        const details = el("details", "process-step");
        const key = JSON.stringify([step.kind, step.turn, step.id]);
        details.open = state.openProcessSteps.has(key);
        const header = el("summary", "live-step-heading");
        header.title = step.kind === "model" ? "查看本轮输入与模型返回" : "查看请求参数与执行结果";
        header.addEventListener("click", () => state.processRounds.set(turn, true));
        const text = el("span", "live-step-text");
        text.append(el("span", "live-step-title", info.title), el("span", "subtle", info.summary));
        header.append(icon("chevron-right"), icon(info.symbol, step.pending ? "spinning" : ""), text);
        const content = el("div", "process-step-detail");
        // 仅展开时载入详情；SSE 重绘后按稳定步骤键恢复，不额外请求模型或数据库。
        if (details.open) renderStepContent(step, content);
        details.append(header, content);
        details.addEventListener("toggle", () => {
          if (!details.isConnected) return;
          if (details.open) {
            state.openProcessSteps.add(key);
            if (!content.childElementCount) {
              renderStepContent(step, content);
              refreshIcons();
            }
          } else state.openProcessSteps.delete(key);
        });
        row.append(details);
        list.append(row);
      }
      round.append(roundHeader, list);
      progress.append(round);
    }
    if (!rounds.size) progress.append(el("p", "subtle", "正在准备查询上下文…"));
    panel.append(progress);
  }
  // 单独显示 SQL 和真实结果
  record.sqlCalls.forEach((call, index) => renderSql(call, panel, index));
  if (!record.sqlCalls.length) {
    const schemas = record.calls.filter((call) => ["schema_retrieve", "schema_expand", "describe_table"].includes(call.name));
    for (const call of schemas) {
      const section = el("section", "data-section");
      section.append(heading(toolNames[call.name], toolIcons[call.name]));
      if (call.pending) section.append(el("p", "subtle", "工具执行中…"));
      else if (call.success === false) section.append(el("div", "inline-error", call.text || call.errorCode));
      else if (call.success == null) section.append(el("p", "subtle", "工具结果尚未收到"));
      else renderSchema(call, section);
      panel.append(section);
    }
  }
  if (!panel.childElementCount) panel.append(el("div", "muted-empty", record.raw.error ? "本次运行未产生查询结果" : "本轮尚未生成最终回答"));
}

function stepInfo(step) {
  if (step.kind === "model") return {
    title: step.pending ? "正在请求模型" : step.error ? "模型请求失败" : step.toolCount === 0 ? "生成最终回答"
      : step.toolCount == null ? "模型调用" : `模型提出 ${step.toolCount} 个工具调用`,
    symbol: step.pending ? "loader-circle" : step.error ? "circle-alert" : "message-square",
    summary: `输入 ${step.messageCount} 条消息${step.durationMs == null ? "" : ` · ${formatDuration(step.durationMs)}`}`,
  };
  if (step.kind === "stop") return { title: step.reason === "completed" ? "任务完成" : statusInfo(step.reason).label, symbol: "flag", summary: step.reason };
  if (step.kind === "error") return { title: "运行失败", symbol: "circle-alert", summary: "error" };
  let summary = step.name;
  if (step.name === "schema_expand") summary = (step.arguments.seed_tables || []).join(", ");
  if (step.name === "describe_table") summary = step.errorCode || "";
  if (step.name === "schema_retrieve") summary = step.arguments.query;
  if (step.name === "run_sql") summary = step.success ? `${step.metadata.row_count ?? step.body?.row_count ?? ""} 行 · 只读查询` : step.errorCode || "run_sql";
  if (step.durationMs != null) summary = [summary, formatDuration(step.durationMs)].filter(Boolean).join(" · ");
  const title = step.name === "describe_table" && step.arguments.table
    ? `读取 ${step.arguments.table} 表结构` : toolNames[step.name] || step.name;
  return { title: step.pending ? `正在${title}` : title, symbol: step.pending ? "loader-circle" : toolIcons[step.name] || "wrench", summary };
}

function renderMessages(messages, target) {
  const toolCalls = new Map();
  const roles = { system: "系统规则", user: "用户问题", assistant: "模型回复", tool: "工具结果" };
  messages.forEach((message, index) => {
    // 按消息顺序配对之前的工具请求，避免历史中重复的 call_id 指向本轮调用。
    for (const call of message.tool_calls || []) toolCalls.set(call.id, call);
    const definition = toolCalls.get(message.tool_call_id);
    const envelope = message.role === "tool" ? parseJson(message.content) || {} : null;
    const body = envelope ? parseJson(envelope.content) : null;
    const label = message.role === "tool" ? [
      definition?.name || roles.tool, definition?.arguments?.table || body?.table,
      envelope.success === true ? "成功" : envelope.success === false ? "失败" : "",
    ].filter(Boolean).join(" · ") : roles[message.role] || message.role;
    const details = el("details", "message-item");
    details.append(el("summary", "", `${index + 1}. ${label}`));
    const content = el("div", "message-content");
    details.append(content);
    // 只有展开某条输入消息才构建表格；原始消息不修改，也不重新请求工具。
    details.addEventListener("toggle", () => {
      if (!details.open || content.childElementCount) return;
      if (message.role === "tool") {
        const call = {
          name: definition?.name, id: message.tool_call_id, arguments: definition?.arguments || {},
          success: envelope.success, errorCode: envelope.error_code, recovery: envelope.recovery,
          body, metadata: {}, text: envelope.content || "",
        };
        if (call.name === "run_sql" && (body || call.success === false)) renderSql(call, content);
        else if (call.success === false) content.append(el("div", "inline-error", [call.errorCode, call.text].filter(Boolean).join("\n")));
        else if (body && ["describe_table", "schema_retrieve", "schema_expand"].includes(call.name)) renderSchema(call, content);
        else if (message.content) content.append(el("pre", "json-content", pretty(envelope.content ?? message.content)));
        const raw = el("details", "message-item message-raw");
        raw.append(el("summary", "", "原始消息 JSON"), el("pre", "", pretty(message)));
        content.append(raw);
      } else {
        if (message.content) content.append(el("pre", "", pretty(message.content)));
        if (message.tool_calls?.length) content.append(el("pre", "", pretty(message.tool_calls)));
      }
      refreshIcons();
    });
    target.append(details);
  });
}

function renderStepContent(step, target) {
  // 回复下方的折叠步骤与调试页使用同一份详情，来源都是原 trace。
  if (step.kind === "model") {
    const grid = el("div", "step-content-grid");
    const main = el("div", "step-main");
    // 左侧只放主要内容；完整模型输入另作折叠，避免与本轮工具返回混淆。
    // 完成后看真实 assistant 消息；运行中先展示已收到的同轮工具请求。
    const toolCalls = step.output?.tool_calls ?? state.record.calls.filter((call) => call.turn === step.turn);
    const output = detailSection(toolCalls.length ? "模型返回的工具请求" : "模型返回");
    if (step.error) output.append(el("div", "inline-error", step.error));
    if (step.output?.content) { const text = el("div", "markdown"); renderMarkdown(step.output.content, text); output.append(text); }
    for (const call of toolCalls) output.append(el("pre", "json-content", pretty({ call_id: call.id, name: call.name, arguments: call.arguments })));
    if (!step.output?.content && !toolCalls.length) output.append(el("p", "subtle", step.pending ? "等待模型返回…" : state.record.status === "running" ? "完整回复将在运行结束后载入。" : "本轮没有已记录的可接受回复。"));
    const input = el("details", "message-item model-input");
    input.append(el("summary", "", `本轮发送给模型的输入 · ${step.messageCount} 条消息`));
    if (state.record.status === "running") input.append(el("p", "subtle", "完整消息将在运行结束后载入。"));
    else renderMessages(step.input, input);
    main.append(output, input);
    const modelMetrics = [
      ["状态", step.pending ? "等待模型" : step.error ? "请求失败" : step.toolCount == null ? "未记录" : "已返回"],
      ["输入消息数", step.messageCount], ["返回工具调用数", step.toolCount],
      ["模型请求耗时", formatDuration(step.durationMs)],
      ["输入 Token", formatTokens(step.usage?.prompt_tokens)],
      ["输出 Token", formatTokens(step.usage?.completion_tokens)],
      ["合计 Token", formatTokens(step.usage?.total_tokens)],
      ["模型轮次", step.turn],
    ];
    if (state.record.raw.conversation_id) modelMetrics.push(
      ["会话 ID", state.record.raw.conversation_id],
      ["其中历史消息数", state.record.raw.history_message_count],
    );
    const sidebar = detailSection("调用信息");
    sidebar.classList.add("step-sidebar");
    sidebar.append(keyValues(modelMetrics));
    grid.append(main, sidebar);
    target.append(grid);
  } else if (step.kind === "tool") {
    const grid = el("div", "step-content-grid");
    const main = el("div", "step-main");
    const request = detailSection(step.independent ? "独立检索参数" : "模型发出的工具请求");
    request.append(el("pre", "json-content", pretty({ name: step.name, arguments: step.arguments })));
    const result = detailSection("工具返回");
    if (step.success === false && step.name !== "run_sql") result.append(el("div", "inline-error", `${step.errorCode || "工具失败"}\n${step.text}`));
    if (step.name === "run_sql") renderSql(step, result);
    else if (step.success) renderSchema(step, result);
    else if (step.pending) result.append(el("p", "subtle", "等待工具返回结果…"));
    main.append(request, result);
    const sidebar = detailSection("执行信息");
    sidebar.classList.add("step-sidebar");
    sidebar.append(keyValues([
      ["工具名称", step.name],
      ["执行状态", step.pending ? "执行中" : step.success === true ? "成功" : step.success === false ? "失败" : "未记录"],
      ["执行耗时", formatDuration(step.durationMs)],
      ["读取的表", step.name === "describe_table" ? step.arguments.table : undefined],
      ["结构来源", step.body?.source],
      ["返回字段数", step.name === "describe_table" ? step.body?.columns?.length : undefined],
      ["工具调用 ID", step.id],
      ["结果类型", step.independent ? "独立 Schema 结果" : undefined],
    ]));
    grid.append(main, sidebar);
    target.append(grid);
    if (Object.keys(step.metadata).length) {
      const metadata = el("details", "message-item"); metadata.append(el("summary", "", "原始工具 metadata"), el("pre", "", pretty(step.metadata))); main.append(metadata);
    }
  } else if (step.kind === "error") {
    target.append(el("div", "inline-error", step.error));
  } else {
    target.append(keyValues([["停止原因", step.reason], ["模型轮次", step.turn]]));
    if (step.tool_call_count != null) target.append(keyValues([
      ["工具尝试 / 上限", `${step.tool_call_count} / ${step.max_tool_calls}`],
      ["SQL 尝试 / 上限", `${step.sql_attempt_count} / ${step.max_sql_attempts}`],
    ]));
    if (step.blocked_call) {
      const blocked = detailSection("未执行的工具请求");
      blocked.append(el("pre", "json-content", pretty(step.blocked_call)));
      target.append(blocked);
    }
    if (step.failure) {
      const failure = detailSection(step.reason === "repeated_failed_sql" ? "原失败调用的诊断" : "导致停止的 SQL 错误");
      failure.append(keyValues([
        ["工具调用 ID", step.failure.call_id], ["错误码", step.failure.error_code],
        ["处理说明", step.failure.recovery?.instruction],
      ]));
      target.append(failure);
    }
    if (stopNotices[step.reason]) target.append(el("div", "inline-note", stopNotices[step.reason]));
  }
}

function renderStepDetail() {
  const target = $("step-detail"); target.replaceChildren();
  const step = state.record.steps[state.step];
  if (!step) { target.append(el("div", "muted-empty", "这份记录没有执行步骤")); return; }
  const info = stepInfo(step);
  const title = el("div", "detail-title"); title.append(icon(info.symbol), el("h2", "", info.title));
  target.append(title);
  renderStepContent(step, target);
  refreshIcons();
}

function renderSteps() {
  const list = $("step-navigation"); list.replaceChildren();
  let previousTurn = null;
  state.record.steps.forEach((step, index) => {
    if (step.turn !== previousTurn) { list.append(el("div", "round-heading", step.turn ? `第 ${step.turn} 轮` : "运行记录")); previousTurn = step.turn; }
    const info = stepInfo(step);
    const button = el("button", `step-item${index === state.step ? " active" : ""}`);
    button.type = "button"; button.setAttribute("aria-pressed", String(index === state.step));
    const text = el("span"); text.append(el("span", "step-title", info.title), el("span", "step-summary", info.summary));
    button.append(icon(info.symbol), text);
    if (step.kind === "tool") button.append(icon(
      step.pending ? "loader-circle" : step.success === false ? "circle-alert" : step.success === true ? "check" : "circle",
      `step-state${step.pending ? " spinning" : step.success === false ? " error" : ""}`,
    ));
    button.addEventListener("click", () => { state.step = index; renderSteps(); });
    list.append(button);
  });
  renderStepDetail();
}

function renderUsageSummary(record) {
  const values = $("usage-values"); values.replaceChildren();
  if (record.independent) return;
  const usage = record.raw.token_usage;
  const fields = [
    ["总耗时", formatDuration(record.raw.duration_ms)],
    ["模型请求耗时", formatDuration(record.raw.model_duration_ms)],
    ["工具执行耗时", formatDuration(record.raw.tool_duration_ms)],
    ["输入 Token", formatTokens(usage?.prompt_tokens)],
    ["输出 Token", formatTokens(usage?.completion_tokens)],
    ["合计 Token", formatTokens(usage?.total_tokens)],
  ];
  for (const [label, value] of fields) {
    const item = el("div"); item.append(el("dt", "", label), el("dd", "", value)); values.append(item);
  }
  const scripted = record.raw.mode?.startsWith("scripted_model");
  $("usage-note").textContent = usage
    ? `API 用量 · ${usage.recorded_calls} / ${usage.model_calls} 轮已记录${usage.recorded_calls < usage.model_calls ? " · 部分统计" : ""}`
    : scripted ? "脚本模型 · 未调用模型 API" : "Token 用量未记录";
}

function renderOverview() {
  const record = state.record;
  const status = statusInfo(record.status);
  $("run-overview").hidden = false;
  $("run-status").className = `status-badge ${status.style}`;
  $("run-status").textContent = record.independent ? "Schema 记录" : status.label;
  $("run-question").textContent = record.question;
  $("run-question").title = record.question;
  const metrics = $("run-metrics"); metrics.replaceChildren();
  const values = [];
  const budget = record.raw.execution_budget;
  if (record.rounds) values.push(["message-square", `${record.rounds} 轮模型调用`]);
  if (record.independent) values.push(["network", "独立结构检索"]);
  else if (budget) {
    values.push(["wrench", `${budget.tool_call_count} / ${budget.max_tool_calls} 次工具`]);
    values.push(["database", `${budget.sql_attempt_count} / ${budget.max_sql_attempts} 次 SQL`]);
  }
  else if (record.calls.length) values.push(["wrench", `${record.calls.length} 次工具调用`]);
  for (const [symbol, text] of values) { const item = el("span"); item.append(icon(symbol), document.createTextNode(text)); metrics.append(item); }
  renderUsageSummary(record);
  $("footer-status").textContent = localTime(record.raw.created_at, true) || "已载入运行记录";
}

function showRecord(raw, recordId = null) {
  // 更新前记住阅读位置；只有正在跟随底部时，才跟随新事件滚动。
  const follow = state.busy && state.followLatest && ["query", "debug"].includes(state.view);
  const scrollTop = viewport.scrollTop;
  const record = normalize(raw);
  if (!state.busy) { state.openProcessSteps.clear(); state.processRounds.clear(); state.progressOpen = false; }
  state.raw = raw; state.record = record; state.historyId = recordId;
  state.step = record.status === "running" ? Math.max(0, record.steps.length - 1) : 0;
  $("empty-state").hidden = true;
  $("download-record").disabled = record.status === "running";
  $("raw-record-content").textContent = JSON.stringify(raw, null, 2);
  showNotice(raw.error || stopNotices[record.status] || "", Boolean(stopNotices[record.status]));
  renderOverview();
  if (record.status !== "running") renderConversationContext(record);
  renderQuery(); renderSteps(); setView(state.view);
  if (record.status !== "running") renderHistory();
  refreshIcons();
  if (follow) requestAnimationFrame(() => { viewport.scrollTop = viewport.scrollHeight; });
  else viewport.scrollTop = state.busy ? scrollTop : 0;
}

async function refreshHistory() {
  historyState.records = await api("/api/runs");
  renderHistory();
}

function historyButton(record, index = null) {
  const button = el("button", `history-item${record.id === state.historyId ? " active" : ""}`);
  button.type = "button"; button.dataset.recordId = record.id; button.title = record.question; button.disabled = state.busy;
  button.append(el("span", "history-question", index == null ? record.question : `${index + 1}. ${record.question}`));
  const info = statusInfo(record.status);
  const meta = el("span", "history-meta");
  const status = el("span", `history-status ${info.style}`); status.append(icon(info.icon), document.createTextNode(record.kind === "schema" ? "Schema" : info.label));
  meta.append(status, el("span", "", localTime(record.created_at, true)));
  button.append(meta);
  button.addEventListener("click", async () => {
    if (state.busy) return;
    try {
      const path = record.id.split("/").map(encodeURIComponent).join("/");
      const raw = await api(`/api/runs/${path}`);
      // 点选子记录仍只是回看，不把旧提问当成会话分支；续问有独立入口。
      setConversation(null);
      if (!["query", "debug"].includes(state.view)) setView("query");
      showRecord(raw, record.id);
      closeSidebar();
    } catch (error) { toast(error.message); }
  });
  return button;
}

function renderHistory() {
  const list = $("history-list");
  const scrollTop = list.scrollTop;
  list.replaceChildren();
  if (!historyState.records.length) list.append(el("div", "history-empty", "暂无运行记录"));
  const groups = new Map();
  // 列表已按最近时间倒序；Map 保留这一顺序，会话按最近提问的位置显示。
  for (const record of historyState.records) {
    const key = record.conversation_id && record.kind === "run"
      ? JSON.stringify([record.mode, record.conversation_id]) : record.id;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(record);
  }
  for (const [key, records] of groups) {
    if (!records[0].conversation_id || records[0].kind !== "run") {
      list.append(historyButton(records[0]));
      continue;
    }
    const latest = records[0];
    // 组内恢复提问顺序；保留每次运行的状态和调试入口，不把 trace 合并。
    const ordered = [...records].reverse();
    const selected = records.some((record) => record.id === state.historyId);
    const current = latest.conversation_id === state.conversationId && latest.mode === "deepseek_live_postgres_sql";
    const details = el("details", `history-group${selected || current ? " active" : ""}`);
    details.open = historyState.expanded.has(key) || selected || current;
    details.addEventListener("toggle", () => {
      // 重绘后旧节点的异步 toggle 不应覆盖新节点的展开状态。
      if (!details.isConnected) return;
      if (details.open) historyState.expanded.add(key); else historyState.expanded.delete(key);
    });
    const summary = el("summary", "history-group-heading");
    summary.title = `会话 ${latest.conversation_id}`;
    const title = el("span", "history-group-title");
    title.append(icon("chevron-right"), el("span", "history-question", ordered[0].question));
    const meta = el("span", "history-meta");
    const count = el("span", "history-status"); count.append(icon("messages-square"), document.createTextNode(`会话 · ${records.length} 次提问`));
    meta.append(count, el("span", "", localTime(latest.created_at, true)));
    summary.append(title, meta); details.append(summary);
    const children = el("div", "history-group-runs");
    if (latest.mode === "deepseek_live_postgres_sql") {
      const resume = iconButton("message-square-plus", "继续此会话（读取最新保存的历史）", () => {
        if (state.busy) return;
        setConversation(latest.conversation_id); clearQuery();
      });
      resume.classList.add("history-resume"); resume.disabled = state.busy;
      const actions = el("div", "history-group-actions");
      actions.append(el("span", "subtle", "提问记录"), resume); children.append(actions);
    }
    ordered.forEach((record, index) => children.append(historyButton(record, index)));
    details.append(children); list.append(details);
  }
  list.scrollTop = scrollTop;
  refreshIcons();
}

function syncExpandControl() {
  const disabled = state.busy || $("retrieval-mode").value === "off";
  $("schema-expand").disabled = disabled;
  if ($("retrieval-mode").value === "off") $("schema-expand").checked = false;
}

function setBusy(busy) {
  state.busy = busy;
  for (const id of ["run-button", "question", "retrieval-mode", "max-turns", "max-tools", "max-sql", "new-run", "open-record", "conversation-mode"]) $(id).disabled = busy;
  if ($("resume-conversation")) $("resume-conversation").disabled = busy;
  for (const button of document.querySelectorAll(".history-item, .history-resume")) button.disabled = busy;
  syncExpandControl(); syncConversationControls();
}

viewport.addEventListener("scroll", () => {
  state.followLatest = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 64;
  $("scroll-latest").hidden = state.followLatest || !state.record || !["query", "debug"].includes(state.view);
});
$("scroll-latest").addEventListener("click", () => {
  state.followLatest = true;
  viewport.scrollTop = viewport.scrollHeight;
  $("scroll-latest").hidden = true;
});

$("query-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy) return;
  const question = $("question").value.trim();
  if (!question) { $("question").focus(); return; }
  const request = {
    question, schema_retrieval: $("retrieval-mode").value,
    // 浏览器只发送会话 ID；历史由服务端从原会话文件读取。
    conversation_id: $("conversation-mode").checked ? state.conversationId : undefined,
    schema_expand: $("schema-expand").checked, max_model_turns: $("max-turns").valueAsNumber,
    // 留空时由后端使用默认额度；默认数字也从后端配置读取，避免两端分别维护。
    max_tool_calls: $("max-tools").value === "" ? undefined : $("max-tools").valueAsNumber,
    max_sql_attempts: $("max-sql").value === "" ? undefined : $("max-sql").valueAsNumber,
  };
  const live = {
    question, conversation_id: request.conversation_id, mode: "deepseek_live_postgres_sql",
    status: "running", events: [], messages: [], final_answer: null,
  };
  // 问题已进入本次请求和运行概要；输入框立即留给下一次提问。
  $("question").value = "";
  state.followLatest = true; state.progressOpen = false;
  state.openProcessSteps.clear();
  state.processRounds.clear();
  setBusy(true);
  if (!["query", "debug"].includes(state.view)) setView("query");
  showRecord(live);
  $("footer-status").textContent = "正在执行";
  const started = Date.now();
  const elapsed = el("span"); $("run-metrics").append(elapsed);
  const timer = setInterval(() => { elapsed.textContent = `${((Date.now() - started) / 1000).toFixed(1)} s`; }, 200);
  let reader;
  try {
    const response = await apiResponse("/api/runs/stream", {
      method: "POST", headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
      body: JSON.stringify(request),
    });
    reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalRecord = null;
    // 网络块不是完整事件：UTF-8 解码保留状态，遇到空行才解析一整帧。
    while (finalRecord === null) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        // 本接口每帧固定一行 event 和一行 JSON data；JSON 内部换行由服务端转义。
        const [eventLine, dataLine] = frame.split("\n");
        const kind = eventLine.slice("event: ".length);
        const packet = JSON.parse(dataLine.slice("data: ".length));
        if (kind === "started") $("footer-status").textContent = "已连接 · 正在准备上下文";
        if (kind === "trace") {
          live.events.push(packet);
          showRecord(live);
          $("run-metrics").append(elapsed);
          const last = state.record.steps.at(-1);
          $("footer-status").textContent = packet.kind === "run_stopped" ? "正在保存运行记录" : stepInfo(last).title;
        }
        if (kind === "result") {
          finalRecord = packet;
          showRecord(packet, packet.record_id);
        }
      }
    }
    // 只有最终包才确认本次记录已保存；连接结束本身不代表 Agent 完成。
    if (!finalRecord) throw new Error("连接已结束，但未收到最终记录；查询可能仍在运行，请刷新运行记录查看。");
    await refreshHistory().catch(() => toast("查询已完成，记录列表刷新失败"));
  } catch (error) {
    // 传输中断时保留已经收到的 SQL 和结果，方便继续查看已有证据。
    showRecord({ ...live, status: "error", error: error.message, duration_ms: Date.now() - started });
  } finally {
    reader?.releaseLock(); clearInterval(timer); setBusy(false);
    if (["query", "debug"].includes(state.view)) $("question").focus({ preventScroll: true });
  }
});

for (const button of document.querySelectorAll(".view-tab")) {
  button.addEventListener("click", () => setView(button.dataset.view));
  button.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...document.querySelectorAll(".view-tab")];
    const direction = event.key === "ArrowRight" ? 1 : -1;
    const next = tabs[(tabs.indexOf(button) + direction + tabs.length) % tabs.length];
    setView(next.dataset.view); next.focus();
  });
}
$("schema-search").addEventListener("input", renderSchemaCatalogList);
$("refresh-schema").addEventListener("click", loadSchemaCatalog);
$("refresh-schema-status").addEventListener("click", refreshSchemaIndexStatus);
$("refresh-statistics").addEventListener("click", loadStatistics);
$("statistics-mode").addEventListener("change", renderStatistics);
$("retrieval-mode").addEventListener("change", syncExpandControl);
$("conversation-mode").addEventListener("change", () => {
  setConversation($("conversation-mode").checked ? crypto.randomUUID() : null);
  renderHistory();
});
$("refresh-history").addEventListener("click", () => refreshHistory().catch((error) => toast(error.message)));
$("open-record").addEventListener("click", () => $("record-file").click());
$("record-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  try {
    let text = (await file.text()).trim().replace(/^\uFEFF/, "");
    // 独立检索命令有一行 Mode 标题；允许直接打开这类粘贴文本。
    if (text.startsWith("Mode:")) text = text.slice(text.indexOf("\n") + 1).trim();
    const raw = JSON.parse(text);
    setConversation(null);
    showRecord(raw); setView("debug");
    $("footer-status").textContent = `本地文件 · ${file.name}`;
    closeSidebar();
  } catch (error) { showNotice(error instanceof SyntaxError ? "文件中的 JSON 无法解析。" : error.message); }
  finally { event.target.value = ""; }
});

$("download-record").addEventListener("click", () => {
  if (!state.raw) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(state.raw, null, 2)], { type: "application/json" }));
  const link = el("a"); link.href = url; link.download = `transitmind-${state.raw.run_id || "record"}.json`;
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

function closeSidebar() { $("sidebar").classList.remove("open"); $("sidebar-backdrop").hidden = true; $("toggle-sidebar").setAttribute("aria-expanded", "false"); }
$("toggle-sidebar").addEventListener("click", () => { const open = $("sidebar").classList.toggle("open"); $("sidebar-backdrop").hidden = !open; $("toggle-sidebar").setAttribute("aria-expanded", String(open)); });
$("sidebar-backdrop").addEventListener("click", closeSidebar);
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeSidebar(); });
$("new-run").addEventListener("click", () => {
  setConversation($("conversation-mode").checked ? crypto.randomUUID() : null);
  clearQuery();
});

async function initialize() {
  $("local-address").textContent = location.host;
  // 刷新后明确回到独立查询；可从已有记录显式恢复会话。
  setConversation(null); syncExpandControl();
  const results = await Promise.allSettled([api("/api/config"), refreshHistory()]);
  if (results[0].status === "fulfilled") {
    const configuration = results[0].value;
    const snapshot = configuration.schema_snapshot;
    for (const [id, key] of [["max-tools", "max_tool_calls"], ["max-sql", "max_sql_attempts"]]) {
      if ($(id).value === "") $(id).value = configuration.execution_budget_defaults[key];
    }
    $("snapshot-label").textContent = snapshot ? `Schema · ${snapshot.table_count} 张表` : "尚无 Schema 快照";
    $("snapshot-label").title = snapshot?.version || "";
    if (!snapshot) { $("retrieval-mode").value = "off"; syncExpandControl(); }
  }
  for (const result of results) if (result.status === "rejected") showNotice(result.reason.message);
}

initialize();
