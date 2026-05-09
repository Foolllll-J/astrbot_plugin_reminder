const bridge = window.AstrBotPluginPage;

const state = {
  loaded: {
    reminders: [],
    tasks: [],
    stats: {},
    webui_context_ready: false,
    webui_context: null,
  },
  currentKind: "reminder",
  currentName: "",
  search: "",
  draft: null,
  uploadType: "image",
  statusTimer: null,
};

const els = {
  itemList: document.getElementById("itemList"),
  listMeta: document.getElementById("listMeta"),
  searchInput: document.getElementById("searchInput"),
  emptyState: document.getElementById("emptyState"),
  editorForm: document.getElementById("editorForm"),
  editorTitle: document.getElementById("editorTitle"),
  editorModeLabel: document.getElementById("editorModeLabel"),
  statusBanner: document.getElementById("statusBanner"),
  saveBtn: document.getElementById("saveBtn"),
  triggerBtn: document.getElementById("triggerBtn"),
  newItemBtn: document.getElementById("newItemBtn"),
  nameInput: document.getElementById("nameInput"),
  typeInput: document.getElementById("typeInput"),
  cronMinute: document.getElementById("cronMinute"),
  cronHour: document.getElementById("cronHour"),
  cronDay: document.getElementById("cronDay"),
  cronMonth: document.getElementById("cronMonth"),
  cronWeekday: document.getElementById("cronWeekday"),
  cronPreview: document.getElementById("cronPreview"),
  sessionOrigin: document.getElementById("sessionOrigin"),
  sessionList: document.getElementById("sessionList"),
  executionCount: document.getElementById("executionCount"),
  executionRecallPanel: document.getElementById("executionRecallPanel"),
  recallDays: document.getElementById("recallDays"),
  recallHours: document.getElementById("recallHours"),
  recallMinutes: document.getElementById("recallMinutes"),
  recallSeconds: document.getElementById("recallSeconds"),
  reminderContentPanel: document.getElementById("reminderContentPanel"),
  taskCommandPanel: document.getElementById("taskCommandPanel"),
  taskCommandList: document.getElementById("taskCommandList"),
  linkedCommandPanel: document.getElementById("linkedCommandPanel"),
  linkedCommandList: document.getElementById("linkedCommandList"),
  messageStructureList: document.getElementById("messageStructureList"),
  hiddenUploadInput: document.getElementById("hiddenUploadInput"),
};

function clearStatusTimer() {
  if (state.statusTimer) {
    window.clearTimeout(state.statusTimer);
    state.statusTimer = null;
  }
}

function setStatus(message, tone = "info", options = {}) {
  const { autoHide = tone === "success" } = options;
  clearStatusTimer();
  if (!message) {
    els.statusBanner.textContent = "";
    els.statusBanner.dataset.tone = "";
    els.statusBanner.classList.add("hidden");
    return;
  }

  els.statusBanner.textContent = message;
  els.statusBanner.dataset.tone = tone;
  els.statusBanner.classList.remove("hidden");

  if (autoHide) {
    state.statusTimer = window.setTimeout(() => {
      setStatus("");
    }, 2600);
  }
}

function hasWebuiContext() {
  return Boolean(state.loaded?.webui_context_ready);
}

function getContextHint() {
  const context = state.loaded?.webui_context;
  if (context && (context.creator_name || context.created_by)) {
    const who = context.creator_name || context.created_by;
    return `当前绑定用户：${who}，来源会话：${context.source_origin || "未知"}`;
  }
  return "请先让 AstrBot admin 在聊天中发送“启动提醒控制台”，再使用此页面。";
}

function getErrorMessage(error, fallback) {
  if (error && typeof error === "object") {
    if (typeof error.message === "string" && error.message.trim()) {
      return error.message.trim();
    }
    if (typeof error.error === "string" && error.error.trim()) {
      return error.error.trim();
    }
  }
  return fallback;
}

function setEditorLocked(locked) {
  const nodes = els.editorForm.querySelectorAll("input, textarea, select, button");
  for (const node of nodes) {
    if (node.type === "hidden") continue;
    node.disabled = locked;
  }
  els.newItemBtn.disabled = locked;
}

function makeEmptyDraft(kind) {
  return {
    original_name: "",
    name: "",
    is_task: kind === "task",
    is_paused: false,
    cron_fields: {
      minute: "0",
      hour: "9",
      day: "*",
      month: "*",
      weekday: "*",
    },
    enabled_sessions: [],
    execution: { mode: "unlimited", count: 0 },
    recall: { enabled: false, days: 0, hours: 0, minutes: 0, seconds: 0 },
    message_structure: kind === "task" ? [] : [{ type: "text", content: "" }],
    command: "",
    commands: kind === "task" ? [{ command: "" }] : [],
    linked_commands: [],
  };
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function getCurrentCollection() {
  return state.currentKind === "task" ? state.loaded.tasks : state.loaded.reminders;
}

function normalizeDraft(item) {
  const base = makeEmptyDraft(item?.is_task ? "task" : "reminder");
  const merged = {
    ...base,
    ...(item || {}),
    cron_fields: {
      ...base.cron_fields,
      ...(item?.cron_fields || {}),
    },
    execution: {
      ...base.execution,
      ...(item?.execution || {}),
    },
    recall: {
      ...base.recall,
      ...(item?.recall || {}),
    },
    enabled_sessions: Array.isArray(item?.enabled_sessions)
      ? item.enabled_sessions.map((session) => ({ ...session }))
      : [],
    message_structure: Array.isArray(item?.message_structure)
      ? item.message_structure.map((part) => ({ ...part }))
      : [...base.message_structure],
    commands: Array.isArray(item?.commands)
      ? item.commands.map((command) => ({ ...command }))
      : [],
    linked_commands: Array.isArray(item?.linked_commands)
      ? item.linked_commands.map((linked) => ({ ...linked }))
      : [],
  };

  if (merged.is_task) {
    merged.recall = { ...base.recall };
    merged.linked_commands = [];
    if (!merged.commands.length && item?.command) {
      merged.commands = [{ command: String(item.command) }];
    }
  } else if (!merged.message_structure.length) {
    merged.message_structure = [...base.message_structure];
  }

  merged.original_name = item?.original_name || item?.name || "";
  merged.command = String(item?.command || "");
  return merged;
}

function setDraftFromItem(item) {
  state.draft = normalizeDraft(item);
  state.currentKind = item.is_task ? "task" : "reminder";
  state.currentName = item.name;
  renderEditor();
  renderList();
}

function filterItems(items) {
  const query = state.search.trim().toLowerCase();
  if (!query) return items;
  return items.filter((item) => {
    const commandTexts = Array.isArray(item.commands)
      ? item.commands.map((entry) => entry.command || "")
      : [item.command || ""];
    const haystack = [
      item.name,
      item.cron_expr,
      ...commandTexts,
      ...(item.enabled_sessions || []).map((session) => `${session.kind} ${session.target_id} ${session.origin || ""}`),
    ]
      .join(" ")
      .toLowerCase();
    return haystack.includes(query);
  });
}

function describeContentMeta(item) {
  if (item.is_task) {
    const commands = Array.isArray(item.commands) ? item.commands : [];
    if (!commands.length) {
      return item.command ? `步骤：${item.command}` : "未填写步骤";
    }
    if (commands.length === 1) {
      return `步骤：${commands[0].command}`;
    }
    return `${commands.length} 个步骤`;
  }
  return `${(item.message_structure || []).length} 个内容组件`;
}

function renderList() {
  const items = filterItems(getCurrentCollection());
  const kindLabel = state.currentKind === "task" ? "任务" : "提醒";
  els.listMeta.textContent = `${kindLabel} ${items.length} 项`;
  els.itemList.innerHTML = items
    .map((item) => {
      const active = state.currentName === item.name && state.currentKind === (item.is_task ? "task" : "reminder");
      const sessionCount = (item.enabled_sessions || []).length;
      const statusText = item.is_paused ? "已暂停" : "运行中";
      const toggleLabel = item.is_paused ? "启动" : "暂停";
      const toggleClass = item.is_paused ? "play" : "pause";
      const titleText = escapeHtml(item.name);
      const cronText = escapeHtml(item.cron_expr || "");
      const metaText = escapeHtml(`${sessionCount} 个会话`);
      const contentText = escapeHtml(describeContentMeta(item));
      return `
        <article class="item-card ${active ? "active" : ""} ${item.is_paused ? "paused" : ""}" data-name="${escapeHtml(item.name)}">
          <div class="item-card-head">
            <div class="item-card-title">
              <div class="title-row">
                <h3 title="${titleText}">${titleText}</h3>
                <span class="status-pill ${item.is_paused ? "" : "live"}" title="${statusText}">${statusText}</span>
              </div>
            </div>
            <button
              class="item-toggle-button"
              type="button"
              data-toggle-item="${escapeHtml(item.name)}"
              aria-label="${toggleLabel}"
              title="${toggleLabel}"
            >
              <span class="toggle-glyph ${toggleClass}" aria-hidden="true"></span>
            </button>
          </div>
          <div class="item-meta">
            <span title="${cronText}">${cronText}</span>
            <span title="${metaText}">${metaText}</span>
          </div>
          <div class="item-meta"><span title="${contentText}">${contentText}</span></div>
        </article>
      `;
    })
    .join("");

  for (const node of els.itemList.querySelectorAll(".item-card")) {
    node.addEventListener("click", (event) => {
      if (event.target.closest("[data-toggle-item]")) {
        return;
      }
      const item = getCurrentCollection().find((entry) => entry.name === node.dataset.name);
      if (item) setDraftFromItem(item);
    });
  }

  for (const node of els.itemList.querySelectorAll("[data-toggle-item]")) {
    node.addEventListener("click", async (event) => {
      event.stopPropagation();
      const item = getCurrentCollection().find((entry) => entry.name === node.dataset.toggleItem);
      if (!item) return;
      await toggleItemActive(item);
    });
  }
}

function syncDraftFromInputs() {
  if (!state.draft) return;
  state.draft.name = els.nameInput.value.trim();
  state.draft.is_task = els.typeInput.value === "task";
  state.draft.cron_fields = {
    minute: els.cronMinute.value.trim(),
    hour: els.cronHour.value.trim(),
    day: els.cronDay.value.trim(),
    month: els.cronMonth.value.trim(),
    weekday: els.cronWeekday.value.trim(),
  };
  const executionCount = Math.max(0, Number(els.executionCount.value || 0));
  state.draft.execution = {
    mode: executionCount > 0 ? "limited" : "unlimited",
    count: executionCount,
  };
  const recallDays = Math.max(0, Number(els.recallDays.value || 0));
  const recallHours = Math.max(0, Number(els.recallHours.value || 0));
  const recallMinutes = Math.max(0, Number(els.recallMinutes.value || 0));
  const recallSeconds = Math.max(0, Number(els.recallSeconds.value || 0));
  state.draft.recall = {
    enabled: recallDays + recallHours + recallMinutes + recallSeconds > 0,
    days: recallDays,
    hours: recallHours,
    minutes: recallMinutes,
    seconds: recallSeconds,
  };
  if (state.draft.is_task) {
    state.draft.commands = (state.draft.commands || [])
      .map((item) => ({ command: String(item.command || "").trim() }))
      .filter((item) => item.command);
    state.draft.command = state.draft.commands[0]?.command || "";
  } else {
    state.draft.command = "";
  }
}

function renderSessions() {
  els.sessionList.innerHTML = (state.draft.enabled_sessions || [])
    .map(
      (session, index) => `
        <div class="session-row">
          <div>
            <div><strong>${escapeHtml(session.label || session.origin || "")}</strong></div>
            <div class="inline-hint">${escapeHtml(session.origin || "")}</div>
          </div>
          <button class="ghost-button" type="button" data-remove-session="${index}">移除</button>
        </div>
      `,
    )
    .join("");

  for (const node of els.sessionList.querySelectorAll("[data-remove-session]")) {
    node.addEventListener("click", () => {
      state.draft.enabled_sessions.splice(Number(node.dataset.removeSession), 1);
      renderSessions();
    });
  }
}

function renderMessageStructure() {
  els.messageStructureList.innerHTML = (state.draft.message_structure || [])
    .map((item, index) => {
      let body = "";
      if (item.type === "text") {
        body = `<textarea class="text-area" rows="3" data-component-text="${index}">${escapeHtml(item.content || "")}</textarea>`;
      } else if (item.type === "at") {
        body = `<input class="text-input mono" type="text" value="${escapeHtml(item.qq || "")}" data-component-qq="${index}" />`;
      } else if (item.type === "atall") {
        body = `<div class="inline-hint">@全体成员</div>`;
      } else {
        body = `<div class="inline-hint">${escapeHtml(item.path || "")}</div>`;
      }
      return `
        <div class="component-card">
          <div class="component-head">
            <span class="badge">${escapeHtml(item.type)}</span>
            <div class="button-row">
              <button class="ghost-button" type="button" data-move-up="${index}">上移</button>
              <button class="ghost-button" type="button" data-move-down="${index}">下移</button>
              <button class="danger-button" type="button" data-delete-component="${index}">删除</button>
            </div>
          </div>
          <div class="component-body">${body}</div>
        </div>
      `;
    })
    .join("");

  for (const area of els.messageStructureList.querySelectorAll("[data-component-text]")) {
    area.addEventListener("input", () => {
      state.draft.message_structure[Number(area.dataset.componentText)].content = area.value;
    });
  }
  for (const input of els.messageStructureList.querySelectorAll("[data-component-qq]")) {
    input.addEventListener("input", () => {
      state.draft.message_structure[Number(input.dataset.componentQq)].qq = input.value.trim();
    });
  }
  for (const node of els.messageStructureList.querySelectorAll("[data-delete-component]")) {
    node.addEventListener("click", () => {
      state.draft.message_structure.splice(Number(node.dataset.deleteComponent), 1);
      renderMessageStructure();
    });
  }
  for (const node of els.messageStructureList.querySelectorAll("[data-move-up]")) {
    node.addEventListener("click", () => moveMessageComponent(Number(node.dataset.moveUp), -1));
  }
  for (const node of els.messageStructureList.querySelectorAll("[data-move-down]")) {
    node.addEventListener("click", () => moveMessageComponent(Number(node.dataset.moveDown), 1));
  }
}

function moveMessageComponent(index, offset) {
  const target = index + offset;
  if (target < 0 || target >= state.draft.message_structure.length) return;
  const list = state.draft.message_structure;
  [list[index], list[target]] = [list[target], list[index]];
  renderMessageStructure();
}

function moveArrayItem(list, index, offset) {
  const target = index + offset;
  if (!Array.isArray(list) || target < 0 || target >= list.length) return false;
  [list[index], list[target]] = [list[target], list[index]];
  return true;
}

function renderTaskCommands() {
  els.taskCommandList.innerHTML = (state.draft.commands || [])
    .map(
      (item, index) => `
        <div class="linked-command-card">
          <div class="component-head">
            <span class="badge">step ${index + 1}</span>
            <div class="button-row">
              <button class="ghost-button" type="button" data-task-move-up="${index}">上移</button>
              <button class="ghost-button" type="button" data-task-move-down="${index}">下移</button>
              <button class="danger-button" type="button" data-delete-task-command="${index}">删除</button>
            </div>
          </div>
          <div class="component-body">
            <input class="text-input mono" type="text" value="${escapeHtml(item.command || "")}" data-task-command="${index}" />
          </div>
        </div>
      `,
    )
    .join("");

  for (const input of els.taskCommandList.querySelectorAll("[data-task-command]")) {
    input.addEventListener("input", () => {
      state.draft.commands[Number(input.dataset.taskCommand)].command = input.value;
    });
  }
  for (const node of els.taskCommandList.querySelectorAll("[data-delete-task-command]")) {
    node.addEventListener("click", () => {
      state.draft.commands.splice(Number(node.dataset.deleteTaskCommand), 1);
      renderTaskCommands();
    });
  }
  for (const node of els.taskCommandList.querySelectorAll("[data-task-move-up]")) {
    node.addEventListener("click", () => {
      if (moveArrayItem(state.draft.commands, Number(node.dataset.taskMoveUp), -1)) {
        renderTaskCommands();
      }
    });
  }
  for (const node of els.taskCommandList.querySelectorAll("[data-task-move-down]")) {
    node.addEventListener("click", () => {
      if (moveArrayItem(state.draft.commands, Number(node.dataset.taskMoveDown), 1)) {
        renderTaskCommands();
      }
    });
  }
}

function renderLinkedCommands() {
  els.linkedCommandList.innerHTML = (state.draft.linked_commands || [])
    .map(
      (item, index) => `
        <div class="linked-command-card">
          <div class="component-head">
            <span class="badge">command</span>
            <div class="button-row">
              <button class="ghost-button" type="button" data-linked-move-up="${index}">上移</button>
              <button class="ghost-button" type="button" data-linked-move-down="${index}">下移</button>
              <button class="danger-button" type="button" data-delete-linked="${index}">删除</button>
            </div>
          </div>
          <div class="component-body">
            <input class="text-input mono" type="text" value="${escapeHtml(item.command || "")}" data-linked-command="${index}" />
          </div>
        </div>
      `,
    )
    .join("");

  for (const input of els.linkedCommandList.querySelectorAll("[data-linked-command]")) {
    input.addEventListener("input", () => {
      state.draft.linked_commands[Number(input.dataset.linkedCommand)].command = input.value;
    });
  }
  for (const node of els.linkedCommandList.querySelectorAll("[data-delete-linked]")) {
    node.addEventListener("click", () => {
      state.draft.linked_commands.splice(Number(node.dataset.deleteLinked), 1);
      renderLinkedCommands();
    });
  }
  for (const node of els.linkedCommandList.querySelectorAll("[data-linked-move-up]")) {
    node.addEventListener("click", () => {
      if (moveArrayItem(state.draft.linked_commands, Number(node.dataset.linkedMoveUp), -1)) {
        renderLinkedCommands();
      }
    });
  }
  for (const node of els.linkedCommandList.querySelectorAll("[data-linked-move-down]")) {
    node.addEventListener("click", () => {
      if (moveArrayItem(state.draft.linked_commands, Number(node.dataset.linkedMoveDown), 1)) {
        renderLinkedCommands();
      }
    });
  }
}

function renderEditor() {
  if (!state.draft) {
    els.emptyState.classList.remove("hidden");
    els.editorForm.classList.add("hidden");
    if (hasWebuiContext()) {
      setStatus("");
    } else {
      setStatus(getContextHint(), "error", { autoHide: false });
    }
    return;
  }

  els.emptyState.classList.add("hidden");
  els.editorForm.classList.remove("hidden");

  const isTask = state.draft.is_task;
  if (isTask && (!Array.isArray(state.draft.commands) || !state.draft.commands.length)) {
    state.draft.commands = [{ command: state.draft.command || "" }];
  }
  els.editorModeLabel.textContent = isTask ? "Task" : "Reminder";
  els.editorTitle.textContent = state.draft.name || "未命名";
  els.nameInput.value = state.draft.name || "";
  els.typeInput.value = isTask ? "task" : "reminder";
  els.cronMinute.value = state.draft.cron_fields?.minute || "";
  els.cronHour.value = state.draft.cron_fields?.hour || "";
  els.cronDay.value = state.draft.cron_fields?.day || "";
  els.cronMonth.value = state.draft.cron_fields?.month || "";
  els.cronWeekday.value = state.draft.cron_fields?.weekday || "";
  els.executionCount.value = state.draft.execution?.count || 0;
  els.recallDays.value = state.draft.recall?.days || 0;
  els.recallHours.value = state.draft.recall?.hours || 0;
  els.recallMinutes.value = state.draft.recall?.minutes || 0;
  els.recallSeconds.value = state.draft.recall?.seconds || 0;

  els.executionRecallPanel.classList.toggle("hidden", isTask);
  els.reminderContentPanel.classList.toggle("hidden", isTask);
  els.linkedCommandPanel.classList.toggle("hidden", isTask);
  els.taskCommandPanel.classList.toggle("hidden", !isTask);

  updateCronPreview();
  renderSessions();
  renderMessageStructure();
  renderTaskCommands();
  renderLinkedCommands();
  setEditorLocked(!hasWebuiContext());
  if (!hasWebuiContext()) {
    setStatus(getContextHint(), "error", { autoHide: false });
  }
}

function updateCronPreview() {
  els.cronPreview.textContent = `cron: ${[
    els.cronMinute.value,
    els.cronHour.value,
    els.cronDay.value,
    els.cronMonth.value,
    els.cronWeekday.value,
  ]
    .join(" ")
    .trim()}`;
}

function buildSavePayload() {
  syncDraftFromInputs();
  return {
    original_name: state.draft.original_name || state.currentName || "",
    name: state.draft.name,
    is_task: state.draft.is_task,
    is_paused: Boolean(state.draft.is_paused),
    cron_fields: state.draft.cron_fields,
    enabled_sessions: state.draft.enabled_sessions,
    execution: state.draft.execution,
    recall: state.draft.recall,
    message_structure: state.draft.message_structure,
    command: state.draft.command,
    commands: state.draft.commands,
    linked_commands: state.draft.linked_commands,
  };
}

async function loadState() {
  try {
    const data = await bridge.apiGet("state");
    state.loaded = data;
    if (state.currentName) {
      const selected = [...data.reminders, ...data.tasks].find(
        (item) => item.name === state.currentName && item.is_task === (state.currentKind === "task"),
      );
      if (selected) {
        setDraftFromItem(selected);
        return;
      }
    }
    renderList();
    setEditorLocked(!hasWebuiContext());
    renderEditor();
    if (!hasWebuiContext()) {
      setStatus(getContextHint(), "error", { autoHide: false });
    }
  } catch (error) {
    setStatus(getErrorMessage(error, "加载提醒数据失败，请刷新重试"), "error", { autoHide: false });
  }
}

async function saveDraft(event) {
  event.preventDefault();
  if (!hasWebuiContext()) {
    setStatus(getContextHint(), "error", { autoHide: false });
    return;
  }
  const payload = buildSavePayload();
  const originalLabel = els.saveBtn.textContent;
  els.saveBtn.disabled = true;
  els.saveBtn.textContent = "保存中...";
  setStatus("正在保存变更...", "info", { autoHide: false });
  try {
    const response = await bridge.apiPost("save", payload);
    state.currentKind = response.item.is_task ? "task" : "reminder";
    state.currentName = response.item.name;
    await loadState();
    setStatus(`已保存：${response.item.name}`, "success");
  } catch (error) {
    setStatus(getErrorMessage(error, "保存失败，请检查必填项和参数格式"), "error", { autoHide: false });
  } finally {
    els.saveBtn.disabled = false;
    els.saveBtn.textContent = originalLabel;
  }
}

async function deleteCurrent() {
  if (!state.draft) return;
  if (!hasWebuiContext()) {
    setStatus(getContextHint(), "error", { autoHide: false });
    return;
  }
  setStatus("正在删除...", "info", { autoHide: false });
  try {
    await bridge.apiPost("delete", {
      name: state.draft.name,
      is_task: state.draft.is_task,
    });
    const deletedName = state.draft.name;
    state.currentName = "";
    state.draft = null;
    renderEditor();
    await loadState();
    setStatus(`已删除：${deletedName}`, "success");
  } catch (error) {
    setStatus(getErrorMessage(error, "删除失败"), "error", { autoHide: false });
  }
}

async function triggerCurrent() {
  if (!state.draft) return;
  if (!hasWebuiContext()) {
    setStatus(getContextHint(), "error", { autoHide: false });
    return;
  }
  const originalLabel = els.triggerBtn.textContent;
  els.triggerBtn.disabled = true;
  els.triggerBtn.textContent = "执行中...";
  setStatus("正在立即执行...", "info", { autoHide: false });
  try {
    await bridge.apiPost("trigger-now", {
      name: state.draft.name,
      is_task: state.draft.is_task,
    });
    setStatus(`已发起立即执行：${state.draft.name}`, "success");
  } catch (error) {
    setStatus(getErrorMessage(error, "立即执行失败"), "error", { autoHide: false });
  } finally {
    els.triggerBtn.disabled = false;
    els.triggerBtn.textContent = originalLabel;
  }
}

async function toggleItemActive(item) {
  if (!hasWebuiContext()) {
    setStatus(getContextHint(), "error", { autoHide: false });
    return;
  }
  const nextPaused = !Boolean(item.is_paused);
  setStatus(nextPaused ? `正在暂停：${item.name}` : `正在启动：${item.name}`, "info", { autoHide: false });
  try {
    const response = await bridge.apiPost("toggle-active", {
      name: item.name,
      is_task: item.is_task,
      is_paused: nextPaused,
    });
    if (state.draft && state.draft.name === response.item.name && state.draft.is_task === response.item.is_task) {
      state.draft.is_paused = response.item.is_paused;
    }
    await loadState();
    setStatus(response.item.is_paused ? `已暂停：${response.item.name}` : `已启动：${response.item.name}`, "success");
  } catch (error) {
    setStatus(getErrorMessage(error, "切换状态失败"), "error", { autoHide: false });
  }
}

function uploadComponent(kind) {
  if (!hasWebuiContext()) {
    setStatus(getContextHint(), "error", { autoHide: false });
    return;
  }
  state.uploadType = kind;
  els.hiddenUploadInput.click();
}

async function handleUploadChange() {
  const file = els.hiddenUploadInput.files?.[0];
  if (!file) return;
  setStatus(`正在上传 ${file.name}...`, "info", { autoHide: false });
  try {
    const response = await bridge.upload(`upload-media/${state.uploadType}`, file);
    state.draft.message_structure.push(response.item);
    renderMessageStructure();
    setStatus(`已添加媒体：${file.name}`, "success");
  } catch (error) {
    setStatus(getErrorMessage(error, "媒体上传失败"), "error", { autoHide: false });
  } finally {
    els.hiddenUploadInput.value = "";
  }
}

function applyCronPreset(preset) {
  if (preset === "daily") {
    els.cronMinute.value = "0";
    els.cronHour.value = "9";
    els.cronDay.value = "*";
    els.cronMonth.value = "*";
    els.cronWeekday.value = "*";
  } else if (preset === "workday") {
    els.cronMinute.value = "0";
    els.cronHour.value = "9";
    els.cronDay.value = "*";
    els.cronMonth.value = "*";
    els.cronWeekday.value = "1-5";
  } else if (preset === "hourly") {
    els.cronMinute.value = "0";
    els.cronHour.value = "*";
    els.cronDay.value = "*";
    els.cronMonth.value = "*";
    els.cronWeekday.value = "*";
  }
  updateCronPreview();
}

function createDraftForCurrentKind() {
  state.currentName = "";
  state.draft = makeEmptyDraft(state.currentKind);
  renderEditor();
  const label = state.currentKind === "task" ? "任务" : "提醒";
  setStatus(`已创建${label}草稿，填写后保存即可生效`, "info", { autoHide: true });
}

function installEvents() {
  for (const node of document.querySelectorAll(".tab")) {
    node.addEventListener("click", () => {
      for (const other of document.querySelectorAll(".tab")) {
        other.classList.toggle("active", other === node);
      }
      state.currentKind = node.dataset.kind;
      renderList();
    });
  }

  els.searchInput.addEventListener("input", () => {
    state.search = els.searchInput.value;
    renderList();
  });

  els.newItemBtn.addEventListener("click", () => {
    if (!hasWebuiContext()) {
      setStatus(getContextHint(), "error", { autoHide: false });
      return;
    }
    createDraftForCurrentKind();
  });

  document.getElementById("refreshBtn").addEventListener("click", async () => {
    setStatus("正在刷新数据...", "info", { autoHide: false });
    await loadState();
    if (!els.statusBanner.classList.contains("hidden") && els.statusBanner.dataset.tone === "error") {
      return;
    }
    setStatus("数据已刷新", "success");
  });

  els.editorForm.addEventListener("submit", saveDraft);
  document.getElementById("deleteBtn").addEventListener("click", deleteCurrent);
  document.getElementById("triggerBtn").addEventListener("click", triggerCurrent);

  document.getElementById("addSessionBtn").addEventListener("click", () => {
    const origin = els.sessionOrigin.value.trim();
    if (!/^.+:(GroupMessage|FriendMessage):\d+$/.test(origin)) {
      setStatus("目标会话 ID 格式不正确，示例：DemoBot:GroupMessage:123456789", "error", { autoHide: false });
      return;
    }
    state.draft.enabled_sessions.push({ origin });
    els.sessionOrigin.value = "";
    renderSessions();
    setStatus(`已添加会话：${origin}`, "success");
  });

  els.typeInput.addEventListener("change", () => {
    if (!state.draft) return;
    state.draft.is_task = els.typeInput.value === "task";
    renderEditor();
  });

  for (const input of [els.cronMinute, els.cronHour, els.cronDay, els.cronMonth, els.cronWeekday]) {
    input.addEventListener("input", updateCronPreview);
  }

  for (const node of document.querySelectorAll("[data-cron-preset]")) {
    node.addEventListener("click", () => applyCronPreset(node.dataset.cronPreset));
  }

  for (const node of document.querySelectorAll("[data-component]")) {
    node.addEventListener("click", () => {
      const type = node.dataset.component;
      if (type === "text") {
        state.draft.message_structure.push({ type: "text", content: "" });
      } else if (type === "at") {
        state.draft.message_structure.push({ type: "at", qq: "" });
      } else if (type === "atall") {
        state.draft.message_structure.push({ type: "atall" });
      }
      renderMessageStructure();
    });
  }

  for (const node of document.querySelectorAll(".upload-button")) {
    node.addEventListener("click", () => uploadComponent(node.dataset.uploadType));
  }

  els.hiddenUploadInput.addEventListener("change", handleUploadChange);
  document.getElementById("addTaskCommandBtn").addEventListener("click", () => {
    state.draft.commands.push({ command: "" });
    renderTaskCommands();
  });
  document.getElementById("addLinkedCommandBtn").addEventListener("click", () => {
    state.draft.linked_commands.push({ command: "", message_structure: [] });
    renderLinkedCommands();
  });
}

await bridge.ready();
installEvents();
await loadState();
