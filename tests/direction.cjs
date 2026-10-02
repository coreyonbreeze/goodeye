// Agentic direction: a real temporary store and CLI, no external generation or live agent dispatch.
const assert = require('node:assert/strict');
const {webkit, devices} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {mkdtempSync, writeFileSync, rmSync, mkdirSync} = require('node:fs');
const {tmpdir} = require('node:os');
const path = require('node:path');
const {spawn, spawnSync} = require('node:child_process');
const net = require('node:net');
const repo = path.resolve(__dirname, '..');
(async () => {
  const temporary = mkdtempSync(path.join(tmpdir(), 'goodeye-direction-'));
  let server, browser;
  try {
    const socket = net.createServer();
    await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
    const port = socket.address().port;
    await new Promise(resolve => socket.close(resolve));
    const url = `http://127.0.0.1:${port}`;
    const env = {...process.env, GOODEYE_HOME:path.join(temporary,'store'), GOODEYE_PORT:String(port)};
    const cli = (...args) => {
      const r = spawnSync('python3',[path.join(repo,'goodeye.py'),...args],{env,encoding:'utf8'});
      assert.equal(r.status,0,r.stderr); return r.stdout;
    };
    const post = async (route,body) => {
      const r=await fetch(url+route,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      assert.equal(r.status,200,await r.clone().text()); return r.json();
    };
    const state = async () => (await fetch(url+'/api/direction?project=Mosaic')).json();
    server=spawn('python3',[path.join(repo,'goodeye.py'),'serve'],{env,stdio:'ignore'});
    let ready=false;
    for(let n=0;n<100;n++) {try{ready=(await fetch(url+'/api/items')).ok;}catch{} if(ready)break;await new Promise(r=>setTimeout(r,100));}
    assert.ok(ready);
    const project=await post('/api/project',{name:'Mosaic',expected_revision:0});
    browser=await webkit.launch();
    const page=await browser.newPage({viewport:{width:1440,height:1000},colorScheme:'light'});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(url+'/#project/'+project.key);
    await page.locator('#profilebtn').click();
    await page.waitForFunction(()=>document.querySelector('#direction-prompt')?.value.includes('goodeye-brand'));
    assert.match(await page.locator('#direction-current').textContent(),/No approved direction/);
    assert.equal(await page.locator('#profile-dialog [name="colors"]').count(),0);
    await page.locator('.direction-sources summary').click();
    await page.locator('#direction-sources').fill('/project/brand/BRAND-LOOKBOOK.md\nApproved logo release and rejection notes.');
    await page.locator('#save-direction-sources').click();
    await page.waitForFunction(()=>document.querySelector('#source-result').textContent==='Saved');
    await page.locator('.direction-sources summary').click();
    await page.locator('#copy-direction-prompt').click();
    assert.match(await page.locator('#direction-result').textContent(),/copied|copy the starter/);
    if(process.env.SCREENSHOTS){mkdirSync(process.env.SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.SCREENSHOTS,'direction-empty.png')});}
    console.log('PASS evidence-first empty state, saved sources, and copyable product skill');

    let lose=true;
    await page.route('**/api/direction',async route=>{
      if(route.request().method()==='POST' && route.request().postDataJSON().action==='request' && lose){await route.fetch();lose=false;return route.abort();}
      return route.continue();
    });
    await page.locator('#direction-message').fill('Recover our existing identity before proposing changes.');
    await page.locator('#send-direction').click();
    await page.waitForFunction(()=>document.querySelector('#direction-result').textContent.includes('Save unconfirmed'));
    assert.match(await page.locator('#direction-message').inputValue(),/Recover our existing/);
    await page.locator('#send-direction').click();
    await page.waitForFunction(()=>document.querySelector('#direction-result').textContent.startsWith('Request saved'));
    assert.equal((await state()).requests.length,1);
    assert.equal((await state()).requests[0].status,'waiting');
    assert.equal((await state()).revision,1);
    cli('direction','join','--project','Mosaic','--as','brand','--runtime','pull');
    let current=(await state()).requests.at(-1);
    cli('direction','update','--project','Mosaic','--request',current.id,'--status','needs_input','--message','Should the new direction still prioritize shop owners?');
    await page.locator('#direction-message').fill('Keep the original audience.');
    await page.evaluate(()=>load(true));
    await page.waitForFunction(()=>document.querySelector('#direction-history').textContent.includes('prioritize shop owners'));
    assert.equal(await page.locator('#direction-message').inputValue(),'Keep the original audience.');
    await page.locator('.direction-sources summary').click();
    await page.locator('#direction-sources').fill('/repo/my-source-draft.md');
    cli('direction','update','--project','Mosaic','--request',current.id,'--status','working','--message','Reviewing a second source.');
    await page.evaluate(()=>load(true));
    await page.locator('#save-direction-sources').click();
    await page.waitForFunction(()=>document.querySelector('#source-result').textContent==='Saved');
    assert.equal((await state()).sources,'/repo/my-source-draft.md');
    await page.locator('#direction-sources').fill('/repo/my-second-draft.md');
    const srcState=await state();
    await post('/api/direction',{project:'Mosaic',action:'sources',sources:'/repo/someone-else.md',expected_source_revision:srcState.source_revision});
    await page.locator('#save-direction-sources').click();
    await page.waitForSelector('#source-conflict:not([hidden])');
    assert.equal(await page.locator('#direction-sources').inputValue(),'/repo/my-second-draft.md');
    assert.equal(await page.locator('#saved-direction-sources').inputValue(),'/repo/someone-else.md');
    await page.locator('#replace-direction-sources').click();
    await page.waitForFunction(()=>document.querySelector('#source-result').textContent==='Saved');
    assert.equal((await state()).sources,'/repo/my-second-draft.md');
    await page.unroute('**/api/direction');
    let releaseSources;
    await page.route('**/api/direction',route=>new Promise(resolve=>{releaseSources=async()=>{await route.continue();resolve();};}));
    await page.locator('#direction-sources').fill('/repo/saved-version.md');
    await page.locator('#save-direction-sources').click();
    for(let n=0;n<100&&!releaseSources;n++)await new Promise(r=>setTimeout(r,20));
    assert.ok(releaseSources);
    await page.locator('#direction-sources').fill('/repo/newer-unsaved-version.md');
    await releaseSources();
    await page.waitForFunction(()=>document.querySelector('#source-result').textContent.includes('newer edits'));
    assert.equal(await page.locator('#direction-sources').inputValue(),'/repo/newer-unsaved-version.md');
    await page.unroute('**/api/direction');
    await page.locator('#save-direction-sources').click();
    await page.waitForFunction(()=>document.querySelector('#source-result').textContent==='Saved');
    assert.equal((await state()).sources,'/repo/newer-unsaved-version.md');
    await page.locator('.direction-sources summary').click();

    await page.locator('#send-direction').click();
    await page.waitForFunction(()=>document.querySelector('#direction-message').value==='');
    current=(await state()).requests.at(-1);
    assert.equal((await state()).requests[0].status,'superseded');
    console.log('PASS saved requests, lost-response retry, resumed agent, and live draft preservation');

    const proposalFile=path.join(temporary,'proposal.json');
    const propose = (phase,profile,artifacts=[]) => {
      writeFileSync(proposalFile,JSON.stringify({phase,title:'A useful identity for working shops',rationale:'Specific product behavior shapes the identity.',evidence:['Existing approved brand brief; original shop-owner audience.'],profile,artifacts}));
      cli('direction','propose','--project','Mosaic','--request',current.id,'--file',proposalFile);
    };
    propose('strategy',{strategy:'Useful answers with visible evidence, built for shop owners.'});
    await page.evaluate(()=>load(true));
    await page.waitForSelector('[data-accept-direction]');
    assert.equal((await state()).revision,1);
    await page.locator('[data-accept-direction]').click();
    await page.waitForFunction(()=>document.querySelector('#direction-revision').textContent==='r2');
    assert.equal((await state()).requests.at(-1).status==='waiting' || (await state()).requests.at(-1).status==='queued',true);
    current=(await state()).requests.at(-1);
    const image=path.join(temporary,'study.svg'),reason=path.join(temporary,'reason.json');
    writeFileSync(image,'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="450"><rect width="800" height="450" fill="#eee6cd"/><text x="60" y="230" font-family="Georgia" font-size="65" fill="#245846">A clear point of view.</text></svg>');
    writeFileSync(reason,JSON.stringify({summary:'Synthetic visual test fixture',decisions:[{choice:'Known reference',why:'Exercise artifact review without an external model call'}]}));
    cli('submit',image,'--id','direction-study','--project','Mosaic','--collection','Brand','--reasoning',reason);
    propose('concept',{colors:'Green ink and warm paper.'},[{id:'direction-study',version:'v1',origin:'reference',provenance:'Synthetic reference fixture for integration testing. No image generation claimed.'}]);
    await page.evaluate(()=>load(true));
    await page.waitForSelector('.direction-artifacts img');
    assert.equal(await page.locator('.direction-artifacts img').evaluate(el=>el.complete&&el.naturalWidth>0),true);
    if(process.env.SCREENSHOTS){await page.screenshot({path:path.join(process.env.SCREENSHOTS,'direction-proposal.png')});}
    await page.locator('[data-accept-direction]').click();
    await page.waitForFunction(()=>document.querySelector('#direction-revision').textContent==='r3');
    current=(await state()).requests.at(-1);
    propose('system',{typography:'An inspected production type system.',voice:'Direct and specific.'});
    await page.evaluate(()=>load(true));await page.waitForSelector('[data-accept-direction]');
    await page.locator('[data-accept-direction]').click();
    await page.waitForFunction(()=>document.querySelector('#direction-revision').textContent==='r4');
    assert.equal((await state()).requests.at(-1).status,'accepted');
    await page.locator('#direction-scope').selectOption('voice');
    await page.locator('#direction-message').fill('Make the writing less formal. Keep everything visual.');
    await page.locator('#send-direction').click();
    await page.waitForFunction(()=>document.querySelector('#direction-message').value==='');
    assert.equal((await state()).requests.at(-1).scope,'voice');
    assert.equal((await state()).profile.colors,'Green ink and warm paper.');
    console.log('PASS strategy-to-visual-to-system approvals and scoped steering in one conversation');

    await page.locator('#close-direction').click();
    const phone=await browser.newPage({...devices['iPhone 13'],colorScheme:'light'});
    phone.on('pageerror',e=>errors.push(e.message));
    await phone.addInitScript(() => Object.defineProperty(Crypto.prototype, 'randomUUID', {value:undefined, configurable:true}));
    await phone.goto(url+'/#project/'+project.key);
    await phone.locator('#qbtn').click();await phone.locator('#profilebtn').click();
    await phone.waitForFunction(()=>document.querySelector('#direction-history')?.textContent.includes('less formal'));
    await phone.locator('#direction-message').fill('Phone draft stays here.');
    assert.equal(await phone.locator('#profile-dialog').evaluate(el=>el.scrollWidth<=el.clientWidth),true);
    await phone.locator('#close-direction').click();await phone.locator('#profilebtn').click();
    assert.equal(await phone.locator('#direction-message').inputValue(),'Phone draft stays here.');
    await phone.locator('#direction-message').scrollIntoViewIfNeeded();
    if(process.env.SCREENSHOTS)await phone.screenshot({path:path.join(process.env.SCREENSHOTS,'direction-phone.png')});
    await phone.locator('#send-direction').click();
    await phone.waitForFunction(()=>document.querySelector('#direction-message').value==='');
    assert.equal((await state()).requests.at(-1).message,'Phone draft stays here.');
    assert.deepEqual(errors,[]);
    console.log('PASS mobile conversation and per-project draft recovery');
  } finally {
    if(browser)await browser.close();
    if(server){server.kill();await new Promise(r=>server.once('exit',r));}
    rmSync(temporary,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
