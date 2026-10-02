const assert = require('node:assert/strict');
// Optional integration proof: requires ffmpeg and Playwright with WebKit installed.
// PLAYWRIGHT_MODULE may point to an existing Playwright installation.
const {webkit, devices}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {mkdtempSync, writeFileSync, rmSync}=require('node:fs');
const {tmpdir}=require('node:os');
const path=require('node:path');
const {spawn, spawnSync}=require('node:child_process');
const net=require('node:net');
const repo=path.resolve(__dirname,'..');
function run(command,args,env=process.env){
 const result=spawnSync(command,args,{env,encoding:'utf8'});
 assert.equal(result.status,0,result.stderr);
}

(async()=>{
 const temporary=mkdtempSync(path.join(tmpdir(),'goodeye-phone-'));
 let server, browser;
 try {
 const socket=net.createServer();
 await new Promise(resolve=>socket.listen(0,'127.0.0.1',resolve));
 const port=socket.address().port;
 await new Promise(resolve=>socket.close(resolve));
 const url=`http://127.0.0.1:${port}`;
 const env={...process.env,GOODEYE_HOME:path.join(temporary,'store'),GOODEYE_PORT:String(port)};
 const clip=path.join(temporary,'clip.mp4'), reasoning=path.join(temporary,'reasoning.json');
 run('ffmpeg',['-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10','-t','4','-c:v','libx264','-pix_fmt','yuv420p','-movflags','+faststart',clip]);
 writeFileSync(reasoning,JSON.stringify({summary:'Test video',decisions:[{choice:'Synthetic fixture',why:'Isolated playback regression test'}]}));
 server=spawn('python3',[path.join(repo,'goodeye.py'),'serve','--port',String(port)],{env,stdio:'ignore'});
 let ready=false;
 for(let attempt=0;attempt<100;attempt++){
  try {ready=(await fetch(url+'/api/items')).ok;}catch{}
  if(ready) break;
  await new Promise(resolve=>setTimeout(resolve,100));
 }
 assert.ok(ready,'Test server did not start');
 run('python3',[path.join(repo,'goodeye.py'),'submit',clip,'--id','test-video','--project','Test','--reasoning',reasoning],env);
 browser=await webkit.launch();
 for(const scenario of ['playback','blocked autoplay','network retry']){
  const context=await browser.newContext({...devices['iPhone 13']});
  await context.addInitScript(()=>localStorage.setItem('goodeye.coach','1'));
  if(scenario==='blocked autoplay') await context.addInitScript(()=>{
   const original=HTMLMediaElement.prototype.play;
   let unlocked=false;document.addEventListener('click',()=>{unlocked=true},{capture:true});
   HTMLMediaElement.prototype.play=function(){
    if(this.classList.contains('dvid')&&!unlocked){this.autoplay=false;this.pause();return Promise.reject(new DOMException('Test autoplay restriction','NotAllowedError'));}
    return original.call(this);
   };
  });
  const page=await context.newPage();
  await page.route('**/api/clientlog',route=>route.fulfill({status:200,body:'{}'}));
  let fail=scenario==='network retry';
  await page.route('**/files/**',route=> fail&&route.request().url().includes('.mp4')?route.fulfill({status:503,body:'Temporary failure'}):route.continue());
  await page.goto(url+'/#inbox');
  await page.waitForSelector('#deck .card2:not(.behind) video.dvid');
  if(scenario==='blocked autoplay'){
   await page.locator('#deck .card2:not(.behind) .vplay').click();
  }
  if(scenario==='network retry'){
   await page.waitForSelector('#deck .card2:not(.behind) .verr');
   fail=false;
   await page.locator('#deck .card2:not(.behind) .verr').click();
  }
  await page.waitForFunction(()=>{
   const v=document.querySelector('#deck .card2:not(.behind) video.dvid');return v&&v.readyState>=2&&v.currentTime>1&&!v.paused;
  },{},{timeout:20000});
  assert.equal(await page.locator('#deck .card2:not(.behind) .vplay').count(),0);
  console.log('PASS',scenario);
  await context.close();
 }

 // A lost POST response must be safe to retry without duplicating a verdict.
 const context=await browser.newContext({...devices['iPhone 13']});
 await context.addInitScript(()=>localStorage.setItem('goodeye.coach','1'));
 const page=await context.newPage();
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(url+'/#inbox');
 await page.waitForSelector('#deck .card2:not(.behind) video.dvid');
 let loseResponse=true;
 await page.route('**/api/decide',async route=>{
  if(loseResponse){await route.fetch();loseResponse=false;await route.abort();}
  else await route.continue();
 });
 await page.evaluate(async()=>{
  const it=item('test-video');
  queueDecision(it,{id:it.id,version:'v1',verdict:'changes',feedback:'Keep this feedback'},'Changes requested');
  const accepted=queueDecision(it,{id:it.id,version:'v1',verdict:'rejected',feedback:''},'Rejected');
  if(accepted!==false) throw new Error('Allowed a duplicate pending verdict');
  await commitDecision();
 });
 await page.waitForSelector('#save-errors [data-retry]');
 assert.equal(await page.evaluate(()=>sess.changes),0);
 assert.equal(await page.evaluate(()=>queueDecision(item('test-video'),{id:'test-video',version:'v1',verdict:'rejected',feedback:''},'Rejected')),false);
 assert.equal(await page.evaluate(()=>failedSaves.size),1);
 await page.locator('#save-errors [data-retry]').click();
 await page.waitForFunction(()=>sess.changes===1&&!failedSaves.size);
 let state=await (await fetch(url+'/api/items')).json();
 assert.equal(state.items[0].versions[0].decisions.length,1);
 assert.equal(state.items[0].versions[0].decisions[0].feedback,'Keep this feedback');
 console.log('PASS lost verdict response and idempotent retry');
 // A refresh failure after a saved verdict must not present a second-save retry.
 await page.route('**/api/items',route=>route.fulfill({status:503,body:'{}'}));
 await page.evaluate(async()=>{
  const it=item('test-video');
  queueDecision(it,{id:it.id,version:'v1',verdict:'approved',feedback:''},'Approved');
  await commitDecision();
 });
 assert.equal(await page.evaluate(()=>sess.approved),1);
 assert.equal(await page.evaluate(()=>failedSaves.size),0);
 assert.equal(await page.evaluate(()=>item('test-video').status),'approved');
 assert.deepEqual(errors,[]);
 console.log('PASS saved verdict survives failed refresh');
 await context.close();

 // Desktop stays on the item when advance is disabled. Repeated actions retain the note.
 run('python3',[path.join(repo,'goodeye.py'),'submit',clip,'--id','desktop-video','--project','Test','--reasoning',reasoning],env);
 const desktop=await browser.newContext({viewport:{width:1280,height:900}});
 const desktopPage=await desktop.newPage();
 await desktopPage.goto(url+'/#desktop-video');
 await desktopPage.waitForSelector('#fb');
 await desktopPage.evaluate(()=>{prefs.advance=false;});
 await desktopPage.locator('#fb').fill('First feedback');
 await desktopPage.locator('#main [data-d="changes"]').click();
 await desktopPage.locator('#fb').fill('Keep this second draft');
 await desktopPage.locator('#main [data-d="rejected"]').click();
 assert.equal(await desktopPage.locator('#fb').inputValue(),'Keep this second draft');
 assert.equal(await desktopPage.evaluate(()=>pendingDec.body.verdict),'changes');
 let releasePost;
 await desktopPage.route('**/api/decide',route=>new Promise(resolve=>{
  releasePost=async()=>{await route.continue();resolve();};
 }));
 await desktopPage.evaluate(()=>{commitDecision();});
 for(let attempt=0;attempt<100&&!releasePost;attempt++) await new Promise(resolve=>setTimeout(resolve,20));
 assert.ok(releasePost,'Save request did not start');
 await desktopPage.locator('#main [data-d="rejected"]').click();
 assert.equal(await desktopPage.locator('#fb').inputValue(),'Keep this second draft');
 assert.equal(await desktopPage.evaluate(()=>pendingDec),null);
 await releasePost();
 await desktopPage.waitForFunction(()=>!sending.size);
 console.log('PASS desktop duplicate actions preserve feedback');
 await desktop.close();
 } finally {
  if(browser) await browser.close();
  if(server){server.kill();await new Promise(resolve=>server.once('exit',resolve));}
  rmSync(temporary,{recursive:true,force:true});
 }
})().catch(e=>{console.error(e);process.exit(1)});
