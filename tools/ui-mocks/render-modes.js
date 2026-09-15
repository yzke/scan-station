async (page) => {
  // Run with playwright-cli run-code --filename=tools/ui-mocks/render-modes.js.
  // Only these three static assets reach the local server; every API is mocked.
  const origin = 'http://127.0.0.1:18761';
  const requests = [], modePosts = [], starts = [], continuations = [], errors = [], unexpected = [];
  let tick = 100, activeId = null, conflicts = 0, modeFailure = false;
  const stamp = () => '2026-09-14T10:00:00.' + String(++tick).padStart(6, '0') + 'Z';
  const touch = doc => { doc.updated_at = stamp(); };
  const makeDoc = (id, name, pages = [1]) => ({id, name, name_source: 'manual', auto_name: false,
    ocr_state: 'skipped', state: 'done', msg: '', pages,
    page_details: Object.fromEntries(pages.map(number => [number, {revision: 2, dpi: 300}])),
    order_revision: 1, active_batch: null, rescan_page: null, blank_undo: null,
    dpi: 300, duplex: false, render_version: 7, created_at: stamp(), updated_at: stamp()});
  const legacy = makeDoc('render-legacy', '旧文件 · 模式检查样例');
  // Legacy metadata deliberately lacks render_mode; the UI must show original.
  const records = [legacy];
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const respond = (route, value, status = 200) => route.fulfill({status,
    contentType: 'application/json', body: JSON.stringify(value)});
  const queryOf = url => Object.fromEntries((url.split('?')[1] || '').split('&')
    .filter(Boolean).map(part => part.split('=')));
  const fixture = mode => '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="424">' +
    '<rect width="300" height="424" fill="' + (mode === 'original' ? '#e6e3dd' : 'white') + '"/>' +
    '<text x="28" y="48" font-size="17" fill="#222">模式检查样例</text>' +
    '<path d="M28 78H270M28 100H230M28 122H250M28 145H218" stroke="#555"/>' +
    '<rect x="28" y="175" width="242" height="138" fill="none" stroke="#777"/>' +
    '<path d="M28 209H270M28 244H270M28 279H270M110 175V313M190 175V313" stroke="#888"/>' +
    '<circle cx="219" cy="347" r="35" fill="none" stroke="' + (mode === 'bw' ? '#222' : '#cb4b4b') +
    '" stroke-width="4"/><text x="195" y="352" font-size="14">样例章</text></svg>';
  await page.unroute('**/*');
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', async route => {
    const request = route.request(), url = request.url();
    if (!url.startsWith(origin + '/')) {
      unexpected.push('Blocked external URL: ' + url);
      return route.abort();
    }
    const path = url.slice(origin.length).split('?')[0], method = request.method();
    if (method === 'GET' && ['/', '/index.html', '/app.js', '/style.css'].includes(path)) return route.continue();
    if (path === '/favicon.ico') return route.fulfill({status: 204});
    const body = method === 'POST' ? request.postDataJSON() : null;
    requests.push({method, path, body});
    if (method === 'GET' && path === '/documents') return respond(route, {documents: records, active_id: activeId});
    if (method === 'GET' && path === '/scanner/status') return respond(route, {state: 'online',
      message: '本机模拟设备', supported_dpi: [150, 200, 300], duplex_supported: true, supports_page_rescan: true,
      host_reachable: true, heartbeat_fresh: true});
    const image = path.match(/^\/scan\/([^/]+)\/(page|original)\/(\d+)$/);
    if (method === 'GET' && image) {
      const doc = records.find(item => item.id === image[1]);
      if (!doc || !doc.pages.includes(Number(image[3]))) return respond(route, {error: '无此页面'}, 404);
      return route.fulfill({contentType: 'image/svg+xml', body: fixture(image[2] === 'original' ? 'original' : queryOf(url).mode)});
    }
    if (method === 'POST' && path === '/scan') {
      starts.push(body);
      const doc = makeDoc('render-new-' + starts.length, body.name || '新扫描样例', []);
      Object.assign(doc, {render_mode: body.render_mode, dpi: body.dpi, duplex: body.duplex,
        state: 'scanning', active_batch: 'new-batch'});
      records.unshift(doc); activeId = doc.id;
      return respond(route, doc, 202);
    }
    const action = path.match(/^\/scan\/([^/]+)\/(status|set-render-mode|continue)$/);
    if (action) {
      const doc = records.find(item => item.id === action[1]);
      if (!doc) return respond(route, {error: '无此文件'}, 404);
      if (method === 'GET' && action[2] === 'status') return respond(route, doc);
      if (method === 'POST' && action[2] === 'set-render-mode') {
        modePosts.push({id: doc.id, body, expectedTimestamp: doc.updated_at});
        if (modeFailure) { modeFailure = false; return respond(route, {error: '模拟模式保存失败'}, 503); }
        if (conflicts > 0) { conflicts--; touch(doc); return respond(route, {error: '模拟页面到达，版本变化'}, 409); }
        if (body.updated_at !== doc.updated_at || !['original', 'enhanced', 'bw'].includes(body.render_mode)) {
          return respond(route, {error: '模式或更新时间错误'}, 409);
        }
        doc.render_mode = body.render_mode; touch(doc);
        return respond(route, doc);
      }
      if (method === 'POST' && action[2] === 'continue') {
        continuations.push({id: doc.id, body, modeBefore: doc.render_mode});
        Object.assign(doc, {state: 'scanning', active_batch: 'continued-batch'});
        activeId = doc.id; touch(doc);
        return respond(route, doc, 202);
      }
    }
    unexpected.push(method + ' ' + path);
    return respond(route, {error: 'Unexpected mocked route'}, 404);
  });
  await page.addInitScript(() => {
    if (!sessionStorage.getItem('renderModeMockInitialized')) {
      localStorage.clear(); sessionStorage.clear();
      sessionStorage.setItem('renderModeMockInitialized', '1');
    }
    window.__renderModeConfirmations = [];
    window.confirm = message => { window.__renderModeConfirmations.push(message); return false; };
  });
  await page.setViewportSize({width: 1440, height: 1000});
  await page.goto(origin);
  const button = mode => page.locator('#renderModes button[data-mode="' + mode + '"]');
  const idle = () => page.waitForFunction(() => !document.querySelector('#renderModes button').disabled);
  const waitMode = mode => page.waitForFunction(mode =>
    document.querySelector('#renderModes button[aria-pressed="true"]')?.dataset.mode === mode &&
    !document.querySelector('#renderModes button').disabled, mode);
  const waitCount = count => page.waitForFunction(count => document.querySelectorAll('#grid .pg').length === count, count);
  const refresh = async () => {
    const response = page.waitForResponse(response => response.url() === origin + '/documents');
    await page.locator('#btnHistoryRefresh').click();
    await (await response).finished(); await idle();
  };
  const flowSince = index => requests.slice(index).filter(item => /\/(status|set-render-mode)$/.test(item.path) &&
    item.path.startsWith('/scan/')).map(item => item.method + ' ' + item.path.split('/').pop());
  const checkPageUrl = async (number, mode, revision = 2) => {
    const source = await page.locator('#grid .pg[data-page="' + number + '"] img').getAttribute('src');
    const query = queryOf(source);
    assert(query.mode === mode && query.ev === '7' && query.v === String(revision), 'Incorrect preview URL: ' + source);
    return source;
  };
  await waitCount(1); await waitMode('original');
  assert(await page.locator('#fileName').inputValue() === legacy.name, 'Legacy document name changed');
  await checkPageUrl(1, 'original');

  touch(legacy); // The displayed list snapshot is deliberately older than status.
  let before = requests.length;
  await button('enhanced').click(); await waitMode('enhanced');
  assert(JSON.stringify(flowSince(before)) === JSON.stringify(['GET status', 'POST set-render-mode']), 'Mode update did not read fresh status first');
  assert(modePosts[0].body.updated_at === modePosts[0].expectedTimestamp, 'Mode update used a stale CAS timestamp');
  const enhancedUrl = await checkPageUrl(1, 'enhanced');
  await page.locator('#grid .page-open').first().click();
  await page.waitForFunction(() => document.getElementById('previewDialog').open);
  assert(await page.locator('#pageImage').getAttribute('src') === enhancedUrl, 'Large preview does not use the thumbnail mode/version');
  const raw = await page.locator('#originalLink').getAttribute('href');
  assert(raw.includes('/original/1?v=2') && !raw.includes('mode=') && !raw.includes('ev='), 'Raw original link was converted into a rendered link');
  await page.locator('#btnPreviewClose').click();
  await page.reload(); await waitCount(1); await waitMode('enhanced');
  await checkPageUrl(1, 'enhanced');

  await page.locator('#grid .page-check').first().check();
  assert(await page.locator('#btnDeleteSelected').innerText() === '删除选中页（1）', 'Page selection did not register');
  before = requests.length;
  const postsBeforeConflict = modePosts.length;
  conflicts = 1;
  await button('bw').click(); await waitMode('bw');
  assert(JSON.stringify(flowSince(before)) === JSON.stringify(['GET status', 'POST set-render-mode', 'GET status', 'POST set-render-mode']), '409 did not trigger exactly one fresh-status retry');
  const retried = modePosts.slice(postsBeforeConflict);
  assert(retried.length === 2 && retried[0].body.updated_at !== retried[1].body.updated_at &&
    retried.every(item => item.body.updated_at === item.expectedTimestamp), '409 retry did not use the newly read timestamp');
  assert(!(await page.locator('#grid .page-check').first().isChecked()), 'Changing mode left an old page selection checked');
  assert(await page.locator('#btnDeleteSelected').isDisabled(), 'Cleared page selection can still be deleted');

  const priorName = await page.locator('#fileName').inputValue(), postsBeforeFailure = modePosts.length;
  modeFailure = true;
  await button('enhanced').click();
  await page.waitForFunction(() => document.getElementById('notice').textContent.includes('模拟模式保存失败'));
  await waitMode('bw');
  assert(modePosts.length === postsBeforeFailure + 1, 'Non-conflict failure was retried');
  assert(await page.locator('#fileName').inputValue() === priorName && legacy.render_mode === 'bw', 'Failed mode change altered the mode or name');
  await checkPageUrl(1, 'bw');

  Object.assign(legacy, {state: 'scanning', active_batch: 'mock-active-batch'});
  activeId = legacy.id; touch(legacy); await refresh();
  await page.waitForFunction(() => document.getElementById('fileState').textContent === '正在扫描');
  assert(!(await button('enhanced').isDisabled()) && await page.locator('#grid .page-check').first().isDisabled(), 'Live scanning did not separate mode controls from page editing');
  await button('enhanced').click(); await waitMode('enhanced');
  assert(legacy.active_batch === 'mock-active-batch' && starts.length === 0, 'Mode selection interfered with the scan batch');
  legacy.pages.push(2); legacy.page_details[2] = {revision: 1, dpi: 300};
  legacy.order_revision++; touch(legacy); await refresh(); await waitCount(2); await waitMode('enhanced');
  await checkPageUrl(1, 'enhanced'); await checkPageUrl(2, 'enhanced', 1);
  Object.assign(legacy, {state: 'done', active_batch: null}); activeId = null; touch(legacy);
  await refresh(); await page.waitForFunction(() => !document.getElementById('btnContinue').disabled);

  await page.screenshot({path: 'output/playwright/scanner-render-modes-desktop.png', fullPage: true});
  await page.setViewportSize({width: 390, height: 844});
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile layout overflows horizontally');
  assert(await button('original').isVisible() && await button('enhanced').isVisible() && await button('bw').isVisible(), 'Mobile mode choices are hidden');
  await page.screenshot({path: 'output/playwright/scanner-render-modes-mobile.png', fullPage: true});
  await page.setViewportSize({width: 1440, height: 1000});

  const postsBeforeDraft = modePosts.length;
  await page.locator('#btnScan').click(); await waitMode('original');
  assert(await page.locator('#btnStartScan').isVisible() && starts.length === 0, 'Preparing a new file immediately started scanning');
  assert(await page.locator('#dpi').inputValue() === '300', 'New-file preparation did not select 300 DPI');
  await button('bw').click(); await waitMode('bw');
  assert(modePosts.length === postsBeforeDraft && starts.length === 0, 'Draft mode selection called a document or scan API');
  await page.locator('#fileName').fill('新文件 · 黑白模式样例');
  await page.locator('#btnStartScan').click();
  await page.waitForFunction(() => document.getElementById('fileState').textContent === '正在扫描');
  await waitMode('bw');
  assert(starts.length === 1 && starts[0].render_mode === 'bw' && starts[0].dpi === 300 &&
    starts[0].name === '新文件 · 黑白模式样例' && starts[0].auto_name === false, 'Explicit scan start did not send the draft mode/name/300 DPI');
  const created = records[0];
  created.pages.push(1); created.page_details[1] = {revision: 1, dpi: 300}; created.order_revision++;
  Object.assign(created, {state: 'done', active_batch: null}); activeId = null; touch(created);
  await refresh(); await waitCount(1); await waitMode('bw'); await checkPageUrl(1, 'bw', 1);
  await page.waitForFunction(() => !document.getElementById('btnContinue').disabled);
  await page.locator('#btnContinue').click();
  await page.waitForFunction(() => document.getElementById('fileState').textContent === '正在扫描');
  await waitMode('bw');
  assert(continuations.length === 1 && continuations[0].id === created.id &&
    continuations[0].body.dpi === 300 && !Object.hasOwn(continuations[0].body, 'render_mode') &&
    continuations[0].modeBefore === 'bw' && created.render_mode === 'bw', 'Continue reset the saved mode');
  assert(starts.length === 1 && !requests.some(item => item.path.includes('/rescan/')), 'Unexpected scan creation or rescan request');
  assert(errors.length === 0 && unexpected.length === 0, 'Unexpected browser/API errors: ' + [...errors, ...unexpected].join('; '));
  return {passed: true, origin, modeUpdates: modePosts.length, mockedScanStarts: starts.length,
    mockedContinuations: continuations.length, checks: ['legacy original default', 'fresh status and exact CAS',
      'one 409 reread/retry', 'thumbnail/modal mode+ev+v and raw link', 'reload persistence',
      'mode change clears selection', 'failed update retains mode/name', 'switch during scanning and preserve mode on append',
      'desktop/mobile layout', 'new file prepares without scan POST', 'explicit start sends mode/name/300 DPI',
      'continue preserves saved mode'], errors, unexpected};
}
