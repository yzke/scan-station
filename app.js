'use strict';

(() => {
  const ids = ['deviceBadge', 'deviceLabel', 'deviceMessage', 'deviceCaptureMode', 'btnDeviceRefresh', 'notice',
    'activeBanner', 'activeMessage', 'btnViewActive', 'btnAbandonScan', 'fileState', 'fileName', 'nameState', 'nameHelp',
    'dpi', 'dpiHelp', 'scanMode', 'modeHelp', 'btnScan', 'btnStartScan', 'btnContinue', 'scanActionHelp', 'currentHeading', 'status', 'count',
    'btnPdf', 'grid', 'empty', 'emptyTitle', 'emptyText', 'historyCount', 'history',
    'historyEmpty', 'btnHistoryRefresh', 'historyPager', 'btnHistoryPrev', 'btnHistoryNext', 'historyPageLabel', 'nameDialog', 'nameForm', 'nameDialogTitle',
    'dialogName', 'nameDialogHelp', 'dialogError', 'btnNameClose', 'btnNameCancel',
    'btnNameSubmit', 'previewDialog', 'pageDialogTitle', 'pageImage', 'originalLink',
    'btnPreviewClose', 'pageTools', 'btnReverse', 'rescanHelp', 'orderStatus',
    'rescanDialog', 'rescanForm', 'rescanTitle', 'rescanOptions', 'rescanError',
    'btnRescanClose', 'btnRescanCancel', 'btnRescanConfirm',
    'blankTools', 'btnMarkBlank', 'btnDeleteSelected', 'btnBlankUndo', 'blankStatus',
    'renderModes', 'renderModeHelp'];
  const ui = Object.fromEntries(ids.map(id => [id, document.getElementById(id)]));
  const documents = new Map();
  const removedDocuments = new Set();
  const historyPageSize = 10;
  let historyPage = 0;
  const pageNodes = new Map();
  const historyNodes = new Map();
  const drafts = new Map();
  const nameSaves = new Map();
  const nameLocks = new Map();
  const manualIntents = new Set();
  const manualLocks = new Set();
  const storageKey = 'scanStationSelectedDocument';
  let selectedId = storedSelection(), activeId = null, renderedId = null;
  let scanner = {state: 'unknown'}, listLoaded = false, busy = false;
  let documentsRequest = null, scannerRequest = null, scannerChecked = 0;
  let mutationEpoch = 0, mutationCount = 0, nameTimer = null, pollTimer = null;
  let dialogAction = null, listError = false;
  let newNameEdited = false, creatingDocument = false;
  let dragState = null, rescanTarget = null, requestedRescan = null;
  let pageSelection = null, blankOperation = null, blankFeedback = null;
  let draftRenderMode = 'original';
  const renderModeLabels = {original: '原图', enhanced: '增强', bw: '黑白'};

  function storedSelection() {
    try { return localStorage.getItem(storageKey) || sessionStorage.getItem('scanSid'); }
    catch (_) { return null; }
  }

  function rememberSelection() {
    try {
      if (selectedId) localStorage.setItem(storageKey, selectedId);
      else localStorage.removeItem(storageKey);
      sessionStorage.removeItem('scanSid');
    } catch (_) { /* The workspace also works when browser storage is disabled. */ }
  }

  function defaultName() {
    const now = new Date();
    const pad = n => String(n).padStart(2, '0');
    return '扫描文件 ' + now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' +
      pad(now.getDate()) + ' ' + pad(now.getHours()) + pad(now.getMinutes());
  }

  function pathFor(id, suffix) {
    return '/scan/' + encodeURIComponent(id) + '/' + suffix;
  }

  async function request(path, options = {}, timeout = 20000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, {...options, cache: 'no-store', signal: controller.signal});
      if (!response.ok) {
        let payload = {};
        try { payload = await response.json(); } catch (_) { /* Non-JSON server error. */ }
        const error = new Error(payload.error || (response.status === 404 ? '文件不存在，请刷新文件记录。' : '操作未完成，请稍后重试。'));
        error.status = response.status;
        error.payload = payload;
        throw error;
      }
      return response;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('连接超时，请检查网络后重试。');
      if (error instanceof TypeError) throw new Error('无法连接扫描站，请检查网络后重试。');
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  async function getJson(path) {
    return (await request(path)).json();
  }

  async function mutate(path, body = {}, options = {}) {
    mutationCount++;
    mutationEpoch++;
    try {
      return await (await request(path, {
        ...options, method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
      })).json();
    } finally {
      mutationCount--;
      mutationEpoch++;
    }
  }

  function notify(message, isError = false) {
    ui.notice.textContent = message;
    ui.notice.className = 'notice' + (isError ? ' error' : '');
    ui.notice.hidden = !message;
  }

  function handleError(error) {
    if (error.status === 409 && error.payload && error.payload.active_id) {
      activeId = String(error.payload.active_id);
    }
    notify(error.message || '操作未完成，请重试。', true);
    render();
    if (error.status === 404 || error.status === 409) void refreshDocuments();
  }

  async function runAction(action) {
    if (busy) return;
    busy = true;
    render();
    try { await action(); }
    catch (error) { handleError(error); }
    finally { busy = false; render(); }
  }

  function timestampKey(value) {
    const text = String(value || '');
    const utc = text.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(?:Z|\+00:00)$/);
    if (utc) return utc[1] + '.' + (utc[2] || '').padEnd(6, '0') + 'Z';
    const milliseconds = Date.parse(text);
    return Number.isFinite(milliseconds) ? new Date(milliseconds).toISOString().replace(/Z$/, '000Z') : '';
  }

  function putDocument(value) {
    if (!value || typeof value.id !== 'string' || removedDocuments.has(value.id)) return null;
    const previous = documents.get(value.id);
    const incomingTime = timestampKey(value.updated_at);
    const previousTime = timestampKey(previous?.updated_at);
    if (previous && incomingTime < previousTime) return previous;
    const doc = {...value, pages: [...new Set((value.pages || []).filter(Number.isInteger))]};
    if (value.name_source === 'manual') manualLocks.add(value.id);
    // A late OCR snapshot must not replace a name the user is already editing.
    if (manualIntents.has(value.id) && previous && value.name_source !== 'manual') {
      doc.name = previous.name;
      doc.name_source = 'manual';
      doc.auto_name = false;
    }
    documents.set(doc.id, doc);
    syncPageSelection(doc);
    return doc;
  }

  function displayName(doc) {
    const draft = drafts.get(doc.id);
    return draft && draft.value.trim() ? draft.value.trim() : doc.name || '未命名文件';
  }

  function chooseDocument(id) {
    clearDrag();
    if (id !== selectedId) { pageSelection = null; blankFeedback = null; }
    selectedId = id;
    rememberSelection();
    const doc = documents.get(id);
    if (doc) {
      newNameEdited = false;
      historyPage = Math.floor(sortedHistory().findIndex(item => item.id === id) / historyPageSize);
      ui.scanMode.value = doc.duplex && scanner.duplex_supported === true ? 'duplex' : 'simplex';
    }
    if (ui.previewDialog.open) ui.previewDialog.close();
    render();
  }

  async function openDocument(id) {
    if (id === selectedId) return;
    await runAction(async () => {
      await saveDraft(selectedId);
      if (!documents.has(id)) throw new Error('文件记录已变更，请刷新后再打开。');
      chooseDocument(id);
      notify('');
    });
  }

  async function refreshDocuments() {
    if (documentsRequest) return documentsRequest;
    documentsRequest = (async () => {
      const epoch = mutationEpoch;
      try {
        const result = await getJson('/documents');
        if (!Array.isArray(result.documents)) throw new Error('文件记录读取失败，请刷新后重试。');
        if (epoch !== mutationEpoch || mutationCount) return;
        const incomingIds = new Set();
        for (const doc of result.documents) {
          if (putDocument(doc)) incomingIds.add(doc.id);
        }
        for (const id of documents.keys()) {
          if (!incomingIds.has(id)) {
            removedDocuments.add(id);
            documents.delete(id);
            drafts.delete(id);
            manualIntents.delete(id);
            manualLocks.delete(id);
            if (pageSelection?.id === id) pageSelection = null;
          }
        }
        activeId = result.active_id ? String(result.active_id) : null;
        const missing = listLoaded && selectedId && !documents.has(selectedId);
        if (!listLoaded || missing) {
          listLoaded = true;
          if (!documents.has(selectedId)) {
            chooseDocument(documents.has(activeId) ? activeId : result.documents[0]?.id || null);
            if (missing) notify('当前文件已不存在，已刷新文件记录。', true);
          } else {
            chooseDocument(selectedId);
          }
        }
        if (listError) { listError = false; notify('已重新连接，文件记录已更新。'); }
        render();
      } catch (error) {
        listError = true;
        notify(error.message, true);
        render();
      }
    })().finally(() => { documentsRequest = null; });
    return documentsRequest;
  }

  async function refreshScanner() {
    if (scannerRequest) return scannerRequest;
    ui.btnDeviceRefresh.disabled = true;
    scannerRequest = (async () => {
      try {
        scanner = await getJson('/scanner/status');
        scannerChecked = Date.now();
      } catch (error) {
        scanner = {state: 'unknown', message: error.message};
        scannerChecked = Date.now();
      }
      updateCapabilities();
      render();
    })().finally(() => {
      scannerRequest = null;
      ui.btnDeviceRefresh.disabled = false;
    });
    return scannerRequest;
  }

  function reportedDpi() {
    // Empty capability lists also represent missing or stale agent heartbeats.
    if (!['online', 'scanning'].includes(scanner.state) || !Array.isArray(scanner.supported_dpi)) return null;
    const values = scanner.supported_dpi.filter(value => Number.isInteger(value) && value > 0);
    return values.length ? values : null;
  }

  function updateCapabilities() {
    const capabilities = reportedDpi();
    const reported = capabilities ? [150, 200, 300].filter(dpi => capabilities.includes(dpi)) : null;
    const choices = reported && reported.length ? reported : [150];
    const oldValue = Number(ui.dpi.dataset.preferred || 300);
    const signature = choices.join(',');
    if (ui.dpi.dataset.choices !== signature) {
      ui.dpi.replaceChildren(...choices.map(dpi => new Option(dpi + ' DPI', String(dpi))));
      ui.dpi.dataset.choices = signature;
      ui.dpi.value = String(choices.includes(oldValue) ? oldValue : choices[0]);
    }
    ui.dpiHelp.textContent = reported ? (reported.length ? '选择本次扫描的清晰度' : '未检测到可用清晰度') : '设备信息检测中，暂用 150 DPI';
    ui.scanMode.options[1].disabled = scanner.duplex_supported !== true;
    if (scanner.duplex_supported !== true) ui.scanMode.value = 'simplex';
    ui.modeHelp.textContent = scanner.duplex_supported === true ? '选择本次扫描的面数' :
      scanner.duplex_supported === false ? '此设备仅支持单面' : '正在检测双面能力';
  }

  function hasActiveScan() {
    return !!activeId || scanner.state === 'scanning';
  }

  function settingsUnavailable() {
    const capabilities = reportedDpi();
    return capabilities !== null && !capabilities.includes(Number(ui.dpi.value));
  }

  function deviceProblem() {
    // Mirrors scanner_status.readiness_problem. A scan request reserves the
    // scanner until the agent reports a terminal status, so starting one while
    // the computer or its agent is unreachable would lock the station. The
    // server refuses these too; showing it here explains the disabled button.
    if (!scannerChecked) return '正在确认扫描电脑与扫描仪状态…';
    if (scanner.host_reachable !== true) {
      return '无法连接扫描电脑（未开机或网络不通），请开机并确认扫描代理运行后再扫描。';
    }
    if (scanner.heartbeat_fresh !== true) {
      return '扫描电脑已连接，但扫描代理没有运行，请在该电脑上启动 run-agent.cmd 后再扫描。';
    }
    if (!['online', 'scanning'].includes(scanner.state)) {
      return scanner.message || '扫描仪当前不可用，请检查电源与 USB 连接后刷新状态。';
    }
    return null;
  }

  function assertCanScan() {
    if (hasActiveScan()) throw new Error('扫描仪正在处理另一批纸张，请等待完成。');
    const problem = deviceProblem();
    if (problem) throw new Error(problem);
    if (settingsUnavailable()) throw new Error('尚未检测到可用清晰度，请刷新扫描仪状态。');
  }

  function renderDevice() {
    const state = activeId ? 'scanning' : scanner.state || 'unknown';
    const labels = {online: '扫描仪已连接', offline: '扫描仪未连接', unknown: '连接状态未知', scanning: '扫描仪工作中'};
    ui.deviceBadge.className = 'badge ' + (labels[state] ? state : 'unknown');
    ui.deviceLabel.textContent = labels[state] || labels.unknown;
    const blocked = activeId ? null : deviceProblem();
    ui.deviceMessage.textContent = state === 'scanning' ? '正在处理送纸器中的纸张，请等待本批扫描完成' :
      blocked || scanner.message || (
      state === 'online' ? '可以开始扫描' : state === 'offline' ? '请检查扫描仪电源与连接' :
      state === 'scanning' ? '正在处理送纸器中的纸张' : '暂时无法确认连接状态，可稍后刷新');
    const captureModes = {'wia2-batch': '连续进纸', 'wia-automation-compat': '逐页兼容模式',
      'wia2-preferred': '等待扫描时确认'};
    const captureMode = ['online', 'scanning'].includes(scanner.state) && captureModes[scanner.capture_mode];
    let captureText = captureMode ? '进纸方式：' + captureMode : '';
    const detail = typeof scanner.capture_mode_detail === 'string' ? scanner.capture_mode_detail.trim() : '';
    // Display short explanations written for people, not native interface errors.
    if (captureMode && scanner.capture_mode === 'wia-automation-compat' && detail && detail.length <= 72 && !/[A-Za-z]/.test(detail)) {
      captureText += ' · ' + detail;
    }
    ui.deviceCaptureMode.textContent = captureText;
    ui.deviceCaptureMode.hidden = !captureText;
  }

  function renderActive() {
    const doc = documents.get(activeId);
    ui.activeBanner.hidden = !hasActiveScan();
    ui.btnViewActive.hidden = !doc || selectedId === activeId;
    ui.btnViewActive.disabled = busy;
    // The forced end is the only way out of a batch whose scanner never reports
    // a terminal status, so it stays reachable whenever the station is reserved.
    ui.btnAbandonScan.hidden = !activeId;
    ui.btnAbandonScan.disabled = busy;
    if (doc) {
      const rescanIndex = doc.pages.indexOf(doc.rescan_page);
      ui.activeMessage.textContent = rescanIndex >= 0 ? '「' + displayName(doc) + '」正在重扫第 ' + (rescanIndex + 1) + ' 页 · 原页保留' : '「' + displayName(doc) + '」' +
        (doc.state === 'error' ? '等待扫描仪结束本批任务' : '正在扫描') + ' · 已收到 ' + doc.pages.length + ' 页';
    } else {
      ui.activeMessage.textContent = '扫描仪正在处理纸张，请等待本批扫描完成';
    }
  }

  function renderName() {
    const doc = documents.get(selectedId);
    if (!doc) {
      if (!ui.fileName.value && !newNameEdited) ui.fileName.value = defaultName();
      ui.nameState.textContent = creatingDocument ? '正在创建…' : newNameEdited ? '手动名称' : '默认名称';
      ui.nameHelp.textContent = newNameEdited ? '此名称将用于新文件，扫描后不再自动改名' : '扫描后自动识别第一页标题，也可手动修改';
      return;
    }
    const draft = drafts.get(selectedId);
    const value = draft ? draft.value : doc.name || '';
    if (ui.fileName.value !== value) ui.fileName.value = value;
    const automatic = doc.auto_name === true && !manualIntents.has(selectedId);
    const recognizing = automatic && ['pending', 'running'].includes(doc.ocr_state);
    ui.nameState.textContent = draft?.error ? '保存失败' :
      nameLocks.has(selectedId) || nameSaves.has(selectedId) ? '保存中…' : draft?.dirty ? '待保存' :
      recognizing ? (doc.pages.length ? '识别名称中…' : '等待第一页') :
      doc.name_source === 'ocr' ? '自动命名' :
      automatic && doc.name_source === 'default' ? '默认名称' : '已保存';
    ui.nameState.className = 'field-note' + (draft?.error ? ' error' : '');
    ui.nameHelp.textContent = automatic ? '第一页扫描后自动命名；手动修改后不再自动更改' : '此名称用于下载 PDF，手动修改后不再自动更改';
  }

  function renderSelected() {
    const doc = documents.get(selectedId);
    renderName();
    const state = doc?.state || '';
    const scanning = !!doc && (doc.id === activeId || state === 'scanning');
    ui.fileState.className = 'file-state ' + (scanning ? 'scanning' : state);
    ui.fileState.textContent = scanning ? (state === 'error' ? '等待任务结束' : '正在扫描') :
      state === 'done' ? '已保存' : state === 'error' ? '扫描未完成' :
      state === 'cancelled' ? '已终止' : '准备扫描';
    let message = !listLoaded ? '正在读取文件记录…' :
      !doc ? (creatingDocument ? '正在创建文件并开始扫描…' : '确认文件名称、清晰度和单双面，放好纸张后点击「开始扫描」。') :
      scanning ? '已收到 ' + doc.pages.length + ' 页，新页面将继续显示在下方。' :
      state === 'error' ? '扫描未完成：' + (doc.msg || '请检查纸张与扫描仪连接后重试。') :
      state === 'cancelled' ? (doc.msg || '本批扫描已强制终止。') :
      '已保存 ' + doc.pages.length + ' 页，可继续扫描追加页面，或下载 PDF。';
    if (doc?.state === 'error' && scanning) message = (doc.msg || '扫描任务尚未结束。') + ' 已收到的页面仍会保留，请等待扫描仪结束任务。';
    if (scanning && Number.isInteger(doc.rescan_page)) {
      const index = doc.pages.indexOf(doc.rescan_page) + 1;
      message = doc.state === 'error' ? (doc.msg || '重扫尚未完成。') + ' 原页仍然保留，请等待本批任务结束。' :
        '正在重扫第 ' + index + ' 页。原页会继续显示，成功后只替换这一页。';
    }
    ui.status.textContent = message;
    ui.status.className = 'status ' + (state === 'error' ? 'error' : scanning ? 'scanning' : state);
    ui.count.textContent = (doc?.pages.length || 0) + ' 页';
    ui.empty.hidden = !!doc?.pages.length;
    ui.emptyTitle.textContent = scanning ? '正在等待第一张页面' : doc ? '此文件还没有页面' : '新文件尚未开始扫描';
    ui.emptyText.textContent = scanning ? '完成一页后就会显示预览，无需等整批扫描结束' :
      doc ? '放入纸张，点击「继续扫描」即可追加页面' : '在上方设置好文件名称和扫描选项，再点击「开始扫描」';
    reconcilePages(doc);
  }

  function pageUrl(doc, number, original = false) {
    const revision = doc.page_details?.[number]?.revision;
    const query = new URLSearchParams();
    if (revision != null) query.set('v', revision);
    if (!original) {
      query.set('mode', doc.render_mode || 'original');
      if (doc.render_version != null) query.set('ev', doc.render_version);
    }
    return pathFor(doc.id, (original ? 'original/' : 'page/') + number) + (query.size ? '?' + query : '');
  }

  function renderModes(doc) {
    const mode = doc ? doc.render_mode || 'original' : draftRenderMode;
    for (const button of ui.renderModes.querySelectorAll('button')) {
      button.setAttribute('aria-pressed', String(button.dataset.mode === mode));
      button.disabled = busy || !listLoaded;
    }
    const descriptions = {original: '保留扫描色彩，使用去黑边后的页面',
      enhanced: '纸面增白，保留彩色字迹与印章', bw: '纸面变白，字迹与印章转为黑白'};
    ui.renderModeHelp.textContent = descriptions[mode] + ' · 整份文件的预览和 PDF 同步使用';
  }

  async function setRenderMode(mode) {
    if (!Object.hasOwn(renderModeLabels, mode)) return;
    await runAction(async () => {
      const id = selectedId;
      const doc = documents.get(id);
      if (!doc) { draftRenderMode = mode; return; }
      if ((doc.render_mode || 'original') === mode) return;
      await saveDraft(id);
      // Page arrival and OCR both advance updated_at during a live scan.
      // Refresh before the reversible preference change; retry one such race.
      for (let attempt = 0; attempt < 2; attempt++) {
        const latest = await getJson(pathFor(id, 'status'));
        if (!putDocument(latest)) throw new Error('文件已不存在，请刷新文件记录。');
        try {
          const updated = await mutate(pathFor(id, 'set-render-mode'), {render_mode: mode, updated_at: latest.updated_at});
          if (!putDocument(updated)) throw new Error('文件已不存在，请刷新文件记录。');
          notify('已切换为「' + renderModeLabels[mode] + '」，预览正在更新，下载 PDF 也将使用此模式。');
          return;
        } catch (error) {
          if (error.status !== 409 || attempt) throw error;
        }
      }
    });
  }

  function pageEditingLocked(doc) {
    return busy || hasActiveScan() || !doc || doc.state === 'scanning';
  }

  function pageSnapshot(doc) {
    if (!doc) return null;
    return {order_revision: Number.isInteger(doc.order_revision) ? doc.order_revision : 0,
      page_revisions: Object.fromEntries(doc.pages.map(number => [String(number), doc.page_details?.[number]?.revision ?? 1]))};
  }

  function pageSnapshotKey(doc) {
    if (!doc) return '';
    const snapshot = pageSnapshot(doc);
    return JSON.stringify([doc.id, doc.render_mode || 'original', doc.render_version,
      snapshot.order_revision, doc.pages.map(number => [number, snapshot.page_revisions[number]])]);
  }

  function setBlankFeedback(id, text, error = false) {
    blankFeedback = {id, text, error};
  }

  function syncPageSelection(doc) {
    if (!pageSelection || pageSelection.id !== doc?.id || pageSelection.key === pageSnapshotKey(doc)) return;
    const hadSelection = pageSelection.pages.size > 0;
    pageSelection = null;
    if (hadSelection) setBlankFeedback(doc.id, '页面已更新，旧勾选已清除，请重新选择。');
  }

  function selectPage(id, number, checked) {
    const doc = documents.get(id);
    if (selectedId !== id || pageEditingLocked(doc) || !doc.pages.includes(number)) return;
    syncPageSelection(doc);
    if (!pageSelection) pageSelection = {id, key: pageSnapshotKey(doc), pages: new Set()};
    if (checked) pageSelection.pages.add(number);
    else pageSelection.pages.delete(number);
    if (blankFeedback?.id === id && blankFeedback.error) blankFeedback = null;
    render();
  }

  function renderBlankTools(doc) {
    const selected = pageSelection && pageSelection.id === doc?.id ? pageSelection.pages.size : 0;
    const undo = doc?.blank_undo;
    const locked = pageEditingLocked(doc);
    ui.blankTools.hidden = !doc || (!doc.pages.length && !undo?.cleanup_id);
    ui.btnMarkBlank.disabled = locked || !doc?.pages.length;
    ui.btnMarkBlank.textContent = blankOperation?.id === doc?.id && blankOperation?.type === 'analysis' ? '正在标记…' : '一键标记空白页';
    ui.btnDeleteSelected.disabled = locked || !selected;
    ui.btnDeleteSelected.textContent = '删除选中页（' + selected + '）';
    ui.btnBlankUndo.hidden = !undo?.cleanup_id;
    ui.btnBlankUndo.disabled = locked;
    ui.btnBlankUndo.textContent = '撤销删除' + (undo?.count ? '（' + undo.count + '）' : '');
    const feedback = blankFeedback?.id === doc?.id ? blankFeedback : null;
    const message = feedback?.text || '自动标记后，可在页面卡上补选或取消勾选；点击删除才会移除页面。';
    if (ui.blankStatus.textContent !== message) ui.blankStatus.textContent = message;
    ui.blankStatus.classList.toggle('error', !!feedback?.error);
  }

  function rescanSupported() {
    return scanner.supports_page_rescan === true;
  }

  function clearDrag() {
    dragState = null;
    for (const node of pageNodes.values()) node.card.classList.remove('dragging', 'drop-before', 'drop-after');
  }

  function markDrop(event, docId, number, card) {
    if (!dragState || dragState.id !== docId || dragState.number === number ||
        pageEditingLocked(documents.get(docId))) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    const bounds = card.getBoundingClientRect();
    const after = event.clientX >= bounds.left + bounds.width / 2;
    for (const node of pageNodes.values()) node.card.classList.remove('drop-before', 'drop-after');
    card.classList.add(after ? 'drop-after' : 'drop-before');
    dragState.target = number;
    dragState.after = after;
  }

  function reconcilePages(doc) {
    syncPageSelection(doc);
    if (renderedId !== selectedId) {
      ui.grid.replaceChildren();
      pageNodes.clear();
      renderedId = selectedId;
    }
    const pages = doc?.pages || [];
    const selected = pageSelection && pageSelection.id === doc?.id ? pageSelection.pages : new Set();
    const remaining = new Set(pages);
    for (const [number, node] of pageNodes) {
      if (!remaining.has(number)) { node.card.remove(); pageNodes.delete(number); }
    }
    pages.forEach((number, index) => {
      let node = pageNodes.get(number);
      if (!node) {
        const id = doc.id;
        const card = document.createElement('article');
        card.className = 'pg';
        card.dataset.page = number;
        const top = document.createElement('div');
        top.className = 'page-topbar';
        const handle = document.createElement('button');
        handle.type = 'button';
        handle.className = 'drag-handle';
        handle.textContent = '⠿';
        const label = document.createElement('span');
        label.className = 'page-label';
        const pick = document.createElement('label');
        pick.className = 'page-pick';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'page-check';
        checkbox.addEventListener('change', () => selectPage(id, number, checkbox.checked));
        pick.append(checkbox, label);
        const moves = document.createElement('div');
        moves.className = 'page-moves';
        const up = document.createElement('button');
        up.type = 'button';
        up.className = 'move-button move-up';
        up.textContent = '↑';
        const down = document.createElement('button');
        down.type = 'button';
        down.className = 'move-button move-down';
        down.textContent = '↓';
        up.addEventListener('click', () => movePage(id, number, -1));
        down.addEventListener('click', () => movePage(id, number, 1));
        handle.addEventListener('keydown', event => {
          if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
            event.preventDefault();
            void movePage(id, number, event.key === 'ArrowUp' ? -1 : 1);
          }
        });
        handle.addEventListener('dragstart', event => {
          if (pageEditingLocked(documents.get(id))) { event.preventDefault(); return; }
          dragState = {id, number};
          event.dataTransfer.effectAllowed = 'move';
          event.dataTransfer.setData('text/plain', String(number));
          event.dataTransfer.setDragImage(card, 25, 20);
          card.classList.add('dragging');
        });
        handle.addEventListener('dragend', clearDrag);
        card.addEventListener('dragover', event => markDrop(event, id, number, card));
        card.addEventListener('dragleave', event => {
          if (!card.contains(event.relatedTarget)) card.classList.remove('drop-before', 'drop-after');
        });
        card.addEventListener('drop', event => {
          const drag = dragState;
          if (!drag || drag.id !== id || drag.number === number || pageEditingLocked(documents.get(id))) return;
          event.preventDefault();
          const pages = documents.get(id).pages.filter(page => page !== drag.number);
          const bounds = card.getBoundingClientRect();
          const after = event.clientX >= bounds.left + bounds.width / 2;
          pages.splice(pages.indexOf(number) + (after ? 1 : 0), 0, drag.number);
          clearDrag();
          void savePageOrder(id, pages);
        });
        moves.append(up, down);
        top.append(handle, pick, moves);
        const open = document.createElement('button');
        open.type = 'button';
        open.className = 'page-open';
        const image = document.createElement('img');
        image.loading = 'lazy';
        image.decoding = 'async';
        image.draggable = false;
        open.append(image);
        open.addEventListener('click', () => previewPage(id, number));
        const bar = document.createElement('div');
        bar.className = 'page-bar';
        const rescan = document.createElement('button');
        rescan.type = 'button';
        rescan.className = 'rescan-button';
        rescan.textContent = '重扫本页';
        rescan.addEventListener('click', () => showRescanDialog(id, number));
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'delete-button';
        remove.textContent = '删除';
        remove.addEventListener('click', () => deletePage(id, number));
        const pageState = document.createElement('p');
        pageState.className = 'page-state';
        pageState.setAttribute('role', 'status');
        bar.append(rescan, remove);
        card.append(top, open, bar, pageState);
        node = {card, image, open, label, checkbox, remove, handle, up, down, rescan, pageState};
        pageNodes.set(number, node);
      }
      const label = '第 ' + (index + 1) + ' 页';
      node.label.textContent = label;
      const imageUrl = pageUrl(doc, number);
      if (node.image.getAttribute('src') !== imageUrl) node.image.src = imageUrl;
      node.image.alt = label + '扫描预览';
      node.open.setAttribute('aria-label', '查看' + label);
      node.remove.setAttribute('aria-label', '删除' + label);
      const locked = pageEditingLocked(doc);
      node.checkbox.checked = selected.has(number);
      node.checkbox.disabled = locked;
      node.checkbox.setAttribute('aria-label', '选择' + label);
      node.card.classList.toggle('page-selected', node.checkbox.checked);
      node.remove.disabled = locked;
      node.up.disabled = locked || index === 0;
      node.down.disabled = locked || index === pages.length - 1;
      node.up.setAttribute('aria-label', label + '上移');
      node.down.setAttribute('aria-label', label + '下移');
      node.handle.disabled = locked || pages.length < 2;
      node.handle.draggable = !node.handle.disabled;
      node.handle.setAttribute('aria-label', '拖动' + label + '排序');
      node.handle.title = '拖动排序，也可按 ↑↓ 移动';
      node.rescan.disabled = locked || !rescanSupported() || scanner.state !== 'online';
      node.rescan.setAttribute('aria-label', '重扫' + label);
      node.rescan.title = !rescanSupported() ? '更新扫描仪程序并刷新连接状态后可用' : '只重新扫描并替换这一页';
      const replacing = doc.rescan_page === number && (doc.state === 'scanning' || doc.id === activeId) ||
        requestedRescan?.id === doc.id && requestedRescan.number === number;
      node.card.classList.toggle('replacing', !!replacing);
      node.pageState.hidden = !replacing;
      node.pageState.textContent = '重扫中，当前显示原页';
      // Keep existing image nodes in place while new pages arrive.
      if (ui.grid.children[index] !== node.card) ui.grid.insertBefore(node.card, ui.grid.children[index] || null);
    });
  }

  function shortDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    return new Intl.DateTimeFormat('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false}).format(date);
  }

  function sortedHistory() {
    return [...documents.values()].sort((a, b) => timestampKey(b.updated_at || b.created_at).localeCompare(timestampKey(a.updated_at || a.created_at)) || b.id.localeCompare(a.id));
  }

  function renderHistory() {
    const sorted = sortedHistory();
    const totalPages = Math.max(1, Math.ceil(sorted.length / historyPageSize));
    historyPage = Math.max(0, Math.min(historyPage, totalPages - 1));
    const visible = sorted.slice(historyPage * historyPageSize, (historyPage + 1) * historyPageSize);
    const visibleIds = new Set(visible.map(doc => doc.id));
    ui.historyCount.textContent = sorted.length;
    ui.historyPager.hidden = sorted.length <= historyPageSize;
    ui.historyPageLabel.textContent = '第 ' + (historyPage + 1) + ' / ' + totalPages + ' 页';
    ui.btnHistoryPrev.disabled = busy || historyPage === 0;
    ui.btnHistoryNext.disabled = busy || historyPage >= totalPages - 1;
    ui.historyEmpty.hidden = !!sorted.length;
    ui.historyEmpty.textContent = listError ? '暂时无法读取文件记录，请刷新' : listLoaded ? '还没有文件记录' : '正在读取文件记录…';
    for (const [id, node] of historyNodes) {
      if (!visibleIds.has(id)) { node.entry.remove(); historyNodes.delete(id); }
    }
    visible.forEach((doc, index) => {
      let node = historyNodes.get(doc.id);
      if (!node) {
        const id = doc.id;
        const entry = document.createElement('article');
        entry.className = 'history-entry';
        entry.dataset.id = id;
        const open = document.createElement('button');
        open.type = 'button';
        open.className = 'history-open';
        const title = document.createElement('span');
        title.className = 'history-title';
        const meta = document.createElement('span');
        meta.className = 'history-meta';
        const detail = document.createElement('span');
        const state = document.createElement('span');
        state.className = 'history-state';
        meta.append(detail, state);
        open.append(title, meta);
        open.addEventListener('click', () => openDocument(id));
        const actions = document.createElement('div');
        actions.className = 'history-actions';
        const buttons = ['打开', '重命名', '下载 PDF', '删除'].map(text => {
          const button = document.createElement('button');
          button.type = 'button';
          button.textContent = text;
          actions.append(button);
          return button;
        });
        buttons[0].addEventListener('click', () => openDocument(id));
        buttons[1].addEventListener('click', () => runAction(async () => {
          await saveDraft(selectedId);
          showNameDialog('rename', id);
        }));
        buttons[2].addEventListener('click', () => downloadDocument(id));
        buttons[3].className = 'history-delete';
        buttons[3].addEventListener('click', () => { void deleteHistoryDocument(id); });
        entry.append(open, actions);
        node = {entry, open, title, detail, state, buttons};
        historyNodes.set(id, node);
      }
      const scanning = doc.id === activeId || doc.state === 'scanning';
      node.entry.className = 'history-entry' + (doc.id === selectedId ? ' selected' : '');
      node.open.setAttribute('aria-label', '打开文件：' + displayName(doc));
      node.open.setAttribute('aria-current', doc.id === selectedId ? 'true' : 'false');
      node.open.disabled = busy;
      node.title.textContent = displayName(doc);
      node.title.title = displayName(doc);
      node.detail.textContent = shortDate(doc.created_at) + ' · ' + doc.pages.length + ' 页';
      node.state.textContent = scanning ? '扫描中' : doc.state === 'error' ? '未完成' :
        doc.state === 'cancelled' ? '已终止' : doc.id === selectedId ? '当前文件' : '已保存';
      node.state.className = 'history-state ' + (scanning ? 'scanning' : doc.state || '');
      node.buttons.forEach(button => { button.disabled = busy; });
      node.buttons[2].disabled = busy || !doc.pages.length;
      node.buttons[3].disabled = busy || scanning || !!doc.active_batch;
      node.buttons[3].setAttribute('aria-label', '删除文件：' + displayName(doc));
      if (ui.history.children[index] !== node.entry) ui.history.insertBefore(node.entry, ui.history.children[index] || null);
    });
  }

  async function deleteHistoryDocument(id) {
    if (busy || !documents.has(id)) return;
    await runAction(async () => {
      await saveDraft(id);
      const doc = documents.get(id);
      if (!doc) throw new Error('文件记录已变化，请刷新后重试。');
      if (id === activeId || doc.state === 'scanning' || doc.active_batch) {
        throw new Error('此文件正在扫描，请等待完成后再删除。');
      }
      if (!window.confirm('确认删除文件「' + displayName(doc) + '」？\n共 ' + doc.pages.length + ' 页，删除后会从文件记录中移除。')) return;
      const result = await mutate(pathFor(id, 'delete-document'), {updated_at: doc.updated_at});
      if (result.ok !== true || result.id !== id) throw new Error('删除结果尚未确认，请刷新文件记录。');
      removedDocuments.add(id);
      documents.delete(id);
      drafts.delete(id);
      manualIntents.delete(id);
      manualLocks.delete(id);
      if (pageSelection?.id === id) pageSelection = null;
      if (selectedId === id) {
        newNameEdited = false;
        ui.fileName.value = defaultName();
        chooseDocument(sortedHistory()[0]?.id || null);
      }
      notify('已删除文件「' + doc.name + '」。');
    });
  }

  function render() {
    renderDevice();
    renderActive();
    renderSelected();
    renderHistory();
    const preparing = !documents.has(selectedId);
    const deviceBlocked = !!deviceProblem();
    const lockScan = busy || !listLoaded || hasActiveScan() || deviceBlocked || settingsUnavailable();
    ui.currentHeading.textContent = preparing ? '新文件设置' : '当前文件';
    ui.btnScan.hidden = preparing;
    ui.btnScan.disabled = busy || !listLoaded || hasActiveScan() || deviceBlocked;
    ui.btnStartScan.hidden = !preparing;
    ui.btnStartScan.disabled = lockScan || !preparing;
    ui.btnContinue.hidden = preparing;
    ui.btnContinue.disabled = lockScan || !documents.has(selectedId);
    ui.scanActionHelp.textContent = deviceBlocked ? deviceProblem() :
      preparing ? '确认设置后，点击开始扫描' : '继续扫描会追加到当前文件';
    ui.btnPdf.disabled = busy || !documents.get(selectedId)?.pages.length;
    ui.fileName.disabled = busy || !listLoaded;
    ui.dpi.disabled = busy || hasActiveScan() || !reportedDpi() || settingsUnavailable();
    ui.scanMode.disabled = busy || hasActiveScan();
    ui.btnHistoryRefresh.disabled = busy;
    const doc = documents.get(selectedId);
    renderModes(doc);
    renderBlankTools(doc);
    ui.pageTools.hidden = !doc?.pages.length;
    ui.btnReverse.disabled = pageEditingLocked(doc) || doc.pages.length < 2;
    ui.rescanHelp.hidden = !doc?.pages.length || rescanSupported();
    ui.rescanHelp.textContent = scanner.state === 'offline' ? '连接扫描仪后可重扫本页。' :
      scanner.state === 'unknown' ? '单页重扫能力尚未确认，可刷新扫描仪状态。' :
      '更新扫描仪程序并刷新状态后，可使用「重扫本页」。';
  }

  async function saveDraft(id) {
    if (!id || !drafts.get(id)?.dirty) return;
    if (nameSaves.has(id)) {
      await nameSaves.get(id);
      return saveDraft(id);
    }
    const promise = (async () => {
      await lockAutoName(id);
      while (drafts.get(id)?.dirty) {
        const draft = drafts.get(id);
        const value = draft.value.trim();
        const version = draft.version;
        if (!value) throw new Error('请填写文件名称。');
        draft.error = false;
        const doc = await mutate(pathFor(id, 'rename'), {name: value});
        putDocument(doc);
        if (drafts.get(id)?.version === version) drafts.delete(id);
        render();
      }
    })();
    nameSaves.set(id, promise);
    renderName();
    try {
      await promise;
    } catch (error) {
      const draft = drafts.get(id);
      if (draft) draft.error = true;
      throw error;
    } finally {
      nameSaves.delete(id);
      render();
    }
  }

  function scanOptions() {
    return {dpi: Number(ui.dpi.value), duplex: ui.scanMode.value === 'duplex' && scanner.duplex_supported === true};
  }

  async function lockAutoName(id) {
    if (!id || manualLocks.has(id)) return;
    if (nameLocks.has(id)) return nameLocks.get(id);
    manualIntents.add(id);
    const promise = (async () => {
      const doc = await mutate(pathFor(id, 'name-lock'), {}, {keepalive: true});
      manualLocks.add(id);
      putDocument(doc);
    })();
    nameLocks.set(id, promise);
    renderName();
    try { await promise; }
    catch (error) {
      const draft = drafts.get(id);
      if (draft) draft.error = true;
      throw error;
    } finally {
      nameLocks.delete(id);
      render();
    }
  }

  async function startScan(name, appendId = null, autoName = true) {
    assertCanScan();
    const value = name.trim();
    if (!appendId && !autoName && !value) throw new Error('请填写文件名称。');
    const body = appendId ? scanOptions() : {auto_name: autoName, render_mode: draftRenderMode, ...scanOptions()};
    if (!appendId && !autoName) body.name = value;
    const result = await mutate(appendId ? pathFor(appendId, 'continue') : '/scan', body);
    if (!result || !result.id) throw new Error('扫描请求已发送，但未收到文件编号，请刷新文件记录确认。');
    putDocument(result);
    activeId = result.id;
    chooseDocument(result.id);
    notify(appendId ? '已开始继续扫描，新页面会追加到当前文件。' : '已开始扫描，新页面会逐页显示。');
    schedulePoll(500);
  }

  function showNameDialog(action, id = null) {
    dialogAction = {action, id};
    ui.nameDialogTitle.textContent = '重命名文件';
    ui.btnNameSubmit.textContent = '保存名称';
    ui.dialogName.value = documents.get(id)?.name || '';
    ui.nameDialogHelp.textContent = '手动修改后不再自动改名，新的名称也将用于下载 PDF。';
    ui.dialogError.hidden = true;
    ui.nameDialog.showModal();
    ui.dialogName.focus();
    ui.dialogName.select();
  }

  function previewPage(id, number) {
    const doc = documents.get(id);
    if (!doc) return;
    const index = doc.pages.indexOf(number);
    ui.pageDialogTitle.textContent = displayName(doc) + ' · 第 ' + (index + 1) + ' 页';
    ui.pageImage.src = pageUrl(doc, number);
    ui.pageImage.alt = '第 ' + (index + 1) + ' 页扫描预览';
    ui.originalLink.href = pageUrl(doc, number, true);
    ui.previewDialog.showModal();
  }

  async function savePageOrder(id, pages, focusNumber = null) {
    const before = documents.get(id);
    if (pageEditingLocked(before)) return;
    if (pages.length !== before.pages.length || new Set(pages).size !== pages.length ||
        pages.some(number => !before.pages.includes(number))) {
      notify('页面已发生变化，请刷新后重新排序。', true);
      return;
    }
    if (pages.every((number, index) => number === before.pages[index])) return;
    const focused = document.activeElement;
    const oldOrder = [...before.pages];
    await runAction(async () => {
      documents.set(id, {...before, pages: [...pages]});
      render();
      ui.orderStatus.textContent = '正在保存页序…';
      try {
        const result = await mutate(pathFor(id, 'reorder'), {
          pages, order_revision: Number.isInteger(before.order_revision) ? before.order_revision : 0,
        });
        putDocument(result);
        ui.orderStatus.textContent = '页序已保存，PDF 将按此顺序导出。';
        notify('页序已保存，预览与 PDF 使用相同顺序。');
      } catch (error) {
        const current = documents.get(id);
        if (current) documents.set(id, {...current, pages: oldOrder});
        ui.orderStatus.textContent = '页序保存失败，已恢复原顺序。';
        schedulePoll(0);
        throw error;
      }
    });
    if (selectedId === id && focusNumber !== null) {
      const node = pageNodes.get(focusNumber);
      const target = focused?.classList.contains('drag-handle') ? node?.handle :
        focused?.classList.contains('move-up') ? node?.up : node?.down;
      (target && !target.disabled ? target : node?.handle)?.focus({preventScroll: true});
    }
  }

  async function movePage(id, number, offset) {
    const doc = documents.get(id);
    if (pageEditingLocked(doc)) return;
    const index = doc.pages.indexOf(number);
    const target = index + offset;
    if (index < 0 || target < 0 || target >= doc.pages.length) return;
    const pages = [...doc.pages];
    [pages[index], pages[target]] = [pages[target], pages[index]];
    await savePageOrder(id, pages, number);
  }

  function blankSnapshotMatches(result, doc, key) {
    if (!doc || result.document_id !== doc.id || pageSnapshotKey(doc) !== key) return false;
    const expected = pageSnapshot(doc);
    const revisions = result.page_revisions;
    return result.order_revision === expected.order_revision && revisions &&
      Object.keys(revisions).length === doc.pages.length &&
      doc.pages.every(number => revisions[number] === expected.page_revisions[number]);
  }

  async function markBlankPages() {
    const doc = documents.get(selectedId);
    if (pageEditingLocked(doc) || !doc.pages.length) return;
    const id = doc.id, key = pageSnapshotKey(doc);
    await runAction(async () => {
      blankOperation = {id, type: 'analysis'};
      setBlankFeedback(id, '正在识别空白页，页面会保留。');
      render();
      try {
        // Analysis is read-only; keep receiving document changes while it runs.
        const response = await request(pathFor(id, 'blank-analysis'), {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
        }, 120000);
        const result = await response.json();
        const current = documents.get(id);
        if (selectedId !== id || !blankSnapshotMatches(result, current, key) || hasActiveScan() || current.state === 'scanning') {
          pageSelection = null;
          setBlankFeedback(id, '页面或扫描状态已变化，请刷新后重新标记。', true);
          schedulePoll(0);
          return;
        }
        if (!Array.isArray(result.candidates) || result.candidates.some(candidate =>
          !current.pages.includes(candidate.page) || candidate.revision !== result.page_revisions[candidate.page])) {
          throw new Error('未能确认空白页结果，请重新标记。');
        }
        syncPageSelection(current);
        if (!pageSelection) pageSelection = {id, key, pages: new Set()};
        const candidates = new Set(result.candidates.map(candidate => candidate.page));
        for (const number of candidates) pageSelection.pages.add(number);
        const errors = Array.isArray(result.errors) ? result.errors.length : 0;
        setBlankFeedback(id, (candidates.size ? '已标记 ' + candidates.size + ' 页疑似空白页，可补选或取消勾选。' :
          '未识别到空白页，仍可手动勾选需要删除的页面。') +
          (errors ? '另有 ' + errors + ' 页未能识别，未自动勾选。' : ''));
      } catch (error) {
        if (error.status === 409) pageSelection = null;
        setBlankFeedback(id, error.status === 409 ? '页面或扫描状态已变化，旧勾选已清除，请刷新后重新标记。' : error.message, true);
        throw error;
      } finally { blankOperation = null; }
    });
  }

  async function deleteSelectedPages() {
    const doc = documents.get(selectedId);
    if (pageEditingLocked(doc)) return;
    syncPageSelection(doc);
    if (pageSelection?.id !== doc.id || !pageSelection.pages.size) { render(); return; }
    const pages = doc.pages.filter(number => pageSelection.pages.has(number));
    const snapshot = pageSnapshot(doc);
    await runAction(async () => {
      try {
        const result = await mutate(pathFor(doc.id, 'delete-selected'), {pages, ...snapshot});
        if (result.document?.id !== doc.id) throw new Error('删除结果尚未确认，请刷新文件记录。');
        putDocument(result.document);
        pageSelection = null;
        setBlankFeedback(doc.id, '已删除 ' + pages.length + ' 页，可撤销本次删除。');
        notify('已删除所选页面，预览与 PDF 页序已更新。');
      } catch (error) {
        if (error.status === 409) pageSelection = null;
        setBlankFeedback(doc.id, error.status === 409 ? '页面或扫描状态已变化，旧勾选已清除，请刷新后重新选择。' : error.message, true);
        throw error;
      }
    });
  }

  async function undoBlankDeletion() {
    const doc = documents.get(selectedId);
    if (pageEditingLocked(doc) || !doc.blank_undo?.cleanup_id) return;
    const cleanupId = doc.blank_undo.cleanup_id;
    await runAction(async () => {
      try {
        const result = await mutate(pathFor(doc.id, 'blank-undo'), {cleanup_id: cleanupId});
        if (result.document?.id !== doc.id) throw new Error('撤销结果尚未确认，请刷新文件记录。');
        putDocument(result.document);
        pageSelection = null;
        setBlankFeedback(doc.id, '已撤销删除，页面和原页序已恢复。');
        notify('已恢复上次删除的页面。');
      } catch (error) {
        setBlankFeedback(doc.id, error.status === 409 ? '页面或扫描状态已变化，请刷新后再查看可撤销的操作。' : error.message, true);
        throw error;
      }
    });
  }

  function showRescanDialog(id, number) {
    const doc = documents.get(id);
    if (pageEditingLocked(doc) || !rescanSupported() || scanner.state !== 'online') return;
    const index = doc.pages.indexOf(number);
    if (index < 0) return;
    rescanTarget = {id, number};
    ui.rescanTitle.textContent = '重扫第 ' + (index + 1) + ' 页';
    const dpi = doc.page_details?.[number]?.dpi;
    ui.rescanOptions.textContent = '按原页' + (dpi ? ' ' + dpi + ' DPI ' : '清晰度') +
      '单面扫描。成功后替换当前第 ' + (index + 1) + ' 页，其他页面保留。';
    ui.rescanError.hidden = true;
    ui.rescanDialog.showModal();
    ui.btnRescanCancel.focus();
  }

  async function confirmRescan(event) {
    event.preventDefault();
    if (busy || !rescanTarget) return;
    const target = {...rescanTarget};
    await runAction(async () => {
      try {
        ui.btnRescanConfirm.disabled = true;
        ui.btnRescanCancel.disabled = true;
        ui.btnRescanClose.disabled = true;
        const doc = documents.get(target.id);
        if (!doc?.pages.includes(target.number)) throw new Error('该页面已不存在，请刷新文件记录。');
        if (hasActiveScan() || doc.state === 'scanning') throw new Error('请等待当前扫描任务完成后再重扫。');
        if (!rescanSupported() || scanner.state !== 'online') throw new Error('单页重扫当前不可用，请检查扫描仪连接并刷新状态。');
        await saveDraft(selectedId);
        requestedRescan = target;
        render();
        const result = await mutate(pathFor(target.id, 'rescan/' + target.number));
        if (!result?.id) throw new Error('未收到重扫任务信息，请刷新确认。');
        putDocument(result);
        activeId = result.id;
        ui.rescanDialog.close();
        notify('已开始重扫这一页；成功前保留原页，页序不变。');
        schedulePoll(500);
      } catch (error) {
        ui.rescanError.textContent = error.message;
        ui.rescanError.hidden = false;
        throw error;
      } finally {
        requestedRescan = null;
        ui.btnRescanConfirm.disabled = false;
        ui.btnRescanCancel.disabled = false;
        ui.btnRescanClose.disabled = false;
      }
    });
  }

  async function deletePage(id, number) {
    if (busy || hasActiveScan()) return;
    const doc = documents.get(id);
    if (!doc || doc.state === 'scanning') return;
    const index = doc.pages.indexOf(number);
    if (!window.confirm('删除「' + displayName(doc) + '」的第 ' + (index + 1) + ' 页？')) return;
    await runAction(async () => {
      const result = await mutate(pathFor(id, 'delete/' + number));
      if (result.document) putDocument(result.document);
      else if (Array.isArray(result.pages)) putDocument({...documents.get(id), pages: result.pages});
      notify('已删除第 ' + (index + 1) + ' 页。');
    });
  }

  async function downloadDocument(id) {
    await runAction(async () => {
      await saveDraft(selectedId);
      if (id !== selectedId) await saveDraft(id);
      const doc = documents.get(id);
      if (!doc || !doc.pages.length) throw new Error('此文件还没有可以下载的页面。');
      const response = await request(pathFor(id, 'pdf'), {}, 120000);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = (doc.name || '扫描文件').replace(/\.pdf$/i, '') + '.pdf';
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      notify(hasActiveScan() && id === activeId ? '已下载当前已完成页面的 PDF；后续页面会继续追加。' : 'PDF 已开始下载。');
    });
  }

  function schedulePoll(delay) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      await Promise.allSettled([refreshDocuments(), Date.now() - scannerChecked > 8000 ? refreshScanner() : Promise.resolve()]);
      schedulePoll(hasActiveScan() ? 1300 : 4000);
    }, delay);
  }

  ui.fileName.addEventListener('input', () => {
    if (!selectedId) {
      newNameEdited = true;
      renderName();
      return;
    }
    const draft = drafts.get(selectedId);
    manualIntents.add(selectedId);
    drafts.set(selectedId, {value: ui.fileName.value, version: (draft?.version || 0) + 1, dirty: true, error: false});
    // Persist the user's intent on the first input event, before debounced rename.
    void lockAutoName(selectedId).catch(handleError);
    renderName();
    clearTimeout(nameTimer);
    const id = selectedId;
    nameTimer = setTimeout(() => { void saveDraft(id).catch(handleError); }, 800);
  });
  ui.dpi.addEventListener('change', () => { ui.dpi.dataset.preferred = ui.dpi.value; });
  ui.fileName.addEventListener('change', () => { void saveDraft(selectedId).catch(handleError); });
  ui.fileName.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); ui.fileName.blur(); }
  });
  ui.btnScan.addEventListener('click', async () => {
    await runAction(async () => {
      await saveDraft(selectedId);
      newNameEdited = false;
      draftRenderMode = 'original';
      ui.fileName.value = defaultName();
      ui.dpi.dataset.preferred = '300';
      delete ui.dpi.dataset.choices;
      updateCapabilities();
      chooseDocument(null);
      notify('');
      ui.currentHeading.scrollIntoView({behavior: 'smooth', block: 'start'});
    });
    if (!selectedId) ui.fileName.focus();
  });
  ui.btnStartScan.addEventListener('click', () => runAction(async () => {
    if (documents.has(selectedId)) return;
    assertCanScan();
    const autoName = !newNameEdited;
    const name = ui.fileName.value;
    creatingDocument = true;
    render();
    try { await startScan(name, null, autoName); }
    finally { creatingDocument = false; }
  }));
  ui.btnContinue.addEventListener('click', () => runAction(async () => {
    await saveDraft(selectedId);
    const doc = documents.get(selectedId);
    if (!doc) throw new Error('请先打开要继续扫描的文件。');
    await startScan('', doc.id);
  }));
  ui.btnPdf.addEventListener('click', () => downloadDocument(selectedId));
  ui.renderModes.addEventListener('click', event => {
    const button = event.target.closest('button[data-mode]');
    if (button && !button.disabled) void setRenderMode(button.dataset.mode);
  });
  ui.btnMarkBlank.addEventListener('click', () => { void markBlankPages(); });
  ui.btnDeleteSelected.addEventListener('click', () => { void deleteSelectedPages(); });
  ui.btnBlankUndo.addEventListener('click', () => { void undoBlankDeletion(); });
  ui.btnReverse.addEventListener('click', () => {
    const doc = documents.get(selectedId);
    if (doc) void savePageOrder(doc.id, [...doc.pages].reverse());
  });
  ui.rescanForm.addEventListener('submit', confirmRescan);
  ui.btnRescanClose.addEventListener('click', () => { if (!busy) ui.rescanDialog.close(); });
  ui.btnRescanCancel.addEventListener('click', () => { if (!busy) ui.rescanDialog.close(); });
  ui.rescanDialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  ui.btnViewActive.addEventListener('click', () => { if (activeId) void openDocument(activeId); });

  async function abandonActiveBatch() {
    if (busy || !activeId) return;
    const doc = documents.get(activeId);
    await runAction(async () => {
      if (!window.confirm('强制终止「' + (doc ? displayName(doc) : '当前扫描') + '」？\n' +
          '本批扫描会立即结束，已收到的页面保留。扫描电脑上的请求会一并撤回；' +
          '如果当前无法连接扫描电脑，会在恢复连接后自动重试撤回。')) return;
      const result = await mutate(pathFor(activeId, 'abandon'));
      if (result && result.document) putDocument(result.document);
      activeId = null;
      notify(result && result.withdrawn === false
        ? '已强制终止本批扫描。扫描电脑当前不可达，请求将在恢复连接后自动撤回。'
        : '已强制终止本批扫描，扫描电脑上的请求已撤回。');
      schedulePoll(200);
    });
  }

  ui.btnAbandonScan.addEventListener('click', () => { void abandonActiveBatch(); });
  ui.btnDeviceRefresh.addEventListener('click', () => { void refreshScanner(); });
  ui.btnHistoryRefresh.addEventListener('click', () => { void refreshDocuments(); });
  ui.btnHistoryPrev.addEventListener('click', () => { if (!busy) { historyPage--; renderHistory(); } });
  ui.btnHistoryNext.addEventListener('click', () => { if (!busy) { historyPage++; renderHistory(); } });
  ui.btnPreviewClose.addEventListener('click', () => ui.previewDialog.close());
  ui.btnNameClose.addEventListener('click', () => { if (!busy) ui.nameDialog.close(); });
  ui.btnNameCancel.addEventListener('click', () => { if (!busy) ui.nameDialog.close(); });
  ui.nameDialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  ui.dialogName.addEventListener('input', () => {
    if (!dialogAction?.id) return;
    manualIntents.add(dialogAction.id);
    void lockAutoName(dialogAction.id).catch(handleError);
  });
  ui.nameForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !dialogAction) return;
    busy = true;
    ui.btnNameSubmit.disabled = true;
    ui.btnNameCancel.disabled = true;
    ui.btnNameClose.disabled = true;
    ui.dialogName.disabled = true;
    ui.dialogError.hidden = true;
    render();
    try {
      const value = ui.dialogName.value.trim();
      if (!value) throw new Error('请填写文件名称。');
      const id = dialogAction.id;
      await lockAutoName(id);
      const doc = await mutate(pathFor(id, 'rename'), {name: value});
      drafts.delete(id);
      putDocument(doc);
      notify('文件已重命名。');
      ui.nameDialog.close();
    } catch (error) {
      ui.dialogError.textContent = error.message;
      ui.dialogError.hidden = false;
      if (error.status === 409) handleError(error);
    } finally {
      busy = false;
      ui.btnNameSubmit.disabled = false;
      ui.btnNameCancel.disabled = false;
      ui.btnNameClose.disabled = false;
      ui.dialogName.disabled = false;
      render();
    }
  });
  window.addEventListener('beforeunload', event => {
    if ([...drafts.values()].some(draft => draft.dirty)) {
      event.preventDefault();
      event.returnValue = '';
    }
  });
  window.addEventListener('online', () => schedulePoll(0));
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) schedulePoll(0);
  });

  ui.fileName.value = defaultName();
  render();
  void Promise.allSettled([refreshDocuments(), refreshScanner()]).then(() => schedulePoll(1300));
})();
