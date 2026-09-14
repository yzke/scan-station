async (page) => {
  if (page.url() !== 'http://127.0.0.1:18761/') throw new Error('Refusing non-mock origin');
  const assert=(value,message)=>{if(!value) throw new Error(message);};
  const read=()=>page.evaluate(async()=>(await fetch('/__mock__')).json());
  const change=value=>page.evaluate(async value=>(await fetch('/__mock__',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)})).json(),value);
  const idle=()=>page.waitForFunction(()=>!document.getElementById('btnHistoryRefresh').disabled);
  const order=()=>page.locator('#grid .pg').evaluateAll(nodes=>nodes.map(node=>Number(node.dataset.page)));
  const selected=()=>page.locator('#grid .pg').evaluateAll(nodes=>nodes.filter(node=>node.querySelector('input').checked).map(node=>Number(node.dataset.page)));
  const waitOrder=values=>page.waitForFunction(values=>JSON.stringify([...document.querySelectorAll('#grid .pg')].map(node=>Number(node.dataset.page)))===JSON.stringify(values),values);
  const pick=number=>page.locator('.pg[data-page="'+number+'"] .page-check');
  const refresh=async()=>{await page.locator('#btnHistoryRefresh').click();await idle();};
  const countRequests=(data,suffix)=>data.logs.filter(item=>item.path.endsWith('/'+suffix)).length;
  const results=[],dialogs=[];
  page.on('dialog',async dialog=>{dialogs.push(dialog.type());await dialog.dismiss();});
  await waitOrder([3,1,4,2]);
  await page.evaluate(()=>{window.__blankImageNodes=Object.fromEntries([...document.querySelectorAll('#grid .pg')].map(node=>[node.dataset.page,node.querySelector('img')]));});
  await page.locator('#btnMarkBlank').click();await idle();
  let data=await read();
  assert(JSON.stringify(await selected())==='[1,2]','Automatic marking did not select stable candidate IDs');
  assert(JSON.stringify(await order())==='[3,1,4,2]','Analysis changed pages or order');
  assert(countRequests(data,'delete-selected')===0,'Marking issued a deletion');
  assert((await page.locator('dialog[open]').count())===0 && dialogs.length===0,'Marking opened a preview/confirmation dialog');
  assert((await page.locator('#blankStatus').innerText()).includes('1 页未能识别'),'Unreadable page was not explained');
  assert(!(await pick(4).isChecked()),'Unreadable page was auto-selected');
  assert(await page.evaluate(()=>[...document.querySelectorAll('#grid .pg')].every(node=>node.querySelector('img')===window.__blankImageNodes[node.dataset.page])),'Marking recreated image nodes');
  await page.screenshot({path:'output/playwright/scanner-blank-marked-desktop.png',fullPage:true});
  results.push('Automatic analysis checks existing cards only; no deletion/modal/order change; unreadable page is unselected and image nodes persist');

  await pick(2).uncheck();await pick(4).check();
  assert(JSON.stringify(await selected())==='[1,4]','Manual cancellation/addition failed');
  assert((await page.locator('#btnDeleteSelected').innerText())==='删除选中页（2）','Selected count does not match button');
  await page.locator('#btnDeleteSelected').click();await idle();await waitOrder([3,2]);
  data=await read();
  let deletion=data.logs.findLast(item=>item.path.endsWith('/delete-selected'));
  assert(JSON.stringify(deletion.body.pages)==='[1,4]','Delete used positions instead of stable IDs');
  assert(deletion.body.order_revision===4 && JSON.stringify(deletion.body.page_revisions)==='{"1":1,"2":1,"3":2,"4":1}','Delete lacks the complete current revision snapshot');
  assert(dialogs.length===0,'Delete required an extra confirmation');
  assert(await page.locator('#btnBlankUndo').isVisible(),'Undo not offered after deletion');
  await page.locator('#btnPdf').click();await idle();
  data=await read();
  assert(JSON.stringify(data.logs.findLast(item=>item.path.endsWith('/pdf')).pdf_order)==='[3,2]','PDF request does not follow the remaining order');
  results.push('Manual add/remove updates count; explicit delete sends all revisions and chosen stable IDs with no third confirmation; preview/PDF order agree');

  await page.locator('#fileName').fill('清理后人工命名');await page.locator('#fileName').press('Tab');
  await page.waitForFunction(async()=>(await(await fetch('/__mock__')).json()).documents[0].name==='清理后人工命名');
  await page.locator('#btnBlankUndo').click();await idle();await waitOrder([3,1,4,2]);
  assert((await page.locator('#fileName').inputValue())==='清理后人工命名','Undo overwrote a manual name');
  assert(!(await page.locator('#btnBlankUndo').isVisible()),'Undo remains after restoration');
  assert((await selected()).length===0,'Undo restored stale checkmarks');
  await pick(3).check();
  await page.locator('#fileName').fill('勾选后保留人工名称');await page.locator('#fileName').press('Tab');
  await page.waitForFunction(async()=>(await(await fetch('/__mock__')).json()).documents[0].name==='勾选后保留人工名称');
  assert(await pick(3).isChecked(),'Rename invalidated a valid selection');
  const analyzed=countRequests(await read(),'blank-analysis');
  await page.locator('#btnDeleteSelected').click();await idle();await waitOrder([1,4,2]);
  assert(countRequests(await read(),'blank-analysis')===analyzed,'Manual deletion depended on an analysis call');
  await page.reload();await waitOrder([1,4,2]);
  assert(await page.locator('#btnBlankUndo').isVisible(),'Reload lost server-provided undo');
  await page.locator('#btnBlankUndo').click();await idle();await waitOrder([3,1,4,2]);
  assert((await page.locator('#fileName').inputValue())==='勾选后保留人工名称','Restoration reset the document name');
  results.push('Pure manual deletion works without analysis; rename preserves selection and undo; reload retains undo and restores exact order without renaming');

  await change({candidates:[],analysisErrors:[]});
  const deletionCount=countRequests(await read(),'delete-selected');
  await page.locator('#btnMarkBlank').click();await idle();
  assert((await selected()).length===0 && await page.locator('#btnDeleteSelected').isDisabled(),'Zero candidates enabled deletion');
  assert((await page.locator('#blankStatus').innerText()).includes('未识别到空白页'),'Zero candidates have no explanation');
  await change({failNext:{path:'/scan/order-doc/blank-analysis',status:500,body:{error:'模拟识别失败，页面保留'}}});
  await page.locator('#btnMarkBlank').click();await idle();
  assert(countRequests(await read(),'delete-selected')===deletionCount,'Zero/error analysis issued deletion');
  results.push('Zero candidates and analysis failures keep pages and never submit deletion');

  for (const mutation of ['image','order','append','remove']) {
    await pick(3).check();
    data=await read();const doc=data.documents[0];
    if (mutation==='image') doc.page_details[2].revision++;
    if (mutation==='order') {doc.pages.reverse();doc.order_revision++;}
    if (mutation==='append') {doc.pages.push(5);doc.page_details[5]={revision:1,dpi:150};doc.order_revision++;}
    if (mutation==='remove') {doc.pages=doc.pages.filter(number=>number!==5);doc.order_revision++;}
    await change({document:doc});await refresh();
    await page.waitForFunction(()=>document.querySelectorAll('.page-check:checked').length===0);
    assert((await selected()).length===0 && await page.locator('#btnDeleteSelected').isDisabled(),mutation+' did not clear stale selection');
  }
  results.push('Polling clears old marks on any page image revision, order, addition or removal, including changes to an unselected page');

  await change({candidates:[1],analysisDelay:5500});
  await page.locator('#btnMarkBlank').click();
  await page.waitForFunction(()=>document.getElementById('btnMarkBlank').textContent==='正在标记…');
  assert(await pick(3).isDisabled(),'Analysis busy state left selection controls active');
  data=await read();const changedDoc=data.documents[0];changedDoc.pages.reverse();changedDoc.order_revision++;
  await change({document:changedDoc});
  await waitOrder(changedDoc.pages);await idle();
  assert((await selected()).length===0 && (await page.locator('#blankStatus').innerText()).includes('已变化'),'Late analysis marked a changed page snapshot');
  await change({analysisDelay:0,staleAnalysis:true});
  await page.locator('#btnMarkBlank').click();await idle();
  assert((await selected()).length===0,'Explicit stale analysis revision was applied');
  await change({staleAnalysis:false});
  await pick(3).check();const beforeConflict=await order();
  await change({staleDeleteOnce:true});
  await page.locator('#btnDeleteSelected').click();await idle();await refresh();
  assert(JSON.stringify(await order())===JSON.stringify(beforeConflict),'Conflict removed a page');
  assert((await selected()).length===0,'Conflict retained the obsolete selection');
  results.push('Late/stale analysis is discarded; full-snapshot delete conflict clears marks and refreshes without dropping pages');

  const online={state:'online',message:'扫描仪已连接',supported_dpi:[150,200,300],duplex_supported:true,supports_page_rescan:true};
  await change({scanner:{...online,state:'scanning'},active_id:'order-doc',document:{id:'order-doc',state:'scanning'}});
  await page.locator('#btnDeviceRefresh').click();await refresh();
  assert(await page.locator('#btnMarkBlank').isDisabled() && await pick(3).isDisabled(),'Active scan did not lock cleanup controls');
  await change({scanner:{state:'offline',message:'扫描仪未连接'},active_id:null,document:{id:'order-doc',state:'done'}});
  await page.locator('#btnDeviceRefresh').click();await refresh();
  await pick(3).check();await page.locator('#btnDeleteSelected').click();await idle();
  assert(!(await order()).includes(3),'Offline scanner prevented history cleanup');
  await page.locator('#btnBlankUndo').click();await idle();
  const allPages=await order();
  for (const number of allPages) await pick(number).check();
  await page.locator('#btnDeleteSelected').click();await idle();await waitOrder([]);
  assert(await page.locator('#btnBlankUndo').isVisible() && await page.locator('#btnPdf').isDisabled(),'Empty document lost undo or enabled PDF');
  await page.reload();await waitOrder([]);
  await page.locator('#btnBlankUndo').click();await idle();await waitOrder(allPages);
  results.push('Scanning locks edits; offline history cleanup works; deleting all pages still offers undo across reload');

  await page.setViewportSize({width:390,height:844});
  await page.locator('#grid .page-pick').first().click();
  assert((await selected()).length===1,'Mobile label tap did not select its checkbox');
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile document has horizontal overflow');
  assert(await page.locator('#grid .pg').evaluateAll(nodes=>nodes.every(node=>node.querySelector('.move-down').getBoundingClientRect().right<=node.getBoundingClientRect().right+1)),'Mobile page controls are clipped');
  await page.screenshot({path:'output/playwright/scanner-blank-controls-mobile.png',fullPage:true});
  data=await read();assert(data.clientErrors.length===0,'Browser script errors: '+data.clientErrors.join('; '));
  assert(data.logs.every(item=>!item.path.endsWith('/continue')&&!item.path.includes('/rescan/')&&item.path!=='/scan'),'Blank-page checks started a simulated scan unexpectedly');
  results.push('Mobile checkbox labels are usable without clipped controls or horizontal overflow; no browser exceptions or scan requests');
  const result={passed:true,checks:results,mockOrigin:page.url()};
  await page.evaluate(value=>{window.__scanUiBlankResult=value;},result);
  return result;
}
