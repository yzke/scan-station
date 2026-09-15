async (page) => {
  const fixture = {
    documents: [{id:'order-doc',name:'手动归档文件',name_source:'manual',auto_name:false,ocr_state:'done',state:'done',msg:'',pages:[1,2,3,4],page_details:{1:{revision:1,dpi:150},2:{revision:1,dpi:200},3:{revision:1,dpi:300},4:{revision:1,dpi:150}},order_revision:0,rescan_page:null,dpi:300,duplex:true,created_at:'2026-09-13T01:00:00Z',updated_at:'2026-09-13T01:00:00Z'}],
    active_id: null,
    scanner: {state: 'online', message: 'A4 送纸式扫描仪已连接，可以开始扫描', checked_at: '2026-09-13T08:00:00Z', supported_dpi: [150, 200, 300], duplex_supported: true, supports_page_rescan: true, host_reachable: true, heartbeat_fresh: true},
    logs: [], nextId: 1, failNext: null, listDelay: 0, createDelay: 0, ocrAttempts: []
  };
  const respond = (route, body, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(body)});
  const pageImage = number => '<svg xmlns="http://www.w3.org/2000/svg" width="620" height="877" viewBox="0 0 620 877"><rect width="620" height="877" fill="#fff"/><text x="64" y="105" font-family="sans-serif" font-size="29" fill="#37455a">文档预览 · 第 ' + number + ' 页</text><path d="M64 140H550" stroke="#cad5e3" stroke-width="2"/><g stroke="#98a6b7" stroke-width="7" opacity=".6">' + Array.from({length: 15}, (_, i) => '<path d="M64 ' + (198 + i * 33) + 'H' + (i % 4 === 3 ? 406 : 550) + '"/>').join('') + '</g><rect x="64" y="745" width="165" height="50" rx="3" fill="#eef2f7"/><text x="88" y="776" font-family="sans-serif" font-size="16" fill="#7f8ca0">模拟文档</text></svg>';
  const uniqueName = (name, id) => {
    const names = new Set(fixture.documents.filter(doc => doc.id !== id).map(doc => doc.name));
    if (!names.has(name)) return name;
    let number = 1;
    while (names.has(name + '-' + String(number).padStart(3, '0'))) number++;
    return name + '-' + String(number).padStart(3, '0');
  };
  await page.unroute('**/*');
  await page.route('**/*', async route => {
    const request = route.request();
    const requestUrl = request.url();
    if (!requestUrl.startsWith('http://127.0.0.1:18761/')) return route.abort();
    const path = requestUrl.slice('http://127.0.0.1:18761'.length).split('?')[0];
    const method = request.method();
    if (path === '/__mock__') {
      if (method === 'POST') {
        const change = request.postDataJSON();
        if (change.document) {
          const doc = fixture.documents.find(item => item.id === change.document.id);
          Object.assign(doc, change.document, {updated_at: new Date().toISOString()});
        }
        for (const key of ['active_id', 'scanner', 'failNext', 'listDelay', 'createDelay']) {
          if (Object.hasOwn(change, key)) fixture[key] = change[key];
        }
        if (change.ocr) {
          const doc = fixture.documents.find(item => item.id === change.ocr.id);
          fixture.ocrAttempts.push(change.ocr);
          if (doc.auto_name && doc.name_source !== 'manual') {
            doc.ocr_state = change.ocr.state || 'done';
            if (change.ocr.name) {
              doc.name = uniqueName(change.ocr.name, doc.id);
              doc.name_source = 'ocr';
            }
            doc.updated_at = new Date().toISOString();
          }
        }
        if (change.remove) fixture.documents = fixture.documents.filter(doc => doc.id !== change.remove);
      }
      return respond(route, fixture);
    }
    if (fixture.failNext && fixture.failNext.path === path) {
      const failure = fixture.failNext;
      fixture.failNext = null;
      return respond(route, failure.body, failure.status);
    }
    if (path === '/documents') {
      const snapshot = {documents: JSON.parse(JSON.stringify(fixture.documents)), active_id: fixture.active_id};
      const delay = fixture.listDelay;
      fixture.listDelay = 0;
      if (delay) await page.waitForTimeout(delay);
      return respond(route, snapshot);
    }
    if (path === '/scanner/status') return respond(route, fixture.scanner);
    if (path === '/favicon.ico') return route.fulfill({status: 204});
    if (path === '/' || path === '/app.js' || path === '/style.css') return route.continue();
    const match = path.match(/^\/scan\/([^/]+)\/(status|continue|rename|name-lock|page|original|pdf|delete|reorder|rescan)(?:\/(\d+))?$/);
    if (path !== '/scan' && !match) return respond(route, {error: 'Mock route missing'}, 404);
    const body = method === 'POST' ? request.postDataJSON() : {};
    fixture.logs.push({path, method, body, time: Date.now()});
    if (path === '/scan') {
      const autoName = body.auto_name === true;
      const doc = {id: 'mock-new-' + fixture.nextId++, name: uniqueName(autoName ? '扫描文件 2026-09-13 0930' : body.name), name_source: autoName ? 'default' : 'manual', auto_name: autoName, ocr_state: autoName ? 'pending' : 'skipped', dpi: body.dpi, duplex: body.duplex, state: 'scanning', msg: '', pages: [], created_at: new Date().toISOString(), updated_at: new Date().toISOString()};
      fixture.documents.unshift(doc);
      fixture.active_id = doc.id;
      fixture.scanner.state = 'scanning';
      if (fixture.createDelay) await page.waitForTimeout(fixture.createDelay);
      return respond(route, doc, 202);
    }
    const doc = fixture.documents.find(item => item.id === match[1]);
    if (!doc) return respond(route, {error: '文件不存在'}, 404);
    const operation = match[2];
    if (operation === 'status') return respond(route, doc);
    if (operation === 'continue') {
      Object.assign(doc, body, {state: 'scanning', updated_at: new Date().toISOString()});
      fixture.active_id = doc.id;
      fixture.scanner.state = 'scanning';
      return respond(route, doc, 202);
    }
    if (operation === 'reorder') {
      if (fixture.active_id) return respond(route,{error:'扫描任务进行中，暂不能修改页序',active_id:fixture.active_id},409);
      if (body.order_revision !== doc.order_revision) return respond(route,{error:'页序已变化，请刷新后重试'},409);
      if (body.pages.length !== doc.pages.length || new Set(body.pages).size !== doc.pages.length || body.pages.some(n=>!doc.pages.includes(n))) return respond(route,{error:'页序必须包含全部页面'},400);
      doc.pages = body.pages.slice();
      doc.order_revision++;
      doc.updated_at = new Date().toISOString();
      return respond(route,doc);
    }
    if (operation === 'rescan') {
      if (fixture.active_id) return respond(route,{error:'扫描仪正在工作',active_id:fixture.active_id},409);
      if (!fixture.scanner.supports_page_rescan) return respond(route,{error:'请更新扫描仪程序以启用单页重扫'},400);
      doc.rescan_page = Number(match[3]);
      doc.last_rescan_settings = {dpi:doc.page_details[doc.rescan_page].dpi,duplex:false,max_pages:1};
      doc.state = 'scanning';
      doc.updated_at = new Date().toISOString();
      fixture.active_id = doc.id;
      fixture.scanner.state = 'scanning';
      return respond(route,doc,202);
    }
    if (operation === 'name-lock') {
      doc.name_source = 'manual';
      doc.auto_name = false;
      doc.updated_at = new Date().toISOString();
      return respond(route, doc);
    }
    if (operation === 'rename') {
      doc.name = uniqueName(body.name, doc.id);
      doc.name_source = 'manual';
      doc.auto_name = false;
      doc.updated_at = new Date().toISOString();
      return respond(route, doc);
    }
    if (operation === 'delete') {
      doc.pages = doc.pages.filter(number => number !== Number(match[3]));
      doc.updated_at = new Date().toISOString();
      return respond(route, {ok: true, pages: doc.pages, document: doc});
    }
    if (operation === 'pdf') fixture.logs[fixture.logs.length - 1].pdf_order = doc.pages.slice();
    if (operation === 'pdf') return route.fulfill({status: 200, contentType: 'application/pdf', headers: {'Content-Disposition': 'attachment; filename="mock.pdf"'}, body: '%PDF-1.4\n%MOCK DOCUMENT\n%%EOF'});
    return route.fulfill({status: 200, contentType: 'image/svg+xml', body: pageImage(match[3]).replace('文档预览 ·', 'v' + doc.page_details?.[match[3]]?.revision + ' 文档预览 ·')});
  });
  await page.setViewportSize({width: 1440, height: 1000});
  await page.goto('http://127.0.0.1:18761/');
}
