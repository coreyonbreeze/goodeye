// Project routing, collection organization, profile editing, and isolation in WebKit.
const assert = require('node:assert/strict');
const {webkit, devices} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {mkdtempSync, writeFileSync, rmSync, mkdirSync} = require('node:fs');
const {tmpdir} = require('node:os');
const path = require('node:path');
const {spawn, spawnSync} = require('node:child_process');
const net = require('node:net');
const repo = path.resolve(__dirname, '..');
(async () => {
  const temporary = mkdtempSync(path.join(tmpdir(), 'goodeye-projects-'));
  let server, browser;
  try {
    const socket = net.createServer();
    await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
    const port = socket.address().port;
    await new Promise(resolve => socket.close(resolve));
    const url = `http://127.0.0.1:${port}`;
    const env = {...process.env, GOODEYE_HOME: path.join(temporary, 'store'), GOODEYE_PORT: String(port)};
    const cli = (...args) => {
      const r = spawnSync('python3', [path.join(repo, 'goodeye.py'), ...args], {env, encoding:'utf8'});
      assert.equal(r.status, 0, r.stderr);
    };
    server = spawn('python3', [path.join(repo, 'goodeye.py'), 'serve'], {env, stdio:'ignore'});
    let ready = false;
    for (let n = 0; n < 100; n++) {
      try { ready = (await fetch(url + '/api/items')).ok; } catch {}
      if (ready) break;
      await new Promise(r => setTimeout(r, 100));
    }
    assert.ok(ready);
    browser = await webkit.launch();
    const page = await browser.newPage({viewport:{width:1512, height:982}, colorScheme:'light'});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.locator('#new-project').click();
    await page.locator('[name="name"]').fill('Mosaic');
    await page.locator('[name="description"]').fill('Brand, website, and campaigns.');
    await page.locator('[name="collections"]').fill('Brand\nWebsite\nCampaigns');
    await page.locator('[name="colors"]').fill('Approved greens with warm neutral backgrounds.');
    await page.locator('[name="visual_rules"]').fill('Keep the layout calm and the message clear.');
    await page.locator('#profile-dialog [type="submit"]').click();
    await page.waitForFunction(() => projectFilter === 'Mosaic' && !document.querySelector('#profile-dialog').open);
    assert.equal(await page.locator('#workspace-title').textContent(), 'Mosaic');
    await page.reload();
    await page.waitForFunction(() => projectFilter === 'Mosaic' && !homeMode);
    await page.locator('#projectsbtn').click();
    await page.locator('#new-project').click();
    await page.locator('[name="name"]').fill('Castle Heist');
    await page.locator('[name="icon"]').fill('CH');
    await page.locator('[name="accent"]').fill('#92714e');
    await page.locator('[name="description"]').fill('Characters, worlds, and the details that bring them to life.');
    await page.locator('[name="collections"]').fill('Characters\nEnvironments\nUI\nAnimation');
    await page.locator('[name="visual_rules"]').fill('Readable silhouettes. Consistent proportions.');
    await page.locator('#profile-dialog [type="submit"]').click();
    await page.waitForFunction(() => projectFilter === 'Castle Heist' && !document.querySelector('#profile-dialog').open);
    console.log('PASS create projects and reload an empty workspace');

    const svg = path.join(temporary, 'hero.svg'), reasoning = path.join(temporary, 'reasoning.json');
    writeFileSync(svg, '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="600"><rect width="1000" height="600" fill="#e4e9dc"/><circle cx="500" cy="245" r="100" fill="#456a51"/><text x="500" y="430" text-anchor="middle" fill="#294333" font-family="Georgia" font-size="56">Room for something good.</text></svg>');
    writeFileSync(reasoning, JSON.stringify({summary:'A focused concept for review.', decisions:[{choice:'One central form', why:'A clear hierarchy at every size'}]}));
    for (const [project, title, collection, watcher] of [['Mosaic','Mosaic launch hero','Website','mosaic-agent'],['Castle Heist','Castle Heist menu','UI','castle-agent']]) {
      cli('submit', svg, '--project', project, '--id', 'hero', '--title', title, '--collection', collection, '--reasoning', reasoning);
      cli('watch', '--project', project, '--as', watcher, '--runtime', 'pull');
      cli('claim', 'hero', '--project', project, '--as', watcher);
    }
    await page.evaluate(() => load(true));
    await page.locator('#projectsbtn').click();
    assert.equal(await page.locator('.project-card').count(), 2);
    if (process.env.SCREENSHOTS) {
      await page.waitForFunction(() => !document.querySelector("#toast").classList.contains("show"));
      mkdirSync(process.env.SCREENSHOTS, {recursive:true});
      await page.screenshot({path:path.join(process.env.SCREENSHOTS, 'projects.png'), fullPage:true});
      await page.emulateMedia({colorScheme:'dark'});
      await page.screenshot({path:path.join(process.env.SCREENSHOTS, 'projects-dark.png'), fullPage:true});
      await page.emulateMedia({colorScheme:'light'});
    }
    await page.getByRole('button', {name:/Mosaic.*Brand, website/}).click();
    assert.equal(await page.locator('#list .row').count(), 1);
    assert.equal(await page.locator('#main h2').textContent(), 'Mosaic launch hero');
    assert.match(await page.locator('#agentline').textContent(), /mosaic-agent/);
    assert.doesNotMatch(await page.locator('#agentline').textContent(), /castle-agent/);
    assert.match(await page.locator('#main .head').textContent(), /Owned by mosaic-agent/);
    await page.locator('#fb').fill('Mosaic feedback only');
    await page.locator('#project-filter').selectOption({label:'Castle Heist'});
    assert.equal(await page.locator('#main h2').textContent(), 'Castle Heist menu');
    assert.equal(await page.locator('#fb').inputValue(), '');
    assert.doesNotMatch(await page.locator('#agentline').textContent(), /mosaic-agent/);
    await page.locator('#fb').fill('Castle feedback only');
    await page.locator('#project-filter').selectOption({label:'Mosaic'});
    assert.equal(await page.locator('#fb').inputValue(), 'Mosaic feedback only');
    await page.locator('[data-collection-item]').selectOption('Campaigns');
    await page.waitForFunction(() => item(sel.id).collection === 'Campaigns');
    await page.locator('#collection-filter').selectOption({label:'Website'});
    assert.equal(await page.locator('#list .row').count(), 0);
    await page.locator('#collection-filter').selectOption({label:'Campaigns'});
    assert.equal(await page.locator('#list .row').count(), 1);
    console.log('PASS same-ID drafts, agents, claims, and collection isolation');

    await page.locator('#profilebtn').click();
    await page.locator('[name="colors"]').fill('Updated palette for future work.');
    await page.keyboard.press('Alt+a');
    assert.equal(await page.evaluate(() => pendingDec), null);
    await page.locator('#profile-dialog [type="submit"]').click();
    await page.waitForFunction(() => !document.querySelector('#profile-dialog').open);
    await page.locator('.profile-snapshot summary').click();
    assert.match(await page.locator('.profile-snapshot').textContent(), /Approved greens/);
    assert.doesNotMatch(await page.locator('.profile-snapshot').textContent(), /Updated palette/);
    assert.match(await page.locator('.asset-organization').textContent(), /direction r1 · current r2/);
    if (process.env.SCREENSHOTS) await page.screenshot({path:path.join(process.env.SCREENSHOTS, 'project-workspace.png'), fullPage:true});
    await page.locator('.profile-snapshot summary').click();
    let releaseProfile;
    await page.route('**/api/project', route => new Promise(resolve => {
      releaseProfile = async () => { await route.continue(); resolve(); };
    }));
    await page.locator('#profilebtn').click();
    await page.locator('[name="voice"]').fill('Clear and direct.');
    await page.locator('#profile-dialog [type="submit"]').click();
    for (let n = 0; n < 100 && !releaseProfile; n++) await new Promise(r => setTimeout(r, 20));
    assert.ok(releaseProfile);
    await page.locator('#close-profile').click();
    await page.locator('#project-filter').selectOption({label:'Castle Heist'});
    await page.locator('#profilebtn').click();
    await page.locator('[name="voice"]').fill('Unsaved Castle Heist direction');
    await releaseProfile();
    await page.waitForFunction(() => projects.find(p => p.name === 'Mosaic').revision === 3);
    assert.equal(await page.locator('#profile-dialog').evaluate(el => el.open), true);
    assert.equal(await page.locator('[name="voice"]').inputValue(), 'Unsaved Castle Heist direction');
    assert.equal(await page.evaluate(() => projectFilter), 'Castle Heist');
    await page.unroute('**/api/project');
    await page.locator('#close-profile').click();
    await page.locator('#project-filter').selectOption({label:'Mosaic'});
    console.log('PASS delayed profile save preserves another editor');
    await page.evaluate(() => { const it = item(sel.id); queueDecision(it, {id:it.id, version:'v1', verdict:'approved', feedback:''}, 'Approved', 4000); return commitDecision(); });
    const state = await (await fetch(url + '/api/items')).json();
    assert.equal(state.items.find(i => i.project === 'Mosaic').status, 'approved');
    assert.equal(state.items.find(i => i.project === 'Castle Heist').status, 'pending');
    const castle = state.items.find(i => i.project === 'Castle Heist');
    await page.goto(url + '/#' + encodeURIComponent(castle.key));
    await page.waitForFunction(() => sel.id && projectFilter === 'Castle Heist');
    assert.equal(await page.locator('#list .row').count(), 1);
    await page.goto(url + '/#hero');
    await page.waitForSelector('.project-card');
    assert.equal(await page.evaluate(() => homeMode), true);
    await page.evaluate(() => { openWorkspace('Castle Heist'); showIdle(); });
    assert.doesNotMatch(await page.locator('#side').textContent(), /Mosaic/);
    await page.evaluate(() => { openWorkspace('Mosaic'); showIdle(); });
    await page.locator('#side [data-go]').click();
    assert.equal(await page.locator('#main h2').textContent(), 'Mosaic launch hero');
    console.log('PASS immutable profile display, isolated verdicts, recent history, and safe deep links');

    const phone = await browser.newPage({...devices['iPhone 13'], colorScheme:'light'});
    phone.on('pageerror', error => errors.push(error.message));
    await phone.addInitScript(() => localStorage.setItem('goodeye.coach', '1'));
    await phone.goto(url);
    await phone.waitForSelector('.project-card');
    if (process.env.SCREENSHOTS) await phone.screenshot({path:path.join(process.env.SCREENSHOTS, 'projects-phone.png'), fullPage:true});
    await phone.getByRole('button', {name:/Castle Heist.*Characters, worlds/}).click();
    await phone.waitForSelector('#deck .card2');
    assert.match(await phone.locator('#deck').textContent(), /Castle Heist menu/);
    assert.doesNotMatch(await phone.locator('#deck').textContent(), /Mosaic/);
    assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await phone.locator('#qbtn').click();
    await phone.locator('#profilebtn').click();
    assert.equal(await phone.locator('#profile-dialog').evaluate(el => el.scrollWidth <= innerWidth), true);
    await phone.locator('#close-profile').click();
    await phone.locator('#projectsbtn').click();
    await phone.waitForSelector('.project-card');
    cli('submit', svg, '--project', 'Castle Heist', '--id', 'other', '--title', 'Another castle asset', '--reasoning', reasoning);
    await phone.goto(url + '/#' + encodeURIComponent(castle.key));
    await phone.waitForSelector('#main h2');
    assert.equal(await phone.locator('#main h2').textContent(), 'Castle Heist menu');
    assert.equal(await phone.evaluate(() => deckMode), false);
    const mosaic = state.items.find(i => i.project === 'Mosaic');
    await phone.goto(url + '/#' + encodeURIComponent(mosaic.key));
    await phone.waitForSelector('#main h2');
    assert.equal(await phone.locator('#main h2').textContent(), 'Mosaic launch hero');
    assert.deepEqual(errors, []);
    console.log('PASS phone project navigation and profile editor');
  } finally {
    if (browser) await browser.close();
    if (server) { server.kill(); await new Promise(resolve => server.once('exit', resolve)); }
    rmSync(temporary, {recursive:true, force:true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
