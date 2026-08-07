(() => {
  "use strict";

  const TOKEN_KEY = "workchat.admin.token.v1";
  const PAGE_SIZE = 10;
  const numberFormatter = new Intl.NumberFormat("zh-CN");
  const dateFormatter = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

  const state = {
    token: "",
    session: null,
    overview: null,
    knowledgeBases: [],
    documents: new Map(),
    page: 0,
    totalDocuments: 0,
    documentRequest: 0,
    activeDocumentId: null,
    uploadDocument: null,
    uploading: false,
    searchTimer: null,
    uploadProgressTimer: null,
  };

  const elements = {
    appShell: document.querySelector("#appShell"),
    authDialog: document.querySelector("#authDialog"),
    authForm: document.querySelector("#authForm"),
    authError: document.querySelector("#authError"),
    tokenInput: document.querySelector("#tokenInput"),
    logoutButton: document.querySelector("#logoutButton"),
    breadcrumbCurrent: document.querySelector("#breadcrumbCurrent"),
    tenantShort: document.querySelector("#tenantShort"),
    navDocumentCount: document.querySelector("#navDocumentCount"),
    knowledgeBaseList: document.querySelector("#knowledgeBaseList"),
    knowledgeFilter: document.querySelector("#knowledgeFilter"),
    statusFilter: document.querySelector("#statusFilter"),
    documentSearch: document.querySelector("#documentSearch"),
    uploadKnowledgeBase: document.querySelector("#uploadKnowledgeBase"),
    documentTableBody: document.querySelector("#documentTableBody"),
    documentEmpty: document.querySelector("#documentEmpty"),
    documentSummary: document.querySelector("#documentSummary"),
    filterHint: document.querySelector("#filterHint"),
    pageIndicator: document.querySelector("#pageIndicator"),
    previousPage: document.querySelector("#previousPage"),
    nextPage: document.querySelector("#nextPage"),
    refreshDocuments: document.querySelector("#refreshDocuments"),
    refreshHealth: document.querySelector("#refreshHealth"),
    healthList: document.querySelector("#healthList"),
    globalHealthDot: document.querySelector("#globalHealthDot"),
    globalHealthText: document.querySelector("#globalHealthText"),
    storageValue: document.querySelector("#storageValue"),
    storageTrack: document.querySelector("#storageTrack"),
    uploadDialog: document.querySelector("#uploadDialog"),
    uploadDialogTitle: document.querySelector("#uploadDialogTitle"),
    uploadDialogCopy: document.querySelector("#uploadDialogCopy"),
    uploadForm: document.querySelector("#uploadForm"),
    uploadDocumentId: document.querySelector("#uploadDocumentId"),
    uploadFile: document.querySelector("#uploadFile"),
    uploadTitle: document.querySelector("#uploadTitle"),
    uploadSourceCode: document.querySelector("#uploadSourceCode"),
    uploadError: document.querySelector("#uploadError"),
    uploadSubmit: document.querySelector("#uploadSubmit"),
    uploadProgress: document.querySelector("#uploadProgress"),
    dropZone: document.querySelector("#dropZone"),
    dropTitle: document.querySelector("#dropTitle"),
    dropMeta: document.querySelector("#dropMeta"),
    detailDialog: document.querySelector("#detailDialog"),
    detailContent: document.querySelector("#detailContent"),
    toastRegion: document.querySelector("#toastRegion"),
    sidebar: document.querySelector("#sidebar"),
    sidebarBackdrop: document.querySelector("#sidebarBackdrop"),
    mobileMenu: document.querySelector("#mobileMenu"),
  };

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[character]);
  }

  function formatNumber(value) {
    const number = Number(value);
    return numberFormatter.format(Number.isFinite(number) ? number : 0);
  }

  function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes <= 0) {
      return "0 B";
    }
    const units = ["B", "KB", "MB", "GB", "TB"];
    const unitIndex = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const amount = bytes / (1024 ** unitIndex);
    const precision = amount >= 100 || unitIndex === 0 ? 0 : amount >= 10 ? 1 : 2;
    return `${amount.toFixed(precision)} ${units[unitIndex]}`;
  }

  function formatDate(value) {
    if (!value) {
      return "—";
    }
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "—" : dateFormatter.format(date).replace("24:", "00:");
  }

  function fileExtension(fileName) {
    const parts = String(fileName || "file").split(".");
    return parts.length > 1 ? parts.pop().slice(0, 4).toUpperCase() : "FILE";
  }

  function baseFileName(fileName) {
    return String(fileName || "").replace(/\.[^.]+$/, "");
  }

  function statusMeta(status) {
    const values = {
      READY: ["可用", ""],
      DISABLED: ["已停用", "disabled"],
      FAILED: ["失败", "failed"],
      UPLOADED: ["已上传", "processing"],
      PARSING: ["解析中", "processing"],
      CHUNKING: ["切片中", "processing"],
      EMBEDDING: ["向量化中", "processing"],
      INDEXING: ["索引中", "processing"],
      QUEUED: ["等待处理", "processing"],
      ACTIVE: ["启用", ""],
    };
    return values[status] || [status || "未知", "disabled"];
  }

  async function api(path, options = {}, token = state.token) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (token) {
      headers.set("X-Internal-Token", token);
    }
    const response = await fetch(path, { ...options, headers });
    const contentType = response.headers.get("content-type") || "";
    let payload = null;
    if (contentType.includes("application/json")) {
      payload = await response.json().catch(() => null);
    } else {
      const text = await response.text().catch(() => "");
      payload = text ? { detail: text } : null;
    }
    if (!response.ok) {
      const message = payload?.detail || payload?.message || `请求失败（HTTP ${response.status}）`;
      throw new ApiError(String(message), response.status);
    }
    return payload;
  }

  function readStoredToken() {
    try {
      return sessionStorage.getItem(TOKEN_KEY) || "";
    } catch (_error) {
      return "";
    }
  }

  function storeToken(token) {
    try {
      if (token) {
        sessionStorage.setItem(TOKEN_KEY, token);
      } else {
        sessionStorage.removeItem(TOKEN_KEY);
      }
    } catch (_error) {
      // 禁用浏览器存储时仍允许当前页面会话继续使用。
    }
  }

  function showToast(title, message = "", type = "success") {
    const toast = document.createElement("div");
    toast.className = `toast${type === "error" ? " error" : ""}`;
    const copy = document.createElement("div");
    const heading = document.createElement("strong");
    const detail = document.createElement("span");
    heading.textContent = title;
    detail.textContent = message;
    copy.append(heading, detail);
    toast.append(copy);
    elements.toastRegion.append(toast);
    window.setTimeout(() => toast.remove(), 4500);
  }

  function handleRequestError(error, fallbackMessage) {
    if (error instanceof ApiError && error.status === 401 && state.session) {
      expireSession();
      return;
    }
    showToast(fallbackMessage, error instanceof Error ? error.message : "未知错误", "error");
  }

  function showAuth(message = "") {
    elements.appShell.setAttribute("aria-hidden", "true");
    elements.authError.textContent = message;
    if (!elements.authDialog.open) {
      elements.authDialog.showModal();
    }
    window.setTimeout(() => elements.tokenInput.focus(), 30);
  }

  function expireSession() {
    state.token = "";
    state.session = null;
    storeToken("");
    closeDialog(elements.uploadDialog);
    closeDialog(elements.detailDialog);
    showAuth("登录状态已失效，请重新输入管理员令牌。");
  }

  async function authenticate(token) {
    const session = await api("/internal/v1/admin/session", {}, token);
    state.token = token;
    state.session = session;
    storeToken(token);
    elements.authError.textContent = "";
    closeDialog(elements.authDialog);
    elements.appShell.setAttribute("aria-hidden", "false");
    configureSession(session);
    await Promise.allSettled([
      loadOverview(),
      loadKnowledgeBases(),
      loadDocuments(),
      loadHealth(),
    ]);
  }

  function configureSession(session) {
    const tenantId = String(session.tenant_id || "");
    elements.tenantShort.textContent = tenantId ? `租户 ${tenantId.slice(0, 8)}` : "内部工作区";
    const maxSize = formatBytes(session.max_upload_bytes);
    const extensions = (session.supported_extensions || []).map((value) => String(value).toUpperCase());
    elements.dropMeta.textContent = `${extensions.join("、")} · 最大 ${maxSize}`;
    elements.uploadFile.accept = extensions.map((value) => `.${value.toLowerCase()}`).join(",");
  }

  async function loadOverview() {
    try {
      const overview = await api("/internal/v1/admin/overview");
      state.overview = overview;
      renderOverview(overview);
    } catch (error) {
      handleRequestError(error, "工作台数据读取失败");
    }
  }

  function renderOverview(overview) {
    const activeChunks = Number(overview.active_chunk_count) || 0;
    const vectorizedChunks = Number(overview.vectorized_chunk_count) || 0;
    const vectorCoverage = activeChunks > 0 ? Math.round((vectorizedChunks / activeChunks) * 100) : 0;
    const metrics = {
      documents: formatNumber(overview.document_count),
      chunks: formatNumber(activeChunks),
      vectors: `${vectorCoverage}%`,
      questions: formatNumber(overview.questions_24h),
    };
    Object.entries(metrics).forEach(([name, value]) => {
      const metric = document.querySelector(`[data-metric="${name}"]`);
      if (metric) {
        metric.textContent = value;
      }
    });
    elements.navDocumentCount.textContent = formatNumber(overview.document_count);
    elements.storageValue.textContent = formatBytes(overview.storage_bytes);
    const storageRatio = Math.min(100, (Number(overview.storage_bytes) / (1024 ** 3)) * 100);
    elements.storageTrack.style.width = `${storageRatio > 0 ? Math.max(storageRatio, 4) : 0}%`;
    if (overview.tenant_name) {
      elements.tenantShort.textContent = String(overview.tenant_name);
    }
  }

  async function loadKnowledgeBases() {
    try {
      const knowledgeBases = await api("/internal/v1/admin/knowledge-bases");
      state.knowledgeBases = Array.isArray(knowledgeBases) ? knowledgeBases : [];
      renderKnowledgeBases();
      populateKnowledgeBaseSelects();
    } catch (error) {
      elements.knowledgeBaseList.innerHTML = '<div class="empty-inline">知识库读取失败，请稍后重试。</div>';
      handleRequestError(error, "知识库读取失败");
    }
  }

  function renderKnowledgeBases() {
    if (!state.knowledgeBases.length) {
      elements.knowledgeBaseList.innerHTML = '<div class="empty-inline">当前租户还没有知识库。</div>';
      return;
    }
    elements.knowledgeBaseList.innerHTML = state.knowledgeBases.map((knowledgeBase) => {
      const [label, statusClass] = statusMeta(knowledgeBase.status);
      return `
        <button class="knowledge-item" type="button" data-kb-id="${escapeHtml(knowledgeBase.id)}">
          <span class="knowledge-icon"><svg><use href="#icon-database"></use></svg></span>
          <span class="knowledge-copy">
            <strong>${escapeHtml(knowledgeBase.name)}</strong>
            <p>${escapeHtml(knowledgeBase.description || knowledgeBase.code || "暂无说明")}</p>
          </span>
          <span class="knowledge-stats">
            <span>文档<strong>${formatNumber(knowledgeBase.document_count)}</strong></span>
            <span>知识块<strong>${formatNumber(knowledgeBase.active_chunk_count)}</strong></span>
            <span class="status-badge ${statusClass}">${escapeHtml(label)}</span>
          </span>
        </button>`;
    }).join("");
  }

  function replaceSelectOptions(select, firstLabel, values, selectedValue) {
    select.replaceChildren();
    const firstOption = document.createElement("option");
    firstOption.value = "";
    firstOption.textContent = firstLabel;
    select.append(firstOption);
    values.forEach((value) => {
      const option = document.createElement("option");
      option.value = String(value.id);
      option.textContent = String(value.name);
      if (value.status !== "ACTIVE") {
        option.textContent += "（已停用）";
        option.disabled = true;
      }
      select.append(option);
    });
    if (selectedValue && [...select.options].some((option) => option.value === selectedValue)) {
      select.value = selectedValue;
    }
  }

  function populateKnowledgeBaseSelects() {
    const filterValue = elements.knowledgeFilter.value;
    const uploadValue = elements.uploadKnowledgeBase.value;
    replaceSelectOptions(elements.knowledgeFilter, "全部知识库", state.knowledgeBases, filterValue);
    replaceSelectOptions(
      elements.uploadKnowledgeBase,
      "选择知识库",
      state.knowledgeBases,
      uploadValue,
    );
    const activeOptions = [...elements.uploadKnowledgeBase.options].filter(
      (option) => option.value && !option.disabled,
    );
    if (!elements.uploadKnowledgeBase.value && activeOptions.length === 1) {
      elements.uploadKnowledgeBase.value = activeOptions[0].value;
    }
  }

  function documentQuery() {
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(state.page * PAGE_SIZE),
    });
    const search = elements.documentSearch.value.trim();
    if (search) {
      params.set("search", search);
    }
    if (elements.knowledgeFilter.value) {
      params.set("knowledge_base_id", elements.knowledgeFilter.value);
    }
    if (elements.statusFilter.value) {
      params.set("status", elements.statusFilter.value);
    }
    return params;
  }

  async function loadDocuments() {
    const requestNumber = ++state.documentRequest;
    setDocumentLoading();
    try {
      const result = await api(`/internal/v1/admin/documents?${documentQuery()}`);
      if (requestNumber !== state.documentRequest) {
        return;
      }
      const items = Array.isArray(result.items) ? result.items : [];
      state.totalDocuments = Number(result.total) || 0;
      const lastPage = Math.max(0, Math.ceil(state.totalDocuments / PAGE_SIZE) - 1);
      if (state.page > lastPage) {
        state.page = lastPage;
        await loadDocuments();
        return;
      }
      state.documents = new Map(items.map((item) => [String(item.id), item]));
      renderDocuments(items);
    } catch (error) {
      if (requestNumber !== state.documentRequest) {
        return;
      }
      elements.documentEmpty.hidden = true;
      elements.documentTableBody.innerHTML = '<tr><td colspan="7"><div class="table-error">文档读取失败，请稍后重试。</div></td></tr>';
      elements.documentSummary.textContent = "文档读取失败";
      handleRequestError(error, "文档列表读取失败");
    }
  }

  function setDocumentLoading() {
    elements.documentEmpty.hidden = true;
    elements.documentTableBody.hidden = false;
    elements.documentTableBody.innerHTML = '<tr><td colspan="7"><div class="table-loading"><span></span><span></span><span></span></div></td></tr>';
    elements.documentSummary.textContent = "正在读取文档…";
  }

  function renderDocuments(items) {
    elements.documentEmpty.hidden = items.length > 0;
    elements.documentTableBody.hidden = items.length === 0;
    elements.documentTableBody.innerHTML = items.map((item) => {
      const [statusLabel, statusClass] = statusMeta(item.status);
      const [indexLabel] = statusMeta(item.index_status);
      const source = item.source_code || item.file_name || "未设置来源";
      let stateAction = "";
      if (item.status === "READY") {
        stateAction = `<button class="mini-action danger" type="button" data-doc-action="state" data-document-id="${escapeHtml(item.id)}" data-active="false">停用</button>`;
      } else if (item.status === "DISABLED") {
        stateAction = `<button class="mini-action" type="button" data-doc-action="state" data-document-id="${escapeHtml(item.id)}" data-active="true">恢复</button>`;
      }
      return `
        <tr>
          <td><div class="document-cell">
            <span class="file-type">${escapeHtml(fileExtension(item.file_name))}</span>
            <span class="document-name">
              <button type="button" data-doc-action="detail" data-document-id="${escapeHtml(item.id)}">${escapeHtml(item.title)}</button>
              <small>${escapeHtml(source)}</small>
            </span>
          </div></td>
          <td><span class="kb-chip">${escapeHtml(item.knowledge_base_name)}</span></td>
          <td><span class="version-copy"><strong>v${formatNumber(item.version_number)}</strong><span>${escapeHtml(indexLabel)} · ${escapeHtml(item.index_version || "无索引版本")}</span></span></td>
          <td><span class="chunk-copy"><strong>${formatNumber(item.chunk_count)}</strong><span>${formatNumber(item.vectorized_chunk_count)} 已向量化</span></span></td>
          <td><span class="status-badge ${statusClass}">${escapeHtml(statusLabel)}</span></td>
          <td><span class="date-copy">${escapeHtml(formatDate(item.updated_at))}</span></td>
          <td><div class="row-actions">
            <button class="mini-action" type="button" data-doc-action="detail" data-document-id="${escapeHtml(item.id)}">详情</button>
            <button class="mini-action" type="button" data-doc-action="version" data-document-id="${escapeHtml(item.id)}">新版本</button>
            ${stateAction}
          </div></td>
        </tr>`;
    }).join("");

    const first = state.totalDocuments ? state.page * PAGE_SIZE + 1 : 0;
    const last = Math.min((state.page + 1) * PAGE_SIZE, state.totalDocuments);
    const pageCount = Math.max(1, Math.ceil(state.totalDocuments / PAGE_SIZE));
    elements.documentSummary.textContent = `共 ${formatNumber(state.totalDocuments)} 份文档 · 当前 ${first}–${last}`;
    const filters = [];
    if (elements.documentSearch.value.trim()) filters.push("关键词");
    if (elements.knowledgeFilter.value) filters.push("知识库");
    if (elements.statusFilter.value) filters.push("状态");
    elements.filterHint.textContent = filters.length ? `已启用 ${filters.join("、")}筛选` : "按更新时间倒序";
    elements.pageIndicator.textContent = `第 ${state.page + 1} / ${pageCount} 页`;
    elements.previousPage.disabled = state.page === 0;
    elements.nextPage.disabled = state.page >= pageCount - 1;
  }

  async function openDocumentDetail(documentId) {
    state.activeDocumentId = documentId;
    elements.detailContent.innerHTML = '<div class="detail-loading"><span></span><p>读取文档详情…</p></div>';
    if (!elements.detailDialog.open) {
      elements.detailDialog.showModal();
    }
    try {
      const documentDetail = await api(`/internal/v1/admin/documents/${encodeURIComponent(documentId)}`);
      if (state.activeDocumentId !== documentId) {
        return;
      }
      renderDocumentDetail(documentDetail);
    } catch (error) {
      elements.detailContent.innerHTML = '<div class="detail-loading"><p>文档详情读取失败。</p></div>';
      handleRequestError(error, "文档详情读取失败");
    }
  }

  function renderDocumentDetail(documentDetail) {
    const [statusLabel, statusClass] = statusMeta(documentDetail.status);
    let stateAction = "";
    if (documentDetail.status === "READY") {
      stateAction = `<button class="secondary-button" type="button" data-detail-action="state" data-active="false" data-document-id="${escapeHtml(documentDetail.id)}">停用检索</button>`;
    } else if (documentDetail.status === "DISABLED") {
      stateAction = `<button class="secondary-button" type="button" data-detail-action="state" data-active="true" data-document-id="${escapeHtml(documentDetail.id)}">恢复检索</button>`;
    }
    const versions = Array.isArray(documentDetail.versions) ? documentDetail.versions : [];
    const versionList = versions.length ? versions.map((version) => {
      const [indexLabel] = statusMeta(version.index_status);
      return `
        <div class="version-item${version.is_current ? " current" : ""}">
          <span class="version-marker"></span>
          <div class="version-body">
            <strong>v${formatNumber(version.version_number)} · ${escapeHtml(version.file_name)}${version.is_current ? "（当前）" : ""}</strong>
            <p>${escapeHtml(indexLabel)} · ${formatNumber(version.chunk_count)} 个知识块 · ${formatBytes(version.file_size)}</p>
            <small>${escapeHtml(formatDate(version.created_at))} · 索引 ${escapeHtml(version.index_version || "—")}</small>
          </div>
        </div>`;
    }).join("") : '<div class="empty-inline">暂无版本记录。</div>';

    elements.detailContent.innerHTML = `
      <div class="detail-heading">
        <p class="panel-kicker">DOCUMENT PROFILE</p>
        <h2>${escapeHtml(documentDetail.title)}</h2>
        <p>${escapeHtml(documentDetail.knowledge_base_name)} · 更新于 ${escapeHtml(formatDate(documentDetail.updated_at))}</p>
        <div class="detail-actions">
          <button class="primary-button" type="button" data-detail-action="version" data-document-id="${escapeHtml(documentDetail.id)}"><svg><use href="#icon-upload"></use></svg>上传新版本</button>
          ${stateAction}
        </div>
      </div>
      <section class="detail-section">
        <h3>文档信息</h3>
        <div class="metadata-grid">
          <div class="metadata-item"><span>状态</span><strong><span class="status-badge ${statusClass}">${escapeHtml(statusLabel)}</span></strong></div>
          <div class="metadata-item"><span>来源代码</span><strong>${escapeHtml(documentDetail.source_code || "未设置")}</strong></div>
          <div class="metadata-item"><span>安全分级</span><strong>${escapeHtml(documentDetail.classification_code || "INTERNAL")}</strong></div>
          <div class="metadata-item"><span>ACL 模式</span><strong>${escapeHtml(documentDetail.acl_mode || "INHERIT")}</strong></div>
          <div class="metadata-item"><span>累计引用</span><strong>${formatNumber(documentDetail.citation_count)} 次</strong></div>
          <div class="metadata-item"><span>文档 ID</span><strong>${escapeHtml(documentDetail.id)}</strong></div>
        </div>
      </section>
      <section class="detail-section">
        <h3>版本历史</h3>
        <div class="version-list">${versionList}</div>
      </section>`;
    state.documents.set(String(documentDetail.id), documentDetail);
  }

  async function setDocumentState(documentId, active, trigger) {
    const documentItem = state.documents.get(documentId);
    if (!active) {
      const title = documentItem?.title || "该文档";
      if (!window.confirm(`确认停用“${title}”的知识检索？原文件和版本历史会保留。`)) {
        return;
      }
    }
    if (trigger) {
      trigger.disabled = true;
    }
    try {
      await api(`/internal/v1/admin/documents/${encodeURIComponent(documentId)}/state`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ active }),
      });
      showToast(active ? "文档已恢复" : "文档已停用", active ? "当前版本已重新进入检索。" : "该文档不会再参与 RAG 检索。");
      await Promise.allSettled([loadDocuments(), loadOverview(), loadKnowledgeBases()]);
      if (elements.detailDialog.open && state.activeDocumentId === documentId) {
        await openDocumentDetail(documentId);
      }
    } catch (error) {
      handleRequestError(error, active ? "恢复文档失败" : "停用文档失败");
    } finally {
      if (trigger) {
        trigger.disabled = false;
      }
    }
  }

  function openUploadDialog(documentItem = null) {
    state.uploadDocument = documentItem;
    state.uploading = false;
    elements.uploadForm.reset();
    elements.uploadError.textContent = "";
    elements.uploadProgress.hidden = true;
    elements.uploadProgress.querySelector("span").style.width = "10%";
    elements.dropZone.classList.remove("has-file", "dragging");
    elements.dropTitle.textContent = "拖放文件到这里，或点击选择";
    configureSession(state.session || {});

    if (documentItem) {
      elements.uploadDialogTitle.textContent = "上传文档新版本";
      elements.uploadDialogCopy.textContent = "新版本索引成功后会成为当前版本，旧版本继续保留。";
      elements.uploadDocumentId.value = String(documentItem.id);
      elements.uploadTitle.value = String(documentItem.title || "");
      elements.uploadSourceCode.value = String(documentItem.source_code || "");
      elements.uploadKnowledgeBase.value = String(documentItem.knowledge_base_id || "");
      elements.uploadKnowledgeBase.disabled = true;
      elements.uploadSubmit.querySelector("span").textContent = "上传新版本";
    } else {
      elements.uploadDialogTitle.textContent = "上传知识文档";
      elements.uploadDialogCopy.textContent = "文件会存入 MinIO，并自动解析、切片和建立索引。";
      elements.uploadDocumentId.value = "";
      elements.uploadKnowledgeBase.disabled = false;
      const activeOptions = [...elements.uploadKnowledgeBase.options].filter(
        (option) => option.value && !option.disabled,
      );
      if (activeOptions.length === 1) {
        elements.uploadKnowledgeBase.value = activeOptions[0].value;
      }
      elements.uploadSubmit.querySelector("span").textContent = "上传并建立索引";
    }
    elements.uploadSubmit.disabled = false;
    if (!elements.uploadDialog.open) {
      elements.uploadDialog.showModal();
    }
  }

  function validateFile(file) {
    if (!file) {
      return "请选择需要上传的文件。";
    }
    const extension = String(file.name).split(".").pop().toLowerCase();
    const supported = new Set((state.session?.supported_extensions || []).map((value) => String(value).toLowerCase()));
    if (supported.has("md")) {
      supported.add("markdown");
    }
    if (supported.size && !supported.has(extension)) {
      return `不支持 .${extension || "未知"} 文件。`;
    }
    if (Number(file.size) > Number(state.session?.max_upload_bytes || Infinity)) {
      return `文件超过 ${formatBytes(state.session.max_upload_bytes)} 上传限制。`;
    }
    if (!file.size) {
      return "不能上传空文件。";
    }
    return "";
  }

  function updateSelectedFile(file) {
    const error = validateFile(file);
    elements.uploadError.textContent = error;
    if (error) {
      elements.dropZone.classList.remove("has-file");
      return;
    }
    elements.dropZone.classList.add("has-file");
    elements.dropTitle.textContent = file.name;
    elements.dropMeta.textContent = `${formatBytes(file.size)} · ${fileExtension(file.name)} 文档`;
    if (!elements.uploadTitle.value.trim()) {
      elements.uploadTitle.value = baseFileName(file.name);
    }
  }

  function startUploadProgress() {
    let progress = 12;
    const bar = elements.uploadProgress.querySelector("span");
    elements.uploadProgress.hidden = false;
    bar.style.width = `${progress}%`;
    window.clearInterval(state.uploadProgressTimer);
    state.uploadProgressTimer = window.setInterval(() => {
      progress = Math.min(88, progress + Math.max(1, Math.round((88 - progress) / 7)));
      bar.style.width = `${progress}%`;
    }, 300);
  }

  function stopUploadProgress(completed) {
    window.clearInterval(state.uploadProgressTimer);
    state.uploadProgressTimer = null;
    const bar = elements.uploadProgress.querySelector("span");
    bar.style.width = completed ? "100%" : "0%";
    if (!completed) {
      elements.uploadProgress.hidden = true;
    }
  }

  async function submitUpload(event) {
    event.preventDefault();
    if (state.uploading) {
      return;
    }
    const file = elements.uploadFile.files[0];
    const fileError = validateFile(file);
    const knowledgeBaseId = elements.uploadKnowledgeBase.value;
    if (fileError || !knowledgeBaseId) {
      elements.uploadError.textContent = fileError || "请选择知识库。";
      return;
    }
    const formData = new FormData();
    formData.append("file", file);
    formData.append("knowledge_base_id", knowledgeBaseId);
    formData.append("title", elements.uploadTitle.value.trim() || baseFileName(file.name));
    if (elements.uploadSourceCode.value.trim()) {
      formData.append("source_code", elements.uploadSourceCode.value.trim());
    }
    if (elements.uploadDocumentId.value) {
      formData.append("document_id", elements.uploadDocumentId.value);
    }

    state.uploading = true;
    elements.uploadSubmit.disabled = true;
    elements.uploadError.textContent = "";
    startUploadProgress();
    try {
      const result = await api("/internal/v1/documents", { method: "POST", body: formData });
      stopUploadProgress(true);
      closeDialog(elements.uploadDialog);
      showToast(
        elements.uploadDocumentId.value ? "新版本已发布" : "文档已入库",
        `v${result.version_number} · ${formatNumber(result.chunk_count)} 个知识块 · ${result.index_mode}`,
      );
      switchSection("documents");
      state.page = 0;
      await Promise.allSettled([loadDocuments(), loadOverview(), loadKnowledgeBases()]);
    } catch (error) {
      stopUploadProgress(false);
      elements.uploadError.textContent = error instanceof Error ? error.message : "上传失败";
      handleRequestError(error, "文档上传失败");
    } finally {
      state.uploading = false;
      elements.uploadSubmit.disabled = false;
    }
  }

  async function loadHealth() {
    elements.globalHealthText.textContent = "检查服务中";
    elements.globalHealthDot.className = "pulse-dot";
    try {
      const response = await fetch("/internal/v1/health/ready", { headers: { Accept: "application/json" } });
      const health = await response.json();
      renderHealth(health);
    } catch (_error) {
      renderHealth({ status: "DOWN", components: { database: "DOWN", object_store: "DOWN" } });
    }
  }

  function renderHealth(health) {
    const components = health.components || {};
    const rows = elements.healthList.querySelectorAll(".health-row");
    const statuses = [components.database, components.object_store];
    rows.forEach((row, index) => {
      const dot = row.querySelector(".status-dot");
      const isUp = statuses[index] === "UP";
      dot.className = `status-dot${isUp ? "" : " down"}`;
      dot.setAttribute("aria-label", isUp ? "正常" : "异常");
    });
    const allUp = health.status === "UP";
    elements.globalHealthDot.className = `pulse-dot ${allUp ? "up" : "down"}`;
    elements.globalHealthText.textContent = allUp ? "核心服务正常" : "服务状态异常";
  }

  function closeDialog(dialog) {
    if (dialog?.open) {
      dialog.close();
    }
    if (dialog === elements.detailDialog) {
      state.activeDocumentId = null;
    }
  }

  function switchSection(section) {
    document.querySelectorAll(".page-section").forEach((page) => {
      page.classList.toggle("active", page.dataset.page === section);
    });
    document.querySelectorAll(".nav-item").forEach((item) => {
      item.classList.toggle("active", item.dataset.section === section);
    });
    elements.breadcrumbCurrent.textContent = section === "documents" ? "知识文档" : "工作台";
    elements.sidebar.classList.remove("open");
    elements.sidebarBackdrop.classList.remove("open");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function findDocument(documentId) {
    return state.documents.get(documentId) || null;
  }

  function bindEvents() {
    elements.authDialog.addEventListener("cancel", (event) => event.preventDefault());
    elements.authForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const token = elements.tokenInput.value;
      const submit = elements.authForm.querySelector("button[type='submit']");
      if (!token) {
        elements.authError.textContent = "请输入管理员令牌。";
        return;
      }
      submit.disabled = true;
      elements.authError.textContent = "正在验证…";
      try {
        await authenticate(token);
        elements.tokenInput.value = "";
      } catch (error) {
        state.token = "";
        state.session = null;
        storeToken("");
        elements.authError.textContent = error instanceof ApiError && error.status === 401
          ? "管理员令牌无效，请检查后重试。"
          : `无法连接后台：${error instanceof Error ? error.message : "未知错误"}`;
      } finally {
        submit.disabled = false;
      }
    });

    elements.logoutButton.addEventListener("click", () => {
      state.token = "";
      state.session = null;
      storeToken("");
      closeDialog(elements.uploadDialog);
      closeDialog(elements.detailDialog);
      showAuth("已安全退出当前管理会话。");
    });

    document.querySelectorAll("[data-section]").forEach((button) => {
      button.addEventListener("click", () => switchSection(button.dataset.section));
    });
    document.querySelectorAll("[data-section-jump]").forEach((button) => {
      button.addEventListener("click", () => switchSection(button.dataset.sectionJump));
    });
    document.querySelectorAll("[data-action='upload']").forEach((button) => {
      button.addEventListener("click", () => openUploadDialog());
    });
    document.querySelectorAll("[data-close-dialog]").forEach((button) => {
      button.addEventListener("click", () => {
        if (!state.uploading) {
          closeDialog(document.querySelector(`#${button.dataset.closeDialog}`));
        }
      });
    });

    elements.mobileMenu.addEventListener("click", () => {
      elements.sidebar.classList.add("open");
      elements.sidebarBackdrop.classList.add("open");
    });
    elements.sidebarBackdrop.addEventListener("click", () => {
      elements.sidebar.classList.remove("open");
      elements.sidebarBackdrop.classList.remove("open");
    });

    elements.knowledgeBaseList.addEventListener("click", (event) => {
      const item = event.target.closest("[data-kb-id]");
      if (!item) return;
      elements.knowledgeFilter.value = item.dataset.kbId;
      state.page = 0;
      switchSection("documents");
      loadDocuments();
    });

    elements.documentSearch.addEventListener("input", () => {
      window.clearTimeout(state.searchTimer);
      state.searchTimer = window.setTimeout(() => {
        state.page = 0;
        loadDocuments();
      }, 320);
    });
    [elements.knowledgeFilter, elements.statusFilter].forEach((select) => {
      select.addEventListener("change", () => {
        state.page = 0;
        loadDocuments();
      });
    });
    elements.refreshDocuments.addEventListener("click", () => loadDocuments());
    elements.refreshHealth.addEventListener("click", () => loadHealth());
    elements.previousPage.addEventListener("click", () => {
      if (state.page > 0) {
        state.page -= 1;
        loadDocuments();
      }
    });
    elements.nextPage.addEventListener("click", () => {
      if ((state.page + 1) * PAGE_SIZE < state.totalDocuments) {
        state.page += 1;
        loadDocuments();
      }
    });

    elements.documentTableBody.addEventListener("click", (event) => {
      const trigger = event.target.closest("[data-doc-action]");
      if (!trigger) return;
      const documentId = trigger.dataset.documentId;
      if (trigger.dataset.docAction === "detail") {
        openDocumentDetail(documentId);
      } else if (trigger.dataset.docAction === "version") {
        openUploadDialog(findDocument(documentId));
      } else if (trigger.dataset.docAction === "state") {
        setDocumentState(documentId, trigger.dataset.active === "true", trigger);
      }
    });

    elements.detailContent.addEventListener("click", (event) => {
      const trigger = event.target.closest("[data-detail-action]");
      if (!trigger) return;
      const documentId = trigger.dataset.documentId;
      if (trigger.dataset.detailAction === "version") {
        const documentItem = findDocument(documentId);
        closeDialog(elements.detailDialog);
        openUploadDialog(documentItem);
      } else if (trigger.dataset.detailAction === "state") {
        setDocumentState(documentId, trigger.dataset.active === "true", trigger);
      }
    });

    elements.uploadDialog.addEventListener("cancel", (event) => {
      if (state.uploading) {
        event.preventDefault();
      }
    });
    elements.uploadForm.addEventListener("submit", submitUpload);
    elements.uploadFile.addEventListener("change", () => updateSelectedFile(elements.uploadFile.files[0]));
    ["dragenter", "dragover"].forEach((name) => {
      elements.dropZone.addEventListener(name, (event) => {
        event.preventDefault();
        elements.dropZone.classList.add("dragging");
      });
    });
    ["dragleave", "drop"].forEach((name) => {
      elements.dropZone.addEventListener(name, (event) => {
        event.preventDefault();
        elements.dropZone.classList.remove("dragging");
      });
    });
    elements.dropZone.addEventListener("drop", (event) => {
      const files = event.dataTransfer?.files;
      if (!files?.length) return;
      elements.uploadFile.files = files;
      updateSelectedFile(files[0]);
    });
  }

  async function initialize() {
    bindEvents();
    const storedToken = readStoredToken();
    if (!storedToken) {
      showAuth();
      return;
    }
    try {
      await authenticate(storedToken);
    } catch (error) {
      state.token = "";
      state.session = null;
      storeToken("");
      const message = error instanceof ApiError && error.status === 401
        ? "上次登录已失效，请重新输入管理员令牌。"
        : "后台暂时无法连接，请确认 Agent HTTP 服务已启动。";
      showAuth(message);
    }
  }

  initialize();
})();
