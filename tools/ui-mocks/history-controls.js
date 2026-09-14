async (page) => {
  const origin = 'http://127.0.0.1:18761';
  const makeDoc = number => ({id: 'history-' + number, name: '测试文件 ' + number,
    name_source: 'manual', auto_name: false, ocr_state: 'skipped', state: 'done', msg: '',
    pages: [1], page_details: {1: {revision: 1, dpi: 300}}, order_revision: 1,
    active_batch: null, rescan_page: null, blank_undo: null, dpi: 300, duplex: false,
    created_at: new Date(Date.UTC(2026, 8, 13, 12, 0, 60 - number)).toISOString(),
    updated_at: new Date(Date.UTC(2026, 8, 13, 12, 0, 60 - number)).toISOString()});
  let records = Array.from({length: 21}, (_, i) => makeDoc(i + 1));
  let conflict = false, activeId = null;
  const deletions = [], requests = [], errors = [];
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const respond = (route, value, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(value)});
  await page.unroute('**/*');
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', async route => {
    const request = route.request(), url = request.url();
    if (!url.startsWith(origin + '/')) return route.abort();
    const path = url.slice(origin.length).split('?')[0];
    if (path === '/' || path === '/app.js' || path === '/style.css') return route.continue();
    if (path === '/favicon.ico') return route.fulfill({status: 204});
    requests.push({method: request.method(), path});
    if (path === '/documents') return respond(route, {documents: records, active_id: activeId});
    if (path === '/scanner/status') return respond(route, {state: 'online', message: '模拟设备', supported_dpi: [150, 200, 300], duplex_supported: true, supports_page_rescan: true});
    if (/\/page\/1$/.test(path)) return route.fulfill({contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="280"><rect width="200" height="280" fill="white"/><text x="15" y="50">History fixture</text></svg>'});
    const match = path.match(/^\/scan\/(history-\d+)\/delete-document$/);
    if (match) {
      const doc = records.find(item => item.id === match[1]);
      if (!doc) return respond(route, {error: '不存在'}, 404);
      const body = request.postDataJSON();
      deletions.push({id: doc.id, body});
      if (conflict) { conflict = false; return respond(route, {error: '文件已变化，请重新确认'}, 409); }
      if (doc.active_batch || body.updated_at !== doc.updated_at) return respond(route, {error: '版本过期或正在扫描'}, 409);
      records = records.filter(item => item.id !== doc.id);
      return respond(route, {ok: true, id: doc.id});
    }
    return respond(route, {error: 'Unexpected route'}, 404);
  });
  await page.addInitScript(() => {
    localStorage.clear(); sessionStorage.clear();
    window.__historyConfirmations = [];
    window.__historyDecisions = [];
    window.confirm = message => { window.__historyConfirmations.push(message); return window.__historyDecisions.shift() === true; };
  });
  await page.goto(origin);
  const entries = () => page.locator('#history .history-entry');
  const ids = () => entries().evaluateAll(nodes => nodes.map(node => node.dataset.id));
  const idle = () => page.waitForFunction(() => !document.getElementById('btnHistoryRefresh').disabled);
  const waitIds = values => page.waitForFunction(values => JSON.stringify([...document.querySelectorAll('#history .history-entry')].map(node => node.dataset.id)) === JSON.stringify(values), values);
  const refresh = async () => {
    const received = page.waitForResponse(response => response.url() === origin + '/documents');
    await page.locator('#btnHistoryRefresh').click();
    await (await received).finished();
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => resolve())));
    await idle();
  };
  const expected = start => Array.from({length: 10}, (_, i) => 'history-' + (start + i));
  const decide = accept => page.evaluate(accept => { window.__historyDecisions.push(accept); }, accept);
  await waitIds(expected(1));
  assert(await page.locator('#btnHistoryPrev').isDisabled(), 'First page has an enabled previous button');
  assert((await page.locator('#historyCount').innerText()) === '21', 'Total count is not all files');
  await page.locator('#btnHistoryNext').click(); await waitIds(expected(11));
  await refresh(); await waitIds(expected(11));
  await page.locator('#btnHistoryNext').click(); await waitIds(['history-21']);
  assert(await page.locator('#btnHistoryNext').isDisabled(), 'Last page has an enabled next button');
  await decide(false); await page.locator('#history .history-delete').click(); await idle();
  assert(deletions.length === 0 && records.length === 21, 'Cancellation deleted a file');
  await decide(true); await page.locator('#history .history-delete').click(); await idle();
  await waitIds(expected(11));
  assert(records.length === 20 && (await entries().count()) === 10, 'Last-page deletion did not clamp pagination');
  const confirmations = await page.evaluate(() => window.__historyConfirmations);
  assert(confirmations.length === 2 && confirmations.every(message => message.includes('测试文件 21') && message.includes('共 1 页')), 'Confirmation lacks the file name or page count');
  assert(deletions[0].body.updated_at === makeDoc(21).updated_at, 'Delete did not send the exact timestamp');

  conflict = true;
  await decide(true); await page.locator('#history .history-delete').first().click(); await idle();
  assert(records.length === 20 && (await ids()).includes('history-11'), 'Conflict optimistically removed a file');
  await page.locator('#btnHistoryPrev').click(); await waitIds(expected(1));
  const removed = {...records[0]};
  await decide(true); await page.locator('#history .history-delete').first().click(); await idle();
  await page.waitForFunction(() => document.getElementById('fileName').value === '测试文件 2');
  assert((await ids())[0] === 'history-2', 'Deleted selected document did not switch to an existing file');
  records.unshift(removed);
  await refresh();
  assert(!(await ids()).includes(removed.id), 'A late snapshot resurrected the deleted record');
  records = records.filter(doc => doc.id !== removed.id);

  records[0].active_batch = 'pending-batch'; activeId = records[0].id;
  await refresh();
  await page.waitForFunction(() => document.querySelector('#history .history-delete').disabled);
  assert(await page.locator('#history .history-delete').first().isDisabled(), 'Pending scan can be deleted');
  records[0].active_batch = null; activeId = null;
  records = [records[0]];
  await refresh(); await page.waitForFunction(() => document.querySelectorAll('#history .history-entry').length === 1);
  await decide(true); await page.locator('#history .history-delete').click(); await idle();
  assert((await entries().count()) === 0 && await page.locator('#btnStartScan').isVisible(), 'Deleting the final document did not open new-file preparation');
  assert(await page.locator('#historyPager').isHidden(), 'Empty history shows a pager');

  records = [makeDoc(201), makeDoc(202)];
  records[0].updated_at = '2026-09-13T12:00:59Z';
  records[1].updated_at = '2026-09-13T12:00:59.000001Z';
  await refresh(); await waitIds(['history-202', 'history-201']);
  await page.locator('.history-entry[data-id="history-202"] .history-open').click(); await idle();
  records[1].updated_at = '2026-09-13T12:00:59.000009Z'; records[1].name = '新的名称';
  await refresh();
  await page.waitForFunction(() => document.getElementById('fileName').value === '新的名称');
  records[1].updated_at = '2026-09-13T12:00:59.000004Z'; records[1].name = '迟到的旧名称';
  await refresh();
  assert(await page.locator('#fileName').inputValue() === '新的名称', 'Older same-millisecond snapshot replaced a newer one');

  records = Array.from({length: 21}, (_, i) => makeDoc(i + 101));
  await refresh(); await page.waitForFunction(() => document.querySelectorAll('#history .history-entry').length === 10);
  await page.setViewportSize({width: 390, height: 844});
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile layout overflows horizontally');
  await page.locator('#btnHistoryNext').click();
  assert((await entries().count()) === 10, 'Mobile second page exceeds ten entries');
  await page.screenshot({path: 'output/playwright/scanner-history-mobile.png', fullPage: true});
  await page.setViewportSize({width: 1440, height: 1000});
  await page.screenshot({path: 'output/playwright/scanner-history-desktop.png', fullPage: true});
  assert(errors.length === 0, 'Browser exceptions: ' + errors.join('; '));
  assert(!requests.some(item => item.path === '/scan' || item.path.endsWith('/continue') || item.path.includes('/rescan/')), 'History checks started a scan');
  return {passed: true, checks: ['10/10/1 pagination and polling', 'cancel and exact snapshot confirmation', 'last-page clamping', '409 retains records', 'selected-file deletion and stale-response suppression', 'pending scan lock', 'empty history preparation', 'microsecond and legacy timestamp ordering', 'mobile layout'], errors, origin};
}
