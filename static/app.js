const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  config: null,
  project: null,
  transcript: null,
  deleted: new Set(),
  deletedWords: new Set(),
  deletedTiming: new Set(),
  timingCleanupInitialized: false,
  subtitleEdits: new Map(),
  subtitleSearchQuery: "",
  subtitleSearchIndex: -1,
  currentSubtitleMatch: null,
  transcriptMode: "cut",
  subtitleSaveTimer: null,
  subtitleSavePromise: Promise.resolve(true),
  lastVideoId: "editVideo",
  jobTimer: null,
  saveTimer: null,
  fillersVisible: false,
  editHistory: [],
  editFuture: [],
  cleanupSuggestions: [],
  dismissedCleanup: new Set(),
  restartRequired: false,
  wordNodes: [],
  selectedWords: new Set(),
  selectionFrame: null,
  savePromise: Promise.resolve(true),
  activeJobId: null,
  editedPreviewEnabled: true,
  previewJumping: false,
  nativeFileBridgeReady: false,
};

const fillers = ["嗯", "啊", "呃", "那个", "就是说", "然后呢", "怎么说呢", "对吧"];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body && typeof options.body === "string" ? { "Content-Type": "application/json", ...(options.headers || {}) } : options.headers,
    ...options,
  });
  const contentType = response.headers.get("content-type") || "";
  const result = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) throw new Error(result.error || result || `请求失败 (${response.status})`);
  return result;
}

function showToast(message, error = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", error);
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 3300);
}

function formatDuration(seconds, detailed = false) {
  if (!Number.isFinite(Number(seconds))) return "—";
  const total = Math.max(0, Number(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = Math.floor(total % 60);
  if (detailed || h) return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function formatBytes(bytes) {
  if (!Number.isFinite(Number(bytes))) return "";
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function formatDate(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

async function loadConfig() {
  state.config = await api("/api/config");
  const badge = $("#engineBadge");
  state.restartRequired = Number(state.config.appVersion || 1) < 14;
  if (state.restartRequired) {
    badge.className = "engine-badge error";
    badge.innerHTML = "<i></i>请重启以加载今日升级";
  } else if (state.config.ready) {
    badge.className = "engine-badge";
    badge.innerHTML = "<i></i>本地识别引擎已就绪";
  } else {
    badge.className = "engine-badge error";
    badge.innerHTML = "<i></i>本地引擎不完整";
  }
}

function syncRecognitionOptions() {
  const engines = state.config?.speechEngines || [];
  const engineSelect = $("#engineSelect");
  const modelSelect = $("#modelSelect");
  engineSelect.innerHTML = "";
  for (const engine of engines) {
    const option = document.createElement("option");
    option.value = engine.id;
    option.textContent = engine.compatible ? engine.name : `${engine.name}（暂未适配）`;
    option.disabled = !engine.compatible || !engine.available;
    engineSelect.append(option);
  }
  const recognition = state.project?.recognition || state.config?.defaultRecognition || {};
  const selectedEngine = engines.find((engine) => engine.id === recognition.engineId && engine.compatible)
    || engines.find((engine) => engine.compatible);
  if (selectedEngine) engineSelect.value = selectedEngine.id;
  modelSelect.innerHTML = "";
  for (const model of selectedEngine?.models || []) {
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent = `${model.name} · ${model.profile}${model.recommended ? "（推荐）" : ""}`;
    modelSelect.append(option);
  }
  const wantedModel = recognition.modelId || state.config?.defaultRecognition?.modelId;
  if ([...modelSelect.options].some((option) => option.value === wantedModel)) modelSelect.value = wantedModel;
  const selectedModel = (selectedEngine?.models || []).find((model) => model.id === modelSelect.value);
  const usable = Boolean(selectedEngine?.available && selectedModel && selectedModel.ready !== false);
  engineSelect.disabled = !selectedEngine;
  modelSelect.disabled = !usable;
  $("#transcribeButton").disabled = !usable || !state.project?.source;
  $("#recognitionCapability").textContent = usable
    ? `发现 ${(selectedEngine.models || []).length} 个可用模型；这里的选择只影响当前项目。Subtitle Edit 中新下载模型后点“重新扫描”。`
    : (selectedModel?.note || selectedEngine?.note || "没有发现可用的本地识别模型。");
  $("#selectedModelNote").textContent = selectedModel
    ? `${selectedEngine.name} · ${selectedModel.name} · ${formatBytes(selectedModel.size)}${selectedModel.note ? ` · ${selectedModel.note}` : ""}`
    : "未发现兼容模型";
}

async function saveRecognitionSelection(selection = null) {
  if (!state.project) return;
  const body = selection || { engineId: $("#engineSelect").value, modelId: $("#modelSelect").value, language: "zh" };
  state.project = await api(`/api/projects/${state.project.id}/recognition`, { method: "POST", body: JSON.stringify(body) });
  syncRecognitionOptions();
}

async function changeRecognitionEngine() {
  const engineId = $("#engineSelect").value;
  const engine = (state.config?.speechEngines || []).find((item) => item.id === engineId);
  const models = engine?.models || [];
  const model = models.find((item) => item.recommended && item.ready !== false)
    || models.find((item) => item.ready !== false)
    || models[0];
  if (!engine || !model) {
    syncRecognitionOptions();
    throw new Error(engine?.note || "所选引擎没有可用模型");
  }
  try {
    await saveRecognitionSelection({ engineId, modelId: model.id, language: "zh" });
  } catch (error) {
    syncRecognitionOptions();
    throw error;
  }
}

async function refreshRecognitionModels() {
  try {
    await loadConfig();
    syncRecognitionOptions();
    showToast("已重新扫描 Subtitle Edit 模型目录");
  } catch (error) { showToast(error.message, true); }
}

async function loadProjects() {
  const { projects } = await api("/api/projects");
  const container = $("#recentProjects");
  container.innerHTML = "";
  if (!projects.length) {
    container.innerHTML = '<div class="empty-projects">还没有项目。导入第一段视频后，它会出现在这里。</div>';
    return;
  }
  for (const project of projects.slice(0, 9)) {
    const card = document.createElement("article");
    card.className = "project-card";
    card.innerHTML = `<button class="project-delete" type="button" title="删除项目" aria-label="删除项目">×</button><span>${formatDate(project.updatedAt)}</span><h3></h3><p></p>`;
    card.querySelector("h3").textContent = project.name;
    card.querySelector("p").textContent = `${project.status || "等待处理"}${project.source?.duration ? ` · ${formatDuration(project.source.duration)}` : ""}`;
    card.querySelector(".project-delete").addEventListener("click", (event) => {
      event.stopPropagation();
      confirmDeleteProject(project);
    });
    card.addEventListener("click", () => openProject(project.id));
    container.appendChild(card);
  }
}

async function loadTrash() {
  const { items } = await api("/api/trash");
  const container = $("#trashProjects");
  const total = items.reduce((sum, item) => sum + Number(item.size || 0), 0);
  $("#trashSummary").textContent = items.length ? `${items.length} 个项目 · 占用 ${formatBytes(total)}` : "回收区为空";
  container.innerHTML = "";
  if (!items.length) {
    container.innerHTML = '<div class="empty-projects">这里没有已删除的项目。</div>';
    return;
  }
  for (const item of items) {
    const row = document.createElement("article");
    row.className = "trash-item";
    row.innerHTML = `<div><span></span><h3></h3><p></p></div><div class="trash-actions"><button class="button small secondary restore">恢复</button><button class="button small danger purge">永久删除</button></div>`;
    row.querySelector("span").textContent = formatDate(item.deletedAt);
    row.querySelector("h3").textContent = item.name;
    row.querySelector("p").textContent = `${item.status || "已删除"} · ${formatBytes(item.size)}`;
    row.querySelector(".restore").addEventListener("click", async () => {
      try {
        await api(`/api/trash/${item.trashId}/restore`, { method: "POST", body: JSON.stringify({}) });
        await Promise.all([loadTrash(), loadProjects()]);
        showToast("项目已恢复");
      } catch (error) { showToast(error.message, true); }
    });
    row.querySelector(".purge").addEventListener("click", async () => {
      if (!window.confirm(`永久删除“${item.name}”？\n\n这会删除项目文件和内部复制的视频，无法恢复。`)) return;
      try {
        await api(`/api/trash/${item.trashId}`, { method: "DELETE" });
        await loadTrash();
        showToast("项目已永久删除");
      } catch (error) { showToast(error.message, true); }
    });
    container.appendChild(row);
  }
}

async function toggleTrash(forceOpen = null) {
  const section = $("#trashSection");
  const shouldOpen = forceOpen ?? section.classList.contains("hidden");
  section.classList.toggle("hidden", !shouldOpen);
  if (shouldOpen) {
    try {
      await loadTrash();
      section.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) { showToast(error.message, true); }
  }
}

async function flushProjectSaves() {
  if (!state.project) return true;
  const results = await Promise.all([saveEdit(), saveSubtitleEdits()]);
  return results.every(Boolean);
}

window.textCutFlushSaves = flushProjectSaves;

async function showWelcome(saveFirst = true) {
  if (saveFirst && state.project && !(await flushProjectSaves())) return false;
  clearInterval(state.jobTimer);
  clearTimeout(state.saveTimer);
  clearTimeout(state.subtitleSaveTimer);
  state.saveTimer = null;
  state.subtitleSaveTimer = null;
  releaseProjectMedia();
  state.project = null;
  state.transcript = null;
  state.wordNodes = [];
  state.selectedWords = new Set();
  state.subtitleEdits = new Map();
  state.activeJobId = null;
  $("#welcomeView").classList.remove("hidden");
  $("#editorView").classList.add("hidden");
  setNativeImportEnabled(true);
  history.replaceState(null, "", location.pathname);
  loadProjects().catch((error) => showToast(error.message, true));
  return true;
}

function releaseProjectMedia() {
  for (const selector of ["#sourceVideo", "#editVideo", "#outputVideo"]) {
    const video = $(selector);
    if (!video) continue;
    video.pause();
    video.removeAttribute("src");
    video.load();
  }
}

async function confirmDeleteProject(project = state.project) {
  if (!project) return;
  const accepted = window.confirm(`删除项目“${project.name}”？\n\n项目会移到本地回收区，不会立即永久清除。`);
  if (!accepted) return;
  try {
    if (state.project?.id === project.id) {
      if (!(await flushProjectSaves())) return;
      clearInterval(state.jobTimer);
      clearTimeout(state.saveTimer);
      clearTimeout(state.subtitleSaveTimer);
      state.saveTimer = null;
      state.subtitleSaveTimer = null;
      releaseProjectMedia();
    }
    await api(`/api/projects/${project.id}`, { method: "DELETE" });
    if (state.project?.id === project.id) await showWelcome(false);
    else await loadProjects();
    showToast("项目已移到本地回收区");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function openProject(id) {
  try {
    setNativeImportEnabled(false);
    clearTimeout(state.saveTimer);
    clearTimeout(state.subtitleSaveTimer);
    state.saveTimer = null;
    state.subtitleSaveTimer = null;
    const project = await api(`/api/projects/${id}`);
    state.project = project;
    state.deleted = new Set(project.edit?.deletedSegmentIds || []);
    state.deletedWords = new Set(project.edit?.deletedWordIds || []);
    state.deletedTiming = new Set(project.edit?.deletedTimingIds || []);
    state.timingCleanupInitialized = project.edit?.timingCleanupInitialized === true;
    state.subtitleEdits = new Map(Object.entries(project.subtitleEdits || {}));
    state.subtitleSearchQuery = "";
    state.subtitleSearchIndex = -1;
    state.currentSubtitleMatch = null;
    state.transcriptMode = "cut";
    state.editHistory = [];
    state.editFuture = [];
    state.cleanupSuggestions = [];
    state.dismissedCleanup = new Set();
    $("#welcomeView").classList.add("hidden");
    $("#editorView").classList.remove("hidden");
    history.replaceState(null, "", `#project=${id}`);
    renderProjectHeader();
    attachMedia();
    syncSettings();
    syncRecognitionOptions();
    if (project.transcript) {
      state.transcript = await api(`/api/projects/${id}/transcript`);
      const automaticTimingCount = await initializeTimingCleanup();
      showWorkspace();
      if (automaticTimingCount) showToast(`首次已自动清理 ${automaticTimingCount} 处超过 0.5 秒的拖音、气口或停顿，可撤销`);
    } else {
      state.transcript = null;
      showTranscribeStage();
    }
    renderOutputCard();
    if (project.upload) {
      const received = Number(project.upload.received || 0);
      const size = Number(project.upload.size || 0);
      showTask({
        kind: "upload",
        state: "paused",
        progress: size ? received / size * 100 : 0,
        error: `导入已暂停在 ${formatBytes(received)}，重新选择同一个视频即可继续`,
        log: [`已保留 ${formatBytes(received)} / ${formatBytes(size)}`],
      });
    }
    if (["running", "queued", "cancelling"].includes(project.activeJob?.state)) monitorJob(project.activeJob.id);
  } catch (error) {
    if (!state.project) setNativeImportEnabled(true);
    showToast(error.message, true);
  }
}

function renderProjectHeader() {
  const project = state.project;
  $("#projectTitle").textContent = project.name;
  const source = project.source;
  $("#projectMeta").textContent = source ? `${source.name} · ${formatDuration(source.duration, true)} · ${source.width || "?"}×${source.height || "?"} · ${formatBytes(source.size)}` : "等待视频信息";
  $("#saveState").textContent = "已保存";
  $("#transcribeButton").disabled = !state.config?.ready || !source;
  $("#previewButton").disabled = !project.transcript;
  $("#renderButton").disabled = !project.transcript;
}

function attachMedia() {
  if (!state.project?.source) return;
  const url = `/api/projects/${state.project.id}/media`;
  $("#sourceVideo").src = url;
  $("#editVideo").src = url;
  $("#sourceVideo").addEventListener("loadeddata", () => $(".video-placeholder")?.classList.add("hidden"), { once: true });
}

function showTranscribeStage() {
  $("#emptyTranscript").classList.remove("hidden");
  $("#workspace").classList.add("hidden");
}

function showWorkspace() {
  $("#emptyTranscript").classList.add("hidden");
  $("#workspace").classList.remove("hidden");
  state.transcriptMode = "cut";
  $("#workspace").classList.remove("subtitle-mode");
  $("#cutModeButton").classList.add("on");
  $("#cutModeButton").setAttribute("aria-pressed", "true");
  $("#subtitleModeButton").classList.remove("on");
  $("#subtitleModeButton").setAttribute("aria-pressed", "false");
  $("#transcriptTitle").textContent = "剪辑文字";
  $("#transcriptHelp").textContent = "像普通文本一样拖动选择，按 Delete 或 Backspace 删除；选中已删除文字再按一次即可恢复。";
  renderTranscript(false);
  updateSummary();
}

function syncSettings() {
  const edit = state.project?.edit || {};
  $("#pauseThreshold").value = edit.pauseThresholdMs ?? 500;
  $("#leadPadding").value = edit.leadPaddingMs ?? 110;
  $("#tailPadding").value = edit.tailPaddingMs ?? 150;
  $("#wordPadding").value = edit.wordPaddingMs ?? 50;
  const autoPauses = edit.autoRemoveLongPauses !== false;
  $("#autoPauseToggle").classList.toggle("on", autoPauses);
  $("#autoPauseToggle").setAttribute("aria-pressed", String(autoPauses));
  updateSettingLabels();
}

function updateSettingLabels() {
  $("#thresholdOutput").textContent = `${(Number($("#pauseThreshold").value) / 1000).toFixed(2)} 秒`;
  $("#leadOutput").textContent = `${(Number($("#leadPadding").value) / 1000).toFixed(2)} 秒`;
  $("#tailOutput").textContent = `${(Number($("#tailPadding").value) / 1000).toFixed(2)} 秒`;
  $("#wordPaddingOutput").textContent = `${(Number($("#wordPadding").value) / 1000).toFixed(2)} 秒`;
}

function editPayload() {
  return {
    deletedSegmentIds: [...state.deleted],
    deletedWordIds: [...state.deletedWords],
    deletedTimingIds: [...state.deletedTiming],
    timingCleanupInitialized: state.timingCleanupInitialized,
    autoRemoveLongPauses: $("#autoPauseToggle").classList.contains("on"),
    pauseThresholdMs: Number($("#pauseThreshold").value),
    leadPaddingMs: Number($("#leadPadding").value),
    tailPaddingMs: Number($("#tailPadding").value),
    wordPaddingMs: Number($("#wordPadding").value),
  };
}

function scheduleSave() {
  if (!state.project) return;
  $("#saveState").textContent = "保存中…";
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveEdit, 450);
}

function saveEdit() {
  if (!state.project) return Promise.resolve(false);
  clearTimeout(state.saveTimer);
  state.saveTimer = null;
  const projectId = state.project.id;
  const payload = editPayload();
  $("#saveState").textContent = "保存中…";
  const request = state.savePromise.catch(() => false).then(async () => {
    try {
      const updated = await api(`/api/projects/${projectId}/edit`, { method: "POST", body: JSON.stringify(payload) });
      if (state.project?.id === projectId) {
        state.project = updated;
        $("#saveState").textContent = "已保存";
        renderOutputCard(true);
      }
      return true;
    } catch (error) {
      if (state.project?.id === projectId) $("#saveState").textContent = "保存失败";
      showToast(error.message, true);
      return false;
    }
  });
  state.savePromise = request;
  return request;
}

function subtitlePayload() {
  return { edits: Object.fromEntries(state.subtitleEdits) };
}

function updateSubtitleEditStatus() {
  const count = state.subtitleEdits.size;
  $("#subtitleEditStatus").textContent = count ? `已修改 ${count} 条字幕` : "尚未修改字幕";
}

function scheduleSubtitleSave() {
  if (!state.project) return;
  $("#saveState").textContent = "保存中…";
  clearTimeout(state.subtitleSaveTimer);
  state.subtitleSaveTimer = setTimeout(saveSubtitleEdits, 450);
  renderOutputCard(true);
}

function saveSubtitleEdits() {
  if (!state.project) return Promise.resolve(false);
  clearTimeout(state.subtitleSaveTimer);
  state.subtitleSaveTimer = null;
  const projectId = state.project.id;
  const payload = subtitlePayload();
  $("#saveState").textContent = "保存中…";
  const request = state.subtitleSavePromise.catch(() => false).then(async () => {
    try {
      const updated = await api(`/api/projects/${projectId}/subtitles`, { method: "POST", body: JSON.stringify(payload) });
      if (state.project?.id === projectId) {
        state.project = updated;
        $("#saveState").textContent = "已保存";
        renderOutputCard(true);
      }
      return true;
    } catch (error) {
      if (state.project?.id === projectId) $("#saveState").textContent = "保存失败";
      showToast(error.message, true);
      return false;
    }
  });
  state.subtitleSavePromise = request;
  return request;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

function highlightedText(text) {
  let result = escapeHtml(text).replace(/\n/g, "<br>");
  if (!state.fillersVisible) return result;
  const pattern = new RegExp(`(${fillers.sort((a, b) => b.length - a.length).join("|")})`, "g");
  return result.replace(pattern, '<span class="filler-mark">$1</span>');
}

function wordId(segment, index) {
  return `${segment.id}:w${String(index).padStart(4, "0")}`;
}

function cutSubtitleText(segment) {
  const words = segment.words || [];
  if (!words.length) return String(segment.text || "");
  return words
    .filter((_, index) => !state.deletedWords.has(wordId(segment, index)))
    .map((word) => String(word.text || ""))
    .join("")
    .replace(/[ \t]+/g, " ")
    .trim();
}

function subtitleText(segment) {
  return state.subtitleEdits.has(segment.id) ? state.subtitleEdits.get(segment.id) : cutSubtitleText(segment);
}

function activeSubtitleSegments() {
  return state.transcript?.segments.filter((segment) => !state.deleted.has(segment.id)) || [];
}

function subtitleMatches(query = $("#subtitleFindInput")?.value || "") {
  if (!query) return [];
  const needle = query.toLocaleLowerCase();
  const matches = [];
  for (const segment of activeSubtitleSegments()) {
    const text = subtitleText(segment);
    const searchable = text.toLocaleLowerCase();
    let start = 0;
    while (start <= searchable.length - needle.length) {
      const index = searchable.indexOf(needle, start);
      if (index < 0) break;
      matches.push({ segmentId: segment.id, start: index, length: query.length });
      start = index + Math.max(1, needle.length);
    }
  }
  return matches;
}

function updateSubtitleFindStatus() {
  const status = $("#subtitleFindStatus");
  if (!status) return;
  const query = $("#subtitleFindInput")?.value || "";
  if (!query) {
    status.textContent = "输入文字开始查找；Ctrl+F 可直接定位这里";
    return;
  }
  const matches = subtitleMatches(query);
  const current = state.currentSubtitleMatch && state.subtitleSearchQuery === query
    ? Math.max(0, matches.findIndex((match) => match.segmentId === state.currentSubtitleMatch.segmentId && match.start === state.currentSubtitleMatch.start)) + 1
    : 0;
  status.textContent = matches.length ? `找到 ${matches.length} 处${current ? ` · 当前第 ${current} 处` : ""}` : "没有找到匹配字幕";
}

function selectSubtitleMatch(match) {
  const editor = $(`.segment[data-id="${match.segmentId}"] .subtitle-editor`);
  if (!editor) return false;
  const node = editor.firstChild;
  if (!node || node.nodeType !== Node.TEXT_NODE) return false;
  editor.focus({ preventScroll: true });
  const selection = window.getSelection();
  const range = document.createRange();
  range.setStart(node, Math.min(match.start, node.textContent.length));
  range.setEnd(node, Math.min(match.start + match.length, node.textContent.length));
  selection.removeAllRanges();
  selection.addRange(range);
  editor.closest(".segment").scrollIntoView({ behavior: "smooth", block: "center" });
  return true;
}

function findNextSubtitle() {
  const query = $("#subtitleFindInput").value;
  if (!query) {
    $("#subtitleFindInput").focus();
    return showToast("请先输入要查找的字幕", true);
  }
  const matches = subtitleMatches(query);
  if (!matches.length) {
    state.currentSubtitleMatch = null;
    updateSubtitleFindStatus();
    return showToast("没有找到匹配字幕", true);
  }
  if (state.subtitleSearchQuery !== query) state.subtitleSearchIndex = -1;
  state.subtitleSearchQuery = query;
  state.subtitleSearchIndex = (state.subtitleSearchIndex + 1) % matches.length;
  state.currentSubtitleMatch = matches[state.subtitleSearchIndex];
  selectSubtitleMatch(state.currentSubtitleMatch);
  updateSubtitleFindStatus();
}

function replaceLiteral(text, query, replacement, firstOnly = false) {
  if (!query) return { text, count: 0 };
  const searchable = text.toLocaleLowerCase();
  const needle = query.toLocaleLowerCase();
  let cursor = 0;
  let count = 0;
  let result = "";
  while (cursor <= text.length - query.length) {
    const index = searchable.indexOf(needle, cursor);
    if (index < 0) break;
    result += text.slice(cursor, index) + replacement;
    cursor = index + query.length;
    count += 1;
    if (firstOnly) break;
  }
  if (!count) return { text, count: 0 };
  return { text: result + text.slice(cursor), count };
}

function storeSubtitleText(segment, value) {
  const base = cutSubtitleText(segment);
  if (value === base) state.subtitleEdits.delete(segment.id); else state.subtitleEdits.set(segment.id, value);
}

function replaceCurrentSubtitle() {
  const query = $("#subtitleFindInput").value;
  const replacement = $("#subtitleReplaceInput").value;
  if (!query) return showToast("请先输入要查找的字幕", true);
  let match = state.currentSubtitleMatch;
  if (!match || state.subtitleSearchQuery !== query) {
    findNextSubtitle();
    match = state.currentSubtitleMatch;
  }
  if (!match) return;
  const segment = activeSubtitleSegments().find((item) => item.id === match.segmentId);
  if (!segment) return findNextSubtitle();
  const original = subtitleText(segment);
  const selected = original.slice(match.start, match.start + match.length);
  if (selected.toLocaleLowerCase() !== query.toLocaleLowerCase()) return findNextSubtitle();
  const updated = original.slice(0, match.start) + replacement + original.slice(match.start + match.length);
  storeSubtitleText(segment, updated);
  state.currentSubtitleMatch = null;
  renderTranscript();
  updateSubtitleEditStatus();
  scheduleSubtitleSave();
  showToast("已替换当前字幕");
}

function replaceAllSubtitles() {
  const query = $("#subtitleFindInput").value;
  const replacement = $("#subtitleReplaceInput").value;
  if (!query) return showToast("请先输入要查找的字幕", true);
  const total = subtitleMatches(query).length;
  if (!total) return showToast("没有找到匹配字幕", true);
  if (!window.confirm(`把 ${total} 处“${query}”全部替换为“${replacement}”？\n\n只修改字幕，不改变声音和画面。`)) return;
  for (const segment of activeSubtitleSegments()) {
    const replaced = replaceLiteral(subtitleText(segment), query, replacement);
    if (replaced.count) storeSubtitleText(segment, replaced.text);
  }
  state.currentSubtitleMatch = null;
  state.subtitleSearchIndex = -1;
  renderTranscript();
  updateSubtitleEditStatus();
  scheduleSubtitleSave();
  showToast(`已替换 ${total} 处字幕`);
}

function normalizedWordText(text) {
  return String(text || "").toLowerCase().replace(/[\s，。！？、；：,.!?;:…—~～“”‘’（）()《》【】]/g, "");
}

function createTimingMarker(kind, seconds, extraClass = "", timingId = null) {
  const marker = document.createElement(timingId ? "button" : "span");
  const labels = { breath: "气", pause: "停", stretch: "拖" };
  const names = { breath: "气口", pause: "停顿", stretch: "拖音尾部" };
  const removed = timingId && state.deletedTiming.has(timingId);
  marker.className = `timing-marker ${kind}${timingId ? " actionable" : ""}${removed ? " removed" : ""}${extraClass ? ` ${extraClass}` : ""}`;
  marker.textContent = `${labels[kind]} ${Number(seconds).toFixed(2)}s`;
  if (timingId) {
    marker.type = "button";
    marker.dataset.timingId = timingId;
    marker.dataset.timingKind = kind;
    marker.dataset.timingSeconds = String(seconds);
    marker.setAttribute("aria-label", `${removed ? "恢复" : "清除"}${names[kind]} ${Number(seconds).toFixed(2)} 秒`);
    marker.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleTimingCut(timingId);
    });
  } else {
    marker.setAttribute("aria-hidden", "true");
  }
  marker.title = `${kind === "stretch" ? "字音持续" : names[kind]}约 ${Number(seconds).toFixed(2)} 秒；${timingId ? "点击可直接清除或恢复" : ""}`;
  return marker;
}

function shouldMarkStretch(word) {
  const clean = normalizedWordText(word.text);
  const duration = Number(word.end) - Number(word.start);
  const expressive = /[啊嗯呃哦喂哎诶呀哈啦嘛吧呢]/.test(clean);
  return duration > .5 && (clean.length <= 2 || expressive);
}

function appendGapMarker(container, gap, timingId = null) {
  if (!Number.isFinite(gap) || gap < .18) return;
  const threshold = Number($("#pauseThreshold")?.value || 500) / 1000;
  container.appendChild(createTimingMarker(gap >= threshold ? "pause" : "breath", gap, "", timingId));
}

function timingMarkerData() {
  if (!state.transcript) return [];
  const threshold = Number($("#pauseThreshold")?.value || 500) / 1000;
  const markers = [];
  const segments = state.transcript.segments || [];
  const addGap = (id, seconds) => {
    if (!Number.isFinite(seconds) || seconds < .18) return;
    markers.push({ id, kind: seconds >= threshold ? "pause" : "breath", seconds });
  };
  for (const [segmentIndex, segment] of segments.entries()) {
    const words = segment.words || [];
    for (const [wordIndex, word] of words.entries()) {
      const id = wordId(segment, wordIndex);
      const duration = Number(word.end) - Number(word.start);
      if (shouldMarkStretch(word)) markers.push({ id: `stretch:${id}`, kind: "stretch", seconds: duration });
      const nextWord = words[wordIndex + 1];
      if (nextWord) addGap(`gap:${id}`, Number(nextWord.start) - Number(word.end));
    }
    const nextSegment = segments[segmentIndex + 1];
    if (nextSegment) {
      const left = Number(words.at(-1)?.end ?? segment.end);
      const right = Number(nextSegment.words?.[0]?.start ?? nextSegment.start);
      addGap(`gap:${segment.id}:end`, right - left);
    }
  }
  return markers;
}

async function initializeTimingCleanup() {
  if (!state.transcript || state.timingCleanupInitialized) return 0;
  if (Number($("#pauseThreshold").value) > 500) {
    $("#pauseThreshold").value = 500;
    updateSettingLabels();
  }
  const ids = timingMarkerData()
    .filter((marker) => marker.seconds > .5 && !state.deletedTiming.has(marker.id))
    .map((marker) => marker.id);
  if (ids.length) {
    pushEditHistory();
    ids.forEach((id) => state.deletedTiming.add(id));
  }
  state.timingCleanupInitialized = true;
  state.project = await api(`/api/projects/${state.project.id}/edit`, { method: "POST", body: JSON.stringify(editPayload()) });
  return ids.length;
}

function updateTimingBatchStatus() {
  if (!state.transcript) return;
  const remaining = timingMarkerData().filter((marker) => !state.deletedTiming.has(marker.id));
  const counts = Object.fromEntries(["stretch", "breath", "pause"].map((kind) => [kind, remaining.filter((marker) => marker.kind === kind).length]));
  $("#timingBatchStatus").textContent = `待处理：拖 ${counts.stretch} · 气 ${counts.breath} · 停 ${counts.pause}`;
  $("#removeAllTimingButton").disabled = !remaining.length;
  $("#removeAllStretchButton").disabled = !counts.stretch;
  $("#removeAllBreathButton").disabled = !counts.breath;
  $("#removeAllPauseButton").disabled = !counts.pause;
}

function removeAllTiming(kind = "all") {
  const labels = { all: "拖音、气口和停顿", stretch: "拖音", breath: "气口", pause: "停顿" };
  const ids = timingMarkerData()
    .filter((marker) => (kind === "all" || marker.kind === kind) && !state.deletedTiming.has(marker.id))
    .map((marker) => marker.id);
  if (!ids.length) return showToast(`当前没有待清理的${labels[kind]}`);
  pushEditHistory();
  ids.forEach((id) => state.deletedTiming.add(id));
  renderTranscript();
  updateSummary();
  scheduleSave();
  refreshCleanupSuggestions();
  showToast(`已一键清理 ${ids.length} 处${labels[kind]}，可撤销`);
}

function cleanupSuggestion(type, segment, wordIds, text, reason, start, timingIds = []) {
  const id = `${type}:${wordIds.join("+")}`;
  return { id, type, segmentId: segment.id, wordIds, timingIds, text, reason, start: Number(start ?? segment.start) };
}

function detectCleanupSuggestions() {
  if (!state.transcript) return [];
  const suggestions = [];
  const usedRepeatIds = new Set();
  const segments = state.transcript.segments;

  for (const segment of segments) {
    if (state.deleted.has(segment.id)) continue;
    const words = segment.words || [];
    const active = words.map((word, index) => ({ word, index, id: wordId(segment, index), clean: normalizedWordText(word.text) }));

    for (const entry of active) {
      if (state.deletedWords.has(entry.id) || !entry.clean) continue;
      const duration = Number(entry.word.end) - Number(entry.word.start);
      const expressive = /[啊嗯呃哦喂哎诶呀哈啦嘛吧呢]/.test(entry.clean);
      const visiblyStretched = /[…—~～]{2,}/.test(String(entry.word.text));
      if ((duration >= .78 && entry.clean.length <= 2) || (duration >= .6 && expressive) || visiblyStretched) {
        const timingId = `stretch:${entry.id}`;
        if (!state.deletedTiming.has(timingId)) suggestions.push(cleanupSuggestion("stretch", segment, [entry.id], String(entry.word.text).trim(), `单词持续 ${duration.toFixed(2)} 秒`, entry.word.start, [timingId]));
      }
    }

    // Prefer the longest repeated phrase first, e.g. “我觉得 / 我觉得”.
    for (let size = 4; size >= 2; size -= 1) {
      for (let start = 0; start + size * 2 <= active.length; start += 1) {
        const left = active.slice(start, start + size);
        const right = active.slice(start + size, start + size * 2);
        const allActive = [...left, ...right].every((entry) => entry.clean && !state.deletedWords.has(entry.id));
        if (!allActive || left.map((entry) => entry.clean).join("") !== right.map((entry) => entry.clean).join("")) continue;
        if (right.some((entry) => usedRepeatIds.has(entry.id))) continue;
        right.forEach((entry) => usedRepeatIds.add(entry.id));
        suggestions.push(cleanupSuggestion("repeat", segment, right.map((entry) => entry.id), right.map((entry) => entry.word.text).join(""), `短句连续重复，建议试听第二遍`, right[0].word.start));
      }
    }

    for (let index = 1; index < active.length; index += 1) {
      const previous = active[index - 1];
      const current = active[index];
      if (!current.clean || current.clean !== previous.clean) continue;
      if (state.deletedWords.has(previous.id) || state.deletedWords.has(current.id) || usedRepeatIds.has(current.id)) continue;
      usedRepeatIds.add(current.id);
      suggestions.push(cleanupSuggestion("repeat", segment, [current.id], String(current.word.text).trim(), `与前一个“${String(previous.word.text).trim()}”重复`, current.word.start));
    }
  }

  let previousSegment = null;
  for (const segment of segments) {
    if (state.deleted.has(segment.id)) { previousSegment = null; continue; }
    const clean = normalizedWordText(segment.text);
    if (previousSegment && clean.length >= 2 && clean.length <= 24 && clean === previousSegment.clean && Number(segment.start) - Number(previousSegment.segment.end) <= 4) {
      const ids = (segment.words || []).map((_, index) => wordId(segment, index)).filter((id) => !state.deletedWords.has(id));
      if (ids.length) suggestions.push(cleanupSuggestion("repeat", segment, ids, segment.text, "与上一句内容相同", segment.start));
    }
    previousSegment = { segment, clean };
  }

  return suggestions
    .filter((suggestion) => !state.dismissedCleanup.has(suggestion.id))
    .sort((left, right) => left.start - right.start || left.type.localeCompare(right.type));
}

function renderCleanupSuggestions() {
  const list = $("#cleanupList");
  list.innerHTML = "";
  const suggestions = state.cleanupSuggestions;
  const stretchCount = suggestions.filter((item) => item.type === "stretch").length;
  const repeatCount = suggestions.length - stretchCount;
  $("#cleanupSummary").textContent = `拖音 ${stretchCount} 处 · 重复 ${repeatCount} 处`;
  if (!suggestions.length) {
    list.innerHTML = '<div class="cleanup-empty">当前保留内容里没有发现明显的拖音或重复。</div>';
    return;
  }
  for (const suggestion of suggestions.slice(0, 120)) {
    const row = document.createElement("div");
    row.className = "cleanup-suggestion";
    const locate = document.createElement("button");
    locate.type = "button";
    locate.className = "cleanup-locate";
    locate.innerHTML = `<span class="cleanup-type ${suggestion.type}">${suggestion.type === "stretch" ? "拖音" : "重复"}</span><strong></strong><small></small>`;
    locate.querySelector("strong").textContent = `“${suggestion.text || "未识别文字"}”`;
    locate.querySelector("small").textContent = `${formatDuration(suggestion.start, true)} · ${suggestion.reason}`;
    locate.addEventListener("click", () => locateCleanupSuggestion(suggestion));
    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "cleanup-apply";
    apply.textContent = "应用";
    apply.addEventListener("click", () => applyCleanupSuggestion(suggestion));
    const ignore = document.createElement("button");
    ignore.type = "button";
    ignore.className = "cleanup-ignore";
    ignore.textContent = "忽略";
    ignore.addEventListener("click", () => {
      state.dismissedCleanup.add(suggestion.id);
      state.cleanupSuggestions = state.cleanupSuggestions.filter((item) => item.id !== suggestion.id);
      renderCleanupSuggestions();
    });
    row.append(locate, apply, ignore);
    list.appendChild(row);
  }
}

function refreshCleanupSuggestions() {
  if ($("#cleanupPanel").classList.contains("hidden")) return;
  state.cleanupSuggestions = detectCleanupSuggestions();
  renderCleanupSuggestions();
}

function scanCleanupSuggestions() {
  state.cleanupSuggestions = detectCleanupSuggestions();
  $("#cleanupPanel").classList.remove("hidden");
  renderCleanupSuggestions();
  showToast(`发现 ${state.cleanupSuggestions.length} 条清理建议`);
}

function applyCleanupSuggestion(suggestion, recordHistory = true, refresh = true) {
  if (recordHistory) pushEditHistory();
  if (suggestion.type === "stretch") {
    (suggestion.timingIds || []).forEach((id) => state.deletedTiming.add(id));
  } else {
    (suggestion.wordIds || []).forEach((id) => state.deletedWords.add(id));
  }
  if (!refresh) return;
  renderTranscript();
  updateSummary();
  scheduleSave();
  refreshCleanupSuggestions();
  showToast(suggestion.type === "stretch" ? "已收紧这处拖音尾部" : "已删除这处重复内容");
}

function applyAllCleanupSuggestions() {
  const suggestions = [...state.cleanupSuggestions];
  if (!suggestions.length) return showToast("当前没有可应用的清理建议");
  const stretchCount = suggestions.filter((item) => item.type === "stretch").length;
  const repeatCount = suggestions.length - stretchCount;
  if (!window.confirm(`一键应用 ${suggestions.length} 条建议？\n\n重复 ${repeatCount} 处：删除第二遍文字\n拖音 ${stretchCount} 处：只收紧估算尾部，不删除文字\n\n完成后可以用“撤销”一次恢复。`)) return;
  pushEditHistory();
  suggestions.forEach((suggestion) => applyCleanupSuggestion(suggestion, false, false));
  renderTranscript();
  updateSummary();
  scheduleSave();
  refreshCleanupSuggestions();
  showToast(`已应用 ${suggestions.length} 条建议，可一次撤销`);
}

function locateCleanupSuggestion(suggestion) {
  const tokens = suggestion.wordIds.map((id) => $(`[data-word-id="${id}"]`)).filter(Boolean);
  if (!tokens.length) return showToast("这处文字已经删除或不在当前字幕中", true);
  $$(".word-token.suggestion-focus").forEach((node) => node.classList.remove("suggestion-focus"));
  tokens.forEach((node) => node.classList.add("suggestion-focus"));
  const selection = window.getSelection();
  const range = document.createRange();
  range.setStart(tokens[0].firstChild, 0);
  range.setEnd(tokens.at(-1).firstChild, tokens.at(-1).textContent.length);
  selection.removeAllRanges();
  selection.addRange(range);
  tokens[0].closest(".segment").scrollIntoView({ behavior: "smooth", block: "center" });
  const video = $("#editVideo");
  video.currentTime = auditionStart(Math.max(0, suggestion.start - .15));
  video.play().catch(() => {});
  refreshSelectionState();
}

function allWordIds() {
  return state.transcript?.segments.flatMap((segment) => (segment.words || []).map((_, index) => wordId(segment, index))) || [];
}

function updateWordEditStatus(selected = state.selectedWords.size) {
  const count = state.deletedWords.size;
  const timingCount = state.deletedTiming.size;
  $("#wordEditStatus").textContent = selected ? `已选 ${selected} 词 · 按 Delete` : count || timingCount ? `已删 ${count} 词 · 已清理 ${timingCount} 处时间` : "选择文字后按 Delete";
  $("#undoEditButton").disabled = state.editHistory.length === 0;
}

function editSnapshot() {
  return { deleted: [...state.deleted], deletedWords: [...state.deletedWords], deletedTiming: [...state.deletedTiming] };
}

function pushEditHistory() {
  state.editHistory.push(editSnapshot());
  if (state.editHistory.length > 100) state.editHistory.shift();
  state.editFuture = [];
  $("#undoEditButton").disabled = false;
}

function restoreEditSnapshot(snapshot) {
  state.deleted = new Set(snapshot.deleted);
  state.deletedWords = new Set(snapshot.deletedWords);
  state.deletedTiming = new Set(snapshot.deletedTiming || []);
  renderTranscript();
  updateSummary();
  scheduleSave();
  refreshCleanupSuggestions();
}

function undoEdit() {
  const snapshot = state.editHistory.pop();
  if (!snapshot) return;
  state.editFuture.push(editSnapshot());
  restoreEditSnapshot(snapshot);
  showToast("已撤销上一步剪辑");
}

function redoEdit() {
  const snapshot = state.editFuture.pop();
  if (!snapshot) return;
  state.editHistory.push(editSnapshot());
  restoreEditSnapshot(snapshot);
  showToast("已重做剪辑");
}

function closestWordToken(node) {
  const element = node?.nodeType === Node.TEXT_NODE ? node.parentElement : node;
  return element?.closest?.(".word-token") || null;
}

function boundaryWordToken(container, offset, usePrevious) {
  const direct = closestWordToken(container);
  if (direct) return direct;
  if (container?.nodeType !== Node.ELEMENT_NODE || !container.childNodes.length) return null;
  const index = usePrevious ? Math.max(0, offset - 1) : Math.min(offset, container.childNodes.length - 1);
  const child = container.childNodes[index];
  const element = child?.nodeType === Node.TEXT_NODE ? child.parentElement : child;
  if (!element) return null;
  return closestWordToken(element) || (usePrevious ? element.querySelector?.(".word-token:last-of-type") : element.querySelector?.(".word-token")) || null;
}

function selectedWordNodes() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return [];
  const range = selection.getRangeAt(0);
  const list = $("#transcriptList");
  try { if (!list || !range.intersectsNode(list)) return []; } catch { return []; }
  const start = boundaryWordToken(range.startContainer, range.startOffset, false);
  const end = boundaryWordToken(range.endContainer, range.endOffset, true);
  if (start && end) {
    const startIndex = Number(start.dataset.wordOrder);
    const endIndex = Number(end.dataset.wordOrder);
    if (Number.isInteger(startIndex) && Number.isInteger(endIndex)) {
      return state.wordNodes.slice(Math.min(startIndex, endIndex), Math.max(startIndex, endIndex) + 1);
    }
  }
  return state.wordNodes.filter((node) => {
    try { return range.intersectsNode(node); } catch { return false; }
  });
}

function refreshSelectionState() {
  const selected = new Set(selectedWordNodes());
  state.selectedWords.forEach((node) => node.classList.remove("selection-target"));
  selected.forEach((node) => node.classList.add("selection-target"));
  state.selectedWords = selected;
  updateWordEditStatus(selected.size);
}

function refreshSegmentWordState(segmentId) {
  const item = $(`.segment[data-id="${segmentId}"]`);
  if (!item) return;
  item.classList.toggle("has-word-edits", !!item.querySelector(".word-token.deleted"));
}

function applyWordDeletion(id, deleted, shouldSave = true, shouldRefresh = true) {
  const alreadyDeleted = state.deletedWords.has(id);
  if (alreadyDeleted === deleted) return;
  if (deleted) state.deletedWords.add(id); else state.deletedWords.delete(id);
  const token = $(`[data-word-id="${id}"]`);
  if (token) {
    token.classList.toggle("deleted", deleted);
    refreshSegmentWordState(token.closest(".segment").dataset.id);
  }
  if (shouldRefresh) {
    updateWordEditStatus();
    cancelAnimationFrame(applyWordDeletion.summaryFrame);
    applyWordDeletion.summaryFrame = requestAnimationFrame(updateSummary);
  }
  if (shouldSave) scheduleSave();
}

function editSelectedWords() {
  if (state.restartRequired) return showToast("请关闭启动窗口并重新双击启动器，再使用逐词剪辑", true);
  const tokens = selectedWordNodes();
  if (!tokens.length) return false;
  const ids = [...new Set(tokens.map((token) => token.dataset.wordId))];
  const targetDeleted = !ids.every((id) => state.deletedWords.has(id));
  pushEditHistory();
  ids.forEach((id) => applyWordDeletion(id, targetDeleted, false, false));
  updateWordEditStatus();
  updateSummary();
  scheduleSave();
  refreshCleanupSuggestions();
  showToast(targetDeleted ? `已删除 ${ids.length} 个词` : `已恢复 ${ids.length} 个词`);
  return true;
}

function toggleTimingCut(timingId) {
  pushEditHistory();
  if (state.deletedTiming.has(timingId)) state.deletedTiming.delete(timingId); else state.deletedTiming.add(timingId);
  const removed = state.deletedTiming.has(timingId);
  $$(`[data-timing-id="${CSS.escape(timingId)}"]`).forEach((marker) => {
    const kind = marker.dataset.timingKind;
    const name = { breath: "气口", pause: "停顿", stretch: "拖音尾部" }[kind];
    const seconds = Number(marker.dataset.timingSeconds || 0);
    marker.classList.toggle("removed", removed);
    marker.setAttribute("aria-label", `${removed ? "恢复" : "清除"}${name} ${seconds.toFixed(2)} 秒`);
  });
  updateSummary();
  scheduleSave();
  refreshCleanupSuggestions();
  showToast(removed ? "已清除这段气口或拖音，可撤销" : "已恢复这段时间");
}

function currentTranscriptAnchor() {
  const toolbarBottom = $(".transcript-toolbar")?.getBoundingClientRect().bottom || 0;
  const item = $$(".segment").find((node) => node.getBoundingClientRect().bottom > toolbarBottom + 4);
  return item ? { id: item.dataset.id, top: item.getBoundingClientRect().top } : null;
}

function restoreTranscriptAnchor(anchor) {
  if (!anchor) return;
  const item = $(`.segment[data-id="${anchor.id}"]`);
  if (item) window.scrollBy(0, item.getBoundingClientRect().top - anchor.top);
}

function renderTranscript(preserveScroll = true) {
  const anchor = preserveScroll ? currentTranscriptAnchor() : null;
  const list = $("#transcriptList");
  list.innerHTML = "";
  state.wordNodes = [];
  state.selectedWords = new Set();
  const segments = state.transcript.segments;
  for (const [segmentIndex, segment] of segments.entries()) {
    const deleted = state.deleted.has(segment.id);
    if (state.transcriptMode === "subtitle" && deleted) continue;
    const hasWordEdits = (segment.words || []).some((_, index) => state.deletedWords.has(wordId(segment, index)));
    const subtitleMode = state.transcriptMode === "subtitle";
    const subtitleCustom = state.subtitleEdits.has(segment.id);
    const item = document.createElement("article");
    item.className = `segment${deleted ? " deleted" : ""}${hasWordEdits ? " has-word-edits" : ""}${subtitleMode ? " subtitle-row" : ""}${subtitleCustom ? " subtitle-custom" : ""}`;
    item.dataset.id = segment.id;
    item.dataset.start = segment.start;
    item.dataset.end = segment.end;
    item.innerHTML = `<div class="segment-copy"><p></p><small>${formatDuration(segment.start, true)} → ${formatDuration(segment.end, true)} · ${(segment.end - segment.start).toFixed(1)} 秒${deleted && subtitleMode ? " · 视频已删除" : ""}</small></div>${subtitleMode ? `<button class="subtitle-reset" type="button" title="恢复为剪辑后的识别文字"${subtitleCustom ? "" : " disabled"}>原</button>` : `<button class="switch ${deleted ? "" : "on"}" aria-label="${deleted ? "恢复" : "删除"}这句话"><i></i></button>`}`;
    const paragraph = item.querySelector(".segment-copy p");
    if (subtitleMode) {
      paragraph.className = "subtitle-editor";
      paragraph.contentEditable = deleted ? "false" : "plaintext-only";
      paragraph.spellcheck = true;
      paragraph.textContent = subtitleText(segment);
      paragraph.setAttribute("aria-label", `修改第 ${segmentIndex + 1} 条字幕`);
      paragraph.addEventListener("input", () => {
        const value = paragraph.innerText.replace(/\r/g, "").replace(/\n$/, "");
        const base = cutSubtitleText(segment);
        if (value === base) state.subtitleEdits.delete(segment.id); else state.subtitleEdits.set(segment.id, value);
        const custom = state.subtitleEdits.has(segment.id);
        item.classList.toggle("subtitle-custom", custom);
        item.querySelector(".subtitle-reset").disabled = !custom;
        updateSubtitleEditStatus();
        state.currentSubtitleMatch = null;
        updateSubtitleFindStatus();
        scheduleSubtitleSave();
      });
      paragraph.addEventListener("blur", () => {
        if (state.subtitleSaveTimer) saveSubtitleEdits();
      });
      item.querySelector(".subtitle-reset").addEventListener("click", () => {
        state.subtitleEdits.delete(segment.id);
        paragraph.textContent = cutSubtitleText(segment);
        item.classList.remove("subtitle-custom");
        item.querySelector(".subtitle-reset").disabled = true;
        updateSubtitleEditStatus();
        scheduleSubtitleSave();
        paragraph.focus();
      });
    } else if (segment.words?.length) {
      segment.words.forEach((word, index) => {
        const id = wordId(segment, index);
        const wordDeleted = state.deletedWords.has(id);
        const token = document.createElement("span");
        token.className = `word-token${wordDeleted ? " deleted" : ""}${state.fillersVisible && fillers.some((filler) => String(word.text).includes(filler)) ? " filler-mark" : ""}`;
        token.dataset.wordId = id;
        token.dataset.wordOrder = state.wordNodes.length;
        token.dataset.start = word.start;
        token.textContent = word.text;
        token.title = `${Number(word.start).toFixed(2)}–${Number(word.end).toFixed(2)} 秒`;
        paragraph.appendChild(token);
        state.wordNodes.push(token);
        if (shouldMarkStretch(word)) paragraph.appendChild(createTimingMarker("stretch", Number(word.end) - Number(word.start), "", `stretch:${id}`));
        const nextWord = segment.words[index + 1];
        if (nextWord) appendGapMarker(paragraph, Number(nextWord.start) - Number(word.end), `gap:${id}`);
      });
    } else {
      paragraph.innerHTML = highlightedText(segment.text);
    }
    const nextSegment = segments[segmentIndex + 1];
    if (!subtitleMode && nextSegment) {
      const nextStart = Number(nextSegment.words?.[0]?.start ?? nextSegment.start);
      const currentEnd = Number(segment.words?.at(-1)?.end ?? segment.end);
      const gapHost = item.querySelector(".segment-copy small");
      const before = gapHost.childNodes.length;
      appendGapMarker(gapHost, nextStart - currentEnd, `gap:${segment.id}:end`);
      if (gapHost.childNodes.length > before) gapHost.lastChild.classList.add("segment-gap");
    }
    if (!subtitleMode) item.addEventListener("click", (event) => {
      if (event.target.closest(".switch") || window.getSelection()?.toString()) return;
      const video = $("#editVideo");
      video.currentTime = auditionStart(Number(event.target.closest(".word-token")?.dataset.start ?? segment.start));
      video.play().catch(() => {});
      $$(".segment.active").forEach((node) => node.classList.remove("active"));
      item.classList.add("active");
    });
    if (!subtitleMode) item.querySelector(".switch").addEventListener("click", () => toggleSegment(segment.id));
    list.appendChild(item);
  }
  updateWordEditStatus();
  updateSubtitleEditStatus();
  updateTimingBatchStatus();
  updateSubtitleFindStatus();
  restoreTranscriptAnchor(anchor);
}

function setTranscriptMode(mode) {
  if (!state.transcript || !["cut", "subtitle"].includes(mode) || state.transcriptMode === mode) return;
  state.transcriptMode = mode;
  const subtitleMode = mode === "subtitle";
  $("#workspace").classList.toggle("subtitle-mode", subtitleMode);
  $("#cutModeButton").classList.toggle("on", !subtitleMode);
  $("#cutModeButton").setAttribute("aria-pressed", String(!subtitleMode));
  $("#subtitleModeButton").classList.toggle("on", subtitleMode);
  $("#subtitleModeButton").setAttribute("aria-pressed", String(subtitleMode));
  $("#transcriptTitle").textContent = subtitleMode ? "修改字幕" : "剪辑文字";
  $("#transcriptHelp").textContent = subtitleMode
    ? "直接修改错字、标点和表达；这里只改变字幕，不会删除声音或画面。"
    : "像普通文本一样拖动选择，按 Delete 或 Backspace 删除；选中已删除文字再按一次即可恢复。";
  if (subtitleMode) $("#cleanupPanel").classList.add("hidden");
  state.currentSubtitleMatch = null;
  state.subtitleSearchIndex = -1;
  window.getSelection()?.removeAllRanges();
  renderTranscript(true);
}

function toggleSegment(id) {
  pushEditHistory();
  if (state.deleted.has(id)) state.deleted.delete(id); else state.deleted.add(id);
  const item = $(`.segment[data-id="${id}"]`);
  const deleted = state.deleted.has(id);
  item.classList.toggle("deleted", deleted);
  item.querySelector(".switch").classList.toggle("on", !deleted);
  item.querySelector(".switch").setAttribute("aria-label", `${deleted ? "恢复" : "删除"}这句话`);
  updateSummary();
  scheduleSave();
  refreshCleanupSuggestions();
}

function clientTimingCutRanges() {
  if (!state.transcript || !state.deletedTiming.size) return [];
  const segments = state.transcript.segments;
  const words = new Map();
  const segmentMap = new Map(segments.map((segment, index) => [segment.id, { segment, index }]));
  for (const segment of segments) {
    (segment.words || []).forEach((word, index, list) => words.set(wordId(segment, index), { word, nextWord: list[index + 1] }));
  }
  const ranges = [];
  for (const timingId of state.deletedTiming) {
    const separator = timingId.indexOf(":");
    const kind = timingId.slice(0, separator);
    const reference = timingId.slice(separator + 1);
    if (kind === "stretch" && words.has(reference)) {
      const { word } = words.get(reference);
      const start = Number(word.start), end = Number(word.end), duration = end - start;
      if (duration > .5) ranges.push([start + Math.min(.48, Math.max(.24, duration * .45)), end]);
    } else if (kind === "gap" && reference.endsWith(":end")) {
      const entry = segmentMap.get(reference.slice(0, -4));
      if (!entry || entry.index + 1 >= segments.length) continue;
      const nextSegment = segments[entry.index + 1];
      const left = Number(entry.segment.words?.at(-1)?.end ?? entry.segment.end);
      const right = Number(nextSegment.words?.[0]?.start ?? nextSegment.start);
      if (right > left) ranges.push([left, right]);
    } else if (kind === "gap" && words.has(reference)) {
      const { word, nextWord } = words.get(reference);
      if (nextWord && Number(nextWord.start) > Number(word.end)) ranges.push([Number(word.end), Number(nextWord.start)]);
    }
  }
  return ranges.sort((a, b) => a[0] - b[0]);
}

function applyClientTimingCuts(clips) {
  let pieces = clips.map((clip) => ({ ...clip }));
  for (const [cutStart, cutEnd] of clientTimingCutRanges()) {
    const next = [];
    for (const clip of pieces) {
      if (cutEnd <= clip.start || cutStart >= clip.end) next.push(clip);
      else {
        if (cutStart > clip.start) next.push({ start: clip.start, end: Math.min(cutStart, clip.end) });
        if (cutEnd < clip.end) next.push({ start: Math.max(cutEnd, clip.start), end: clip.end });
      }
    }
    pieces = next.filter((clip) => clip.end - clip.start > .015);
  }
  return pieces;
}

function computeClientClips() {
  if (!state.transcript) return [];
  const edit = editPayload();
  const threshold = edit.autoRemoveLongPauses ? edit.pauseThresholdMs / 1000 : Infinity;
  const lead = edit.leadPaddingMs / 1000;
  const tail = edit.tailPaddingMs / 1000;
  const duration = state.project.source?.duration ?? Infinity;
  if (!state.deletedWords.size) {
    const clips = [];
    let hardBreak = true;
    for (const segment of state.transcript.segments) {
      if (state.deleted.has(segment.id)) {
        hardBreak = true;
        continue;
      }
      const start = Math.max(0, segment.start - lead);
      const end = Math.min(duration, segment.end + tail);
      const previous = clips.at(-1);
      if (previous && !hardBreak && start - previous.end <= threshold) previous.end = Math.max(previous.end, end);
      else clips.push({ start, end });
      hardBreak = false;
    }
    return applyClientTimingCuts(clips);
  }

  const wordPadding = edit.wordPaddingMs / 1000;
  const units = [];
  let hardBreakBefore = true;
  for (const segment of state.transcript.segments) {
    if (state.deleted.has(segment.id)) {
      hardBreakBefore = true;
      continue;
    }
    const words = segment.words || [];
    if (!words.length) {
      units.push({ start: Math.max(0, segment.start - lead), end: Math.min(duration, segment.end + tail), hardBreak: hardBreakBefore });
      hardBreakBefore = false;
      continue;
    }
    let runStart = null;
    let runEnd = null;
    let runHardBreak = hardBreakBefore;
    let previousDeleted = null;
    words.forEach((word, index) => {
      const id = wordId(segment, index);
      if (state.deletedWords.has(id)) {
        if (runStart !== null) {
          units.push({ start: runStart, end: Math.min(duration, Math.min(runEnd + wordPadding, word.start + .015)), hardBreak: runHardBreak });
          runStart = runEnd = null;
        }
        hardBreakBefore = true;
        previousDeleted = word;
        return;
      }
      if (runStart === null) {
        runStart = Math.max(0, word.start - wordPadding);
        if (previousDeleted) runStart = Math.max(runStart, previousDeleted.end - .015);
        runEnd = word.end;
        runHardBreak = hardBreakBefore;
        hardBreakBefore = false;
        previousDeleted = null;
      } else if (word.start - runEnd > threshold) {
        units.push({ start: runStart, end: Math.min(duration, runEnd + wordPadding), hardBreak: runHardBreak });
        runStart = Math.max(0, word.start - wordPadding);
        runEnd = word.end;
        runHardBreak = true;
      } else {
        runEnd = Math.max(runEnd, word.end);
      }
    });
    if (runStart !== null) units.push({ start: runStart, end: Math.min(duration, runEnd + wordPadding), hardBreak: runHardBreak });
    hardBreakBefore = hardBreakBefore || state.deletedWords.has(wordId(segment, words.length - 1));
  }
  const clips = [];
  for (const unit of units) {
    if (unit.end <= unit.start) continue;
    const previous = clips.at(-1);
    if (previous && !unit.hardBreak && unit.start - previous.end <= threshold) previous.end = Math.max(previous.end, unit.end);
    else clips.push({ start: unit.start, end: unit.end });
  }
  return applyClientTimingCuts(clips);
}

function auditionStart(requestedTime) {
  const time = Math.max(0, Number(requestedTime) || 0);
  if (!state.editedPreviewEnabled) return time;
  const clips = computeClientClips();
  if (!clips.length) return time;
  const containing = clips.find((clip) => time >= clip.start && time < clip.end);
  if (containing) return time;
  const nextIndex = clips.findIndex((clip) => clip.start > time);
  if (nextIndex > 0) {
    const previous = clips[nextIndex - 1];
    return Math.max(previous.start, previous.end - 1.2);
  }
  if (nextIndex === 0) return clips[0].start;
  return Math.max(clips.at(-1).start, clips.at(-1).end - 1.2);
}

function jumpPreviewVideo(video, target) {
  if (Math.abs(video.currentTime - target) < .02) return;
  state.previewJumping = true;
  video.currentTime = Math.max(0, target);
  setTimeout(() => { state.previewJumping = false; }, 80);
}

function alignEditedPreview(video) {
  if (!state.editedPreviewEnabled || state.previewJumping) return;
  const clips = computeClientClips();
  if (!clips.length) return video.pause();
  const time = video.currentTime;
  const insideIndex = clips.findIndex((clip) => time >= clip.start - .025 && time < clip.end - .015);
  if (insideIndex >= 0) return;
  const nextIndex = clips.findIndex((clip) => clip.start > time + .015);
  if (nextIndex >= 0) return jumpPreviewVideo(video, clips[nextIndex].start);
  video.pause();
}

function toggleEditedPreview() {
  state.editedPreviewEnabled = !state.editedPreviewEnabled;
  const button = $("#previewModeToggle");
  button.classList.toggle("on", state.editedPreviewEnabled);
  button.setAttribute("aria-pressed", String(state.editedPreviewEnabled));
  $("#previewModeHint").textContent = state.editedPreviewEnabled
    ? "播放时自动跳过已删字词和收紧的停顿"
    : "当前播放原片，不自动跳过剪辑位置";
  showToast(state.editedPreviewEnabled ? "已开启剪后即时试听" : "已切换为原片试听");
}

function toggleAutoPauses() {
  const button = $("#autoPauseToggle");
  const enabled = !button.classList.contains("on");
  button.classList.toggle("on", enabled);
  button.setAttribute("aria-pressed", String(enabled));
  updateSummary();
  renderTranscript();
  scheduleSave();
  showToast(enabled ? "已开启自动收紧长停顿" : "已关闭自动收紧，未单独清理的停顿会保留");
}

function updateSummary() {
  const source = Number(state.project?.source?.duration || 0);
  const output = computeClientClips().reduce((sum, clip) => sum + clip.end - clip.start, 0);
  $("#sourceDuration").textContent = formatDuration(source);
  $("#outputDuration").textContent = formatDuration(output);
  $("#removedDuration").textContent = formatDuration(Math.max(0, source - output));
}

async function importFromPath(path) {
  const project = await api("/api/projects/from-path", { method: "POST", body: JSON.stringify({ path }) });
  showToast("视频已打开");
  await openProject(project.id);
}

function nativeDesktopBridge() {
  const bridge = window.chrome?.webview;
  return bridge && typeof bridge.postMessage === "function" ? bridge : null;
}

function setNativeImportEnabled(enabled) {
  const bridge = nativeDesktopBridge();
  if (bridge && state.nativeFileBridgeReady) bridge.postMessage({ type: "set-native-import-enabled", enabled });
}

function requestNativeSourcePicker() {
  const bridge = nativeDesktopBridge();
  if (bridge && state.nativeFileBridgeReady) {
    bridge.postMessage({ type: "pick-native-source-file" });
    return;
  }
  $("#fileInput").click();
}

function bindNativeDesktopBridge() {
  const bridge = nativeDesktopBridge();
  if (!bridge) return;
  bridge.addEventListener("message", (event) => {
    const message = event.data || {};
    if (message.type === "native-bridge-ready") {
      state.nativeFileBridgeReady = true;
      setNativeImportEnabled(!$("#welcomeView").classList.contains("hidden"));
    } else if (message.type === "native-drag-state") {
      $("#dropZone").classList.toggle("dragging", Boolean(message.active));
    } else if (message.type === "native-source-importing") {
      showToast("正在直接打开原视频…");
    } else if (message.type === "native-source-ready" && message.projectId) {
      openProject(message.projectId).catch((error) => showToast(error.message, true));
    } else if (message.type === "native-source-error") {
      showToast(message.message || "无法打开这个视频文件", true);
    }
  });
  bridge.postMessage({ type: "native-bridge-handshake" });
}

async function uploadFile(file) {
  const pending = state.project?.upload;
  const resuming = !!pending;
  if (resuming && (pending.name !== file.name || Number(pending.size) !== file.size)) {
    showTask({ kind: "upload", state: "paused", progress: Number(pending.received || 0) / Number(pending.size || 1) * 100, error: "选择的不是原视频，请重新选择同名、同大小的文件", log: [] });
    throw new Error("请选择之前导入的同一个视频文件");
  }
  const project = resuming
    ? state.project
    : await api("/api/projects", { method: "POST", body: JSON.stringify({ name: file.name.replace(/\.[^.]+$/, "") }) });
  state.project = project;
  setNativeImportEnabled(false);
  $("#welcomeView").classList.add("hidden");
  $("#editorView").classList.remove("hidden");
  history.replaceState(null, "", `#project=${project.id}`);
  renderProjectHeader();
  let offset = Number(pending?.received || 0);
  showTask({ kind: "upload", state: "running", progress: offset / file.size * 100, log: [resuming ? `从 ${formatBytes(offset)} 继续` : "准备复制视频"] });
  if (!resuming) {
    state.project = await api(`/api/projects/${project.id}/upload-init`, { method: "POST", body: JSON.stringify({ name: file.name, size: file.size }) });
    offset = Number(state.project.upload?.received || 0);
  }
  const chunkSize = 8 * 1024 * 1024;
  try {
    while (offset < file.size) {
      const chunk = file.slice(offset, Math.min(file.size, offset + chunkSize));
      let result = null;
      let lastError = null;
      for (let attempt = 0; attempt < 3 && !result; attempt += 1) {
        try {
          result = await api(`/api/projects/${project.id}/upload?offset=${offset}`, { method: "PUT", body: chunk });
        } catch (error) {
          lastError = error;
          const latest = await api(`/api/projects/${project.id}`);
          const serverOffset = Number(latest.upload?.received || 0);
          if (serverOffset > offset) result = { received: serverOffset };
          else if (attempt < 2) await new Promise((resolve) => setTimeout(resolve, 450 * (attempt + 1)));
        }
      }
      if (!result) throw lastError || new Error("视频分块传输失败");
      offset = result.received;
      showTask({ kind: "upload", state: "running", progress: offset / file.size * 100, log: [`正在复制 ${formatBytes(offset)} / ${formatBytes(file.size)}`] });
    }
  } catch (error) {
    state.project = await api(`/api/projects/${project.id}`);
    const received = Number(state.project.upload?.received || offset);
    showTask({ kind: "upload", state: "paused", progress: received / file.size * 100, error: "导入已暂停，点击“继续导入”可从当前位置恢复", log: [`已保留 ${formatBytes(received)} / ${formatBytes(file.size)}`] });
    throw error;
  }
  state.project = await api(`/api/projects/${project.id}/upload-complete`, { method: "POST", body: JSON.stringify({}) });
  hideTask();
  renderProjectHeader();
  attachMedia();
  showTranscribeStage();
  showToast("视频导入完成");
}

function taskName(kind) {
  return ({ transcribe: "正在识别对白", preview: "正在生成预览", render: "正在导出成片", upload: "正在导入视频" })[kind] || "正在处理";
}

function taskDetailText(job) {
  if (job.error) return job.error;
  const details = [];
  if (job.phase) details.push(job.phase);
  if (job.processedSeconds != null && job.expectedSeconds != null
      && Number.isFinite(Number(job.processedSeconds)) && Number.isFinite(Number(job.expectedSeconds))) {
    details.push(`已处理 ${formatDuration(job.processedSeconds, true)} / ${formatDuration(job.expectedSeconds, true)}`);
  }
  if (Number(job.cachedBatches) > 0) details.push(`已复用 ${job.cachedBatches} 段`);
  if (job.state === "running" && Number(job.stalledSeconds) >= 30) {
    details.push(`已 ${formatDuration(job.stalledSeconds, true)} 没有新增画面，可取消后重试，完成的分段会保留`);
  }
  return details.join(" · ") || (job.state === "running"
    ? "可以继续浏览页面，请不要关闭启动窗口" : "任务准备中");
}

function showTask(job) {
  const panel = $("#taskPanel");
  panel.classList.remove("hidden", "done", "failed");
  panel.classList.toggle("done", job.state === "completed");
  panel.classList.toggle("failed", job.state === "failed");
  panel.classList.toggle("stalled", job.state === "running" && Number(job.stalledSeconds) >= 30);
  $("#taskTitle").textContent = job.state === "cancelling" ? "正在取消任务" : job.state === "cancelled" ? "任务已取消" : job.state === "paused" ? "导入已暂停" : job.state === "failed" ? "处理没有完成" : job.state === "completed" ? "处理完成" : taskName(job.kind);
  $("#taskDetail").textContent = taskDetailText(job);
  $("#taskProgress").style.width = `${Math.max(2, Number(job.progress || 0))}%`;
  $("#taskLog").textContent = (job.log || []).join("\n");
  $("#resumeUploadButton").classList.toggle("hidden", !(job.kind === "upload" && job.state === "paused"));
  $("#cancelTaskButton").classList.toggle("hidden", job.kind === "upload" || !["queued", "running", "cancelling"].includes(job.state));
  $("#cancelTaskButton").disabled = job.state === "cancelling";
}

function hideTask() {
  $("#taskPanel").classList.add("hidden");
  $("#taskPanel").classList.remove("done", "failed", "stalled");
  $("#taskLog").classList.add("hidden");
}

async function startProjectJob(kind) {
  if (!state.project) return;
  if (!(await flushProjectSaves())) return;
  try {
    closeModal();
    const job = await api(`/api/projects/${state.project.id}/${kind}`, { method: "POST", body: JSON.stringify({}) });
    monitorJob(job.id);
  } catch (error) {
    showToast(error.message, true);
  }
}

function monitorJob(jobId) {
  clearInterval(state.jobTimer);
  state.activeJobId = jobId;
  const poll = async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      showTask(job);
      if (["completed", "failed", "cancelled"].includes(job.state)) {
        clearInterval(state.jobTimer);
        state.activeJobId = null;
        if (job.state === "completed") {
          showToast("处理完成");
          const completedKind = job.kind;
          setTimeout(async () => {
            hideTask();
            await openProject(state.project.id);
            if (completedKind === "preview") openOutput();
          }, 500);
        } else if (job.state === "cancelled") {
          showToast("任务已取消");
          state.project = await api(`/api/projects/${state.project.id}`);
          renderProjectHeader();
          setTimeout(hideTask, 700);
        } else {
          showToast(job.error || "处理失败", true);
          state.project = await api(`/api/projects/${state.project.id}`);
          renderProjectHeader();
        }
      }
    } catch (error) {
      clearInterval(state.jobTimer);
      showToast(error.message, true);
    }
  };
  poll();
  state.jobTimer = setInterval(poll, 1200);
}

async function cancelActiveTask() {
  if (!state.activeJobId) return;
  if (!window.confirm("取消当前处理任务？已经生成的旧字幕和旧成片不会受影响。")) return;
  try {
    const job = await api(`/api/jobs/${state.activeJobId}/cancel`, { method: "POST", body: JSON.stringify({}) });
    showTask(job);
  } catch (error) { showToast(error.message, true); }
}

function renderOutputCard(stale = false) {
  const outputs = state.project?.outputs || {};
  const output = outputs.final || outputs.preview;
  const card = $("#outputCard");
  if (!output) return card.classList.add("hidden");
  card.classList.remove("hidden");
  const kind = outputs.final ? "final" : "preview";
  card.dataset.kind = kind;
  const revisionStale = Number(output.editRevision ?? -1) !== Number(state.project?.editRevision ?? 0);
  $("#outputTitle").textContent = `${kind === "final" ? "成片" : "预览"}已生成${stale || revisionStale ? " · 剪辑已改动" : ""}`;
  $("#outputMeta").textContent = `${formatDuration(output.duration)} · ${output.clipCount} 个保留片段${output.encoder ? ` · ${output.encoder}` : ""} · ${formatDate(output.generatedAt)}`;
}

function openOutput() {
  const kind = $("#outputCard").dataset.kind;
  if (!kind || !state.project) return;
  $("#modalTitle").textContent = kind === "final" ? "最终成片" : "快速预览";
  $("#outputVideo").src = `/api/projects/${state.project.id}/outputs/${kind}?t=${Date.now()}`;
  $("#modal").classList.remove("hidden");
}

async function openOutputFolder() {
  if (!state.project) return;
  try {
    await api(`/api/projects/${state.project.id}/open-output`, { method: "POST", body: JSON.stringify({}) });
  } catch (error) { showToast(error.message, true); }
}

function closeModal() {
  $("#modal").classList.add("hidden");
  $("#outputVideo").pause();
  $("#outputVideo").removeAttribute("src");
}

function visibleVideo(video) {
  return video && video.src && video.offsetParent !== null;
}

function toggleActiveVideoPlayback() {
  const videos = [$("#outputVideo"), $("#editVideo"), $("#sourceVideo")].filter(visibleVideo);
  const playing = videos.find((video) => !video.paused && !video.ended);
  if (playing) {
    state.lastVideoId = playing.id;
    playing.pause();
    return true;
  }
  const preferred = document.getElementById(state.lastVideoId);
  const video = visibleVideo(preferred) ? preferred : videos[0];
  if (!video) return false;
  state.lastVideoId = video.id;
  if (video.ended) video.currentTime = 0;
  video.play().catch(() => {});
  return true;
}

function bindEvents() {
  $("#brandHome").addEventListener("click", (event) => { event.preventDefault(); showWelcome(); });
  $("#trashButton").addEventListener("click", () => toggleTrash());
  $("#closeTrashButton").addEventListener("click", () => toggleTrash(false));
  $("#newProjectButton").addEventListener("click", () => showWelcome());
  $("#backButton").addEventListener("click", () => showWelcome());
  $("#pathForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const path = $("#pathInput").value.trim();
    if (!path) return showToast("请先粘贴视频文件路径", true);
    try { await importFromPath(path); } catch (error) { showToast(error.message, true); }
  });
  $("#fileInput").addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (file) uploadFile(file).catch((error) => showToast(error.message, true));
  });
  const dropZone = $("#dropZone");
  dropZone.addEventListener("click", requestNativeSourcePicker);
  dropZone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      requestNativeSourcePicker();
    }
  });
  for (const name of ["dragenter", "dragover"]) dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.add("dragging"); });
  for (const name of ["dragleave", "drop"]) dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.remove("dragging"); });
  dropZone.addEventListener("drop", (event) => {
    const file = event.dataTransfer.files[0];
    // Desktop mode deliberately uses the native file picker for a clean,
    // path-based import. Browser mode keeps the existing upload fallback.
    if (nativeDesktopBridge() && state.nativeFileBridgeReady) return;
    if (file) uploadFile(file).catch((error) => showToast(error.message, true));
  });
  $("#transcribeButton").addEventListener("click", async () => {
    try {
      await saveRecognitionSelection();
      await startProjectJob("transcribe");
    } catch (error) { showToast(error.message, true); }
  });
  $("#engineSelect").addEventListener("change", () => changeRecognitionEngine().catch((error) => showToast(error.message, true)));
  $("#modelSelect").addEventListener("change", () => saveRecognitionSelection().catch((error) => showToast(error.message, true)));
  $("#refreshModelsButton").addEventListener("click", refreshRecognitionModels);
  $("#srtForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const path = $("#srtPathInput").value.trim();
    if (!path) return showToast("请先粘贴 SRT 字幕路径", true);
    try {
      state.project = await api(`/api/projects/${state.project.id}/import-srt`, { method: "POST", body: JSON.stringify({ path }) });
      showToast("字幕已导入");
      await openProject(state.project.id);
    } catch (error) { showToast(error.message, true); }
  });
  $("#previewButton").addEventListener("click", () => startProjectJob("preview"));
  $("#previewModeToggle").addEventListener("click", toggleEditedPreview);
  $("#cutModeButton").addEventListener("click", () => setTranscriptMode("cut"));
  $("#subtitleModeButton").addEventListener("click", () => setTranscriptMode("subtitle"));
  $("#renderButton").addEventListener("click", () => startProjectJob("render"));
  $("#deleteProjectButton").addEventListener("click", () => confirmDeleteProject());
  $("#resumeUploadButton").addEventListener("click", () => $("#fileInput").click());
  $("#cancelTaskButton").addEventListener("click", cancelActiveTask);
  $("#taskLogToggle").addEventListener("click", () => $("#taskLog").classList.toggle("hidden"));
  $("#playOutputButton").addEventListener("click", openOutput);
  $("#openOutputFolderButton").addEventListener("click", openOutputFolder);
  $("#scanCleanupButton").addEventListener("click", scanCleanupSuggestions);
  $("#applyAllCleanupButton").addEventListener("click", applyAllCleanupSuggestions);
  $("#removeAllTimingButton").addEventListener("click", () => removeAllTiming("all"));
  $("#removeAllStretchButton").addEventListener("click", () => removeAllTiming("stretch"));
  $("#removeAllBreathButton").addEventListener("click", () => removeAllTiming("breath"));
  $("#removeAllPauseButton").addEventListener("click", () => removeAllTiming("pause"));
  $("#findNextSubtitleButton").addEventListener("click", findNextSubtitle);
  $("#replaceCurrentSubtitleButton").addEventListener("click", replaceCurrentSubtitle);
  $("#replaceAllSubtitlesButton").addEventListener("click", replaceAllSubtitles);
  $("#subtitleFindInput").addEventListener("input", () => {
    state.subtitleSearchQuery = $("#subtitleFindInput").value;
    state.subtitleSearchIndex = -1;
    state.currentSubtitleMatch = null;
    updateSubtitleFindStatus();
  });
  $("#subtitleFindInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); findNextSubtitle(); }
  });
  $("#closeCleanupButton").addEventListener("click", () => $("#cleanupPanel").classList.add("hidden"));
  $("#undoEditButton").addEventListener("click", undoEdit);
  $$('[data-close-modal]').forEach((node) => node.addEventListener("click", closeModal));
  for (const selector of ["#pauseThreshold", "#leadPadding", "#tailPadding", "#wordPadding"]) {
    $(selector).addEventListener("input", () => { updateSettingLabels(); updateSummary(); scheduleSave(); });
  }
  $("#pauseThreshold").addEventListener("change", renderTranscript);
  $("#autoPauseToggle").addEventListener("click", toggleAutoPauses);
  $("#resetSettings").addEventListener("click", () => {
    $("#pauseThreshold").value = 500; $("#leadPadding").value = 110; $("#tailPadding").value = 150; $("#wordPadding").value = 50;
    $("#autoPauseToggle").classList.add("on"); $("#autoPauseToggle").setAttribute("aria-pressed", "true");
    updateSettingLabels(); updateSummary(); renderTranscript(); scheduleSave();
  });
  $("#keepAllButton").addEventListener("click", () => {
    if (!state.deleted.size && !state.deletedWords.size && !state.deletedTiming.size) return;
    pushEditHistory();
    state.deleted.clear();
    state.deletedWords.clear();
    state.deletedTiming.clear();
    renderTranscript();
    updateSummary();
    scheduleSave();
    refreshCleanupSuggestions();
  });
  $("#removeFillersButton").addEventListener("click", () => {
    state.fillersVisible = !state.fillersVisible;
    renderTranscript();
    const count = state.transcript?.segments.filter((segment) => fillers.some((word) => segment.text.includes(word))).length || 0;
    showToast(state.fillersVisible ? `已标出 ${count} 句中的口头禅，请人工确认` : "已关闭口头禅标记");
  });
  $("#editVideo").addEventListener("timeupdate", (event) => {
    const time = event.target.currentTime;
    $("#videoTime").textContent = formatDuration(time, true);
    if (!event.target.paused && !event.target.seeking) alignEditedPreview(event.target);
    const current = state.transcript?.segments.find((segment) => time >= segment.start && time <= segment.end);
    if (current) {
      $$(".segment.active").forEach((node) => node.classList.toggle("active", node.dataset.id === current.id));
    }
  });
  $("#editVideo").addEventListener("play", (event) => {
    state.lastVideoId = event.target.id;
    if (state.editedPreviewEnabled) jumpPreviewVideo(event.target, auditionStart(event.target.currentTime));
  });
  for (const selector of ["#sourceVideo", "#editVideo", "#outputVideo"]) {
    const video = $(selector);
    video.addEventListener("pointerdown", () => { state.lastVideoId = video.id; });
    video.addEventListener("play", () => { state.lastVideoId = video.id; });
  }
  document.addEventListener("selectionchange", () => {
    cancelAnimationFrame(state.selectionFrame);
    state.selectionFrame = requestAnimationFrame(refreshSelectionState);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") return closeModal();
    if ((event.ctrlKey || event.metaKey) && $("#workspace").classList.contains("subtitle-mode")) {
      if (event.key.toLowerCase() === "f") {
        event.preventDefault();
        $("#subtitleFindInput").focus();
        $("#subtitleFindInput").select();
        return;
      }
      if (event.key.toLowerCase() === "h") {
        event.preventDefault();
        $("#subtitleReplaceInput").focus();
        $("#subtitleReplaceInput").select();
        return;
      }
    }
    const editingControl = event.target.closest?.("input, textarea, select, [contenteditable]");
    if (editingControl) return;
    if ((event.code === "Space" || event.key === " ") && !event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey && !event.repeat) {
      if (toggleActiveVideoPlayback()) event.preventDefault();
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
      event.preventDefault();
      return event.shiftKey ? redoEdit() : undoEdit();
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "y") {
      event.preventDefault();
      return redoEdit();
    }
    if (["Delete", "Backspace"].includes(event.key) && !$("#workspace").classList.contains("hidden")) {
      if (editSelectedWords()) event.preventDefault();
    }
  });
}

async function init() {
  bindNativeDesktopBridge();
  bindEvents();
  try {
    await Promise.all([loadConfig(), loadProjects()]);
    const match = location.hash.match(/project=([a-f0-9]{12})/);
    if (match) await openProject(match[1]);
  } catch (error) {
    showToast(error.message, true);
  }
}

init();
