"use strict";

const $ = (id) => document.getElementById(id);
const state = { raw: null, record: null, view: "query", step: 0, historyId: null, busy: false };
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

async function api(path, options) {
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) {
    const detail = payload.detail;
    const message = Array.isArray(detail) ? detail.map((item) => item.msg).join("；") : detail;
    throw new Error(message || `请求失败（${response.status}）`);
  }
  return payload;
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
  const toolMessages = new Map();
  for (const message of messages) {
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
  state.view = view;
  const schemaView = view === "schema";
  for (const button of document.querySelectorAll(".view-tab")) {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  }
  $("query-panel").hidden = !state.record || view !== "query";
  $("debug-panel").hidden = !state.record || view !== "debug";
  $("schema-panel").hidden = !schemaView;
  $("query-composer").hidden = schemaView;
  $("run-overview").hidden = schemaView || (!state.record && !state.busy);
  $("usage-summary").hidden = schemaView || !state.record || state.record.independent;
  $("empty-state").hidden = schemaView || Boolean(state.record);
  $("notice").hidden = schemaView || !$("notice").textContent;
  $("open-record").hidden = schemaView;
  $("download-record").hidden = schemaView;
  if (schemaView && !schemaState.snapshot) loadSchemaCatalog();
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
  section.append(heading(index ? `查询结果 ${index + 1}` : "查询结果", "table-2", call.success ? `显示 ${rows.length} 行 · 保留 ${retained} 行` : "执行未成功"));
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
          (match.primary_key || []).includes(column.name) ? "PK" : "",
          ...(match.foreign_keys || []).filter((key) => key.column === column.name).map((key) => `FK → ${key.referenced_table}.${key.referenced_column}`),
        ].filter(Boolean).join(" / "), column.description || "",
      ]);
      details.append(renderTable(["字段", "类型", "可空", "键", "说明"], rows));
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
  // 单独显示 SQL 和真实结果
  record.sqlCalls.forEach((call, index) => renderSql(call, panel, index));
  if (!record.sqlCalls.length) {
    const schemas = record.calls.filter((call) => ["schema_retrieve", "schema_expand", "describe_table"].includes(call.name));
    for (const call of schemas) {
      const section = el("section", "data-section");
      section.append(heading(toolNames[call.name], toolIcons[call.name]));
      if (call.success === false) section.append(el("div", "inline-error", call.text || call.errorCode));
      else renderSchema(call, section);
      panel.append(section);
    }
  }
  if (!panel.childElementCount) panel.append(el("div", "muted-empty", record.raw.error ? "本次运行未产生查询结果" : "本轮尚未生成最终回答"));
}

function stepInfo(step) {
  if (step.kind === "model") return {
    title: step.error ? "模型请求失败" : step.toolCount === 0 ? "生成最终回答" : "模型调用",
    symbol: step.error ? "circle-alert" : "message-square",
    summary: `输入 ${step.messageCount} 条消息${step.toolCount == null ? "" : ` · ${step.toolCount} 个工具调用`}${step.durationMs == null ? "" : ` · ${formatDuration(step.durationMs)}`}`,
  };
  if (step.kind === "stop") return { title: step.reason === "completed" ? "任务完成" : statusInfo(step.reason).label, symbol: "flag", summary: step.reason };
  if (step.kind === "error") return { title: "运行失败", symbol: "circle-alert", summary: "error" };
  let summary = step.name;
  if (step.name === "schema_expand") summary = (step.arguments.seed_tables || []).join(", ");
  if (step.name === "describe_table") summary = step.arguments.table;
  if (step.name === "schema_retrieve") summary = step.arguments.query;
  if (step.name === "run_sql") summary = step.success ? `${step.metadata.row_count ?? step.body?.row_count ?? ""} 行 · 只读查询` : step.errorCode || "run_sql";
  if (step.durationMs != null) summary += ` · ${step.durationMs} ms`;
  return { title: toolNames[step.name] || step.name, symbol: toolIcons[step.name] || "wrench", summary };
}

function renderMessages(messages, target) {
  messages.forEach((message, index) => {
    const details = el("details", "message-item");
    const roles = { system: "系统规则", user: "用户", assistant: "模型", tool: "工具结果" };
    details.append(el("summary", "", `${index + 1}. ${roles[message.role] || message.role}`));
    if (message.content) details.append(el("pre", "", pretty(message.content)));
    if (message.tool_calls?.length) details.append(el("pre", "", pretty(message.tool_calls)));
    target.append(details);
  });
}

function renderStepDetail() {
  const target = $("step-detail"); target.replaceChildren();
  const step = state.record.steps[state.step];
  if (!step) { target.append(el("div", "muted-empty", "这份记录没有执行步骤")); return; }
  const info = stepInfo(step);
  const title = el("div", "detail-title"); title.append(icon(info.symbol), el("h2", "", info.title));
  target.append(title, el("div", "detail-meta", step.independent ? "独立 Schema 结果" : step.kind === "tool" ? `${step.name} · ${step.id}` : step.turn ? `第 ${step.turn} 轮` : "运行记录"));
  if (step.kind === "model") {
    const modelMetrics = [
      ["输入消息数", step.messageCount], ["返回工具调用数", step.toolCount],
      ["模型请求耗时", formatDuration(step.durationMs)],
      ["输入 Token", formatTokens(step.usage?.prompt_tokens)],
      ["输出 Token", formatTokens(step.usage?.completion_tokens)],
      ["合计 Token", formatTokens(step.usage?.total_tokens)],
    ];
    target.append(keyValues(modelMetrics));
    if (step.error) target.append(el("div", "inline-error", step.error));
    const input = detailSection("发送给模型的消息"); renderMessages(step.input, input); target.append(input);
    if (step.output?.role === "assistant") {
      const output = detailSection("模型返回");
      if (step.output.content) { const text = el("div", "markdown"); renderMarkdown(step.output.content, text); output.append(text); }
      for (const call of step.output.tool_calls || []) output.append(el("pre", "json-content", pretty({ name: call.name, arguments: call.arguments })));
      target.append(output);
    }
  } else if (step.kind === "tool") {
    if (step.durationMs != null) target.append(keyValues([["执行耗时", formatDuration(step.durationMs)]]));
    if (step.name !== "run_sql") {
      const argumentsSection = detailSection("调用参数"); argumentsSection.append(el("pre", "json-content", pretty(step.arguments))); target.append(argumentsSection);
    }
    if (step.success === false && step.name !== "run_sql") target.append(el("div", "inline-error", `${step.errorCode || "工具失败"}\n${step.text}`));
    if (step.name === "run_sql") renderSql(step, target);
    else if (step.success) renderSchema(step, target);
    if (Object.keys(step.metadata).length) {
      const metadata = el("details", "message-item"); metadata.append(el("summary", "", "工具 metadata"), el("pre", "", pretty(step.metadata))); target.append(metadata);
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
    if (step.kind === "tool") button.append(icon(step.success === false ? "circle-alert" : "check", `step-state${step.success === false ? " error" : ""}`));
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
  const record = normalize(raw);
  state.raw = raw; state.record = record; state.step = 0; state.historyId = recordId;
  $("empty-state").hidden = true;
  $("download-record").disabled = false;
  $("raw-record-content").textContent = JSON.stringify(raw, null, 2);
  if (raw.question) $("question").value = raw.question;
  showNotice(raw.error || stopNotices[record.status] || "", Boolean(stopNotices[record.status]));
  renderOverview(); renderQuery(); renderSteps(); setView(state.view);
  for (const item of document.querySelectorAll(".history-item")) item.classList.toggle("active", item.dataset.recordId === recordId);
  refreshIcons();
}

async function refreshHistory() {
  const records = await api("/api/runs");
  const list = $("history-list"); list.replaceChildren();
  if (!records.length) list.append(el("div", "history-empty", "暂无运行记录"));
  for (const record of records) {
    const button = el("button", `history-item${record.id === state.historyId ? " active" : ""}`);
    button.type = "button"; button.dataset.recordId = record.id; button.title = record.question; button.disabled = state.busy;
    button.append(el("span", "history-question", record.question));
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
        if (state.view === "schema") setView("query");
        showRecord(raw, record.id);
        closeSidebar();
      } catch (error) { showNotice(error.message); }
    });
    list.append(button);
  }
  refreshIcons();
}

function syncExpandControl() {
  const disabled = state.busy || $("retrieval-mode").value === "off";
  $("schema-expand").disabled = disabled;
  if ($("retrieval-mode").value === "off") $("schema-expand").checked = false;
}

function setBusy(busy) {
  state.busy = busy;
  for (const id of ["run-button", "question", "retrieval-mode", "max-turns", "max-tools", "max-sql", "new-run", "open-record"]) $(id).disabled = busy;
  for (const button of document.querySelectorAll(".history-item")) button.disabled = busy;
  $("run-button").replaceChildren(icon(busy ? "loader-circle" : "play", busy ? "spinning" : ""), el("span", "", busy ? "运行中" : "运行查询"));
  syncExpandControl(); refreshIcons();
}

$("query-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy) return;
  const question = $("question").value.trim();
  if (!question) { $("question").focus(); return; }
  const request = {
    question, schema_retrieval: $("retrieval-mode").value,
    schema_expand: $("schema-expand").checked, max_model_turns: $("max-turns").valueAsNumber,
    // 留空时由后端使用默认额度；默认数字也从后端配置读取，避免两端分别维护。
    max_tool_calls: $("max-tools").value === "" ? undefined : $("max-tools").valueAsNumber,
    max_sql_attempts: $("max-sql").value === "" ? undefined : $("max-sql").valueAsNumber,
  };
  state.raw = null; state.record = null; state.historyId = null;
  showNotice(""); setView(state.view); setBusy(true);
  $("download-record").disabled = true;
  $("empty-state").hidden = false;
  $("empty-state").querySelector("h2").textContent = "正在执行查询";
  $("run-overview").hidden = false;
  $("run-status").className = "status-badge neutral"; $("run-status").textContent = "运行中";
  $("run-question").textContent = question;
  $("footer-status").textContent = "正在执行";
  const started = Date.now();
  const timer = setInterval(() => { $("run-metrics").textContent = `${((Date.now() - started) / 1000).toFixed(1)} s`; }, 200);
  try {
    const raw = await api("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) });
    // 等待的是整次 Agent 运行完成
    clearInterval(timer); showRecord(raw, raw.record_id);
    await refreshHistory().catch(() => toast("查询已完成，记录列表刷新失败"));
  } catch (error) {
    clearInterval(timer);
    showRecord({ question, status: "error", error: error.message, events: [], messages: [], duration_ms: Date.now() - started });
  } finally { clearInterval(timer); setBusy(false); }
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
$("retrieval-mode").addEventListener("change", syncExpandControl);
$("refresh-history").addEventListener("click", () => refreshHistory().catch((error) => showNotice(error.message)));
$("open-record").addEventListener("click", () => $("record-file").click());
$("record-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  try {
    let text = (await file.text()).trim().replace(/^\uFEFF/, "");
    // 独立检索命令有一行 Mode 标题；允许直接打开这类粘贴文本。
    if (text.startsWith("Mode:")) text = text.slice(text.indexOf("\n") + 1).trim();
    const raw = JSON.parse(text);
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
  state.raw = null; state.record = null; state.historyId = null;
  $("question").value = ""; $("run-overview").hidden = true; $("download-record").disabled = true;
  $("empty-state").hidden = false; $("empty-state").querySelector("h2").textContent = "等待查询";
  $("footer-status").textContent = "本地会话";
  for (const item of document.querySelectorAll(".history-item")) item.classList.remove("active");
  showNotice(""); setView("query"); closeSidebar(); $("question").focus();
});

async function initialize() {
  $("local-address").textContent = location.host;
  refreshIcons(); syncExpandControl();
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
