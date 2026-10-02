// Review-studio interactions and responsive layout. Uses an isolated demo store.
const assert = require('node:assert/strict');
const {webkit, devices} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {mkdtempSync, writeFileSync, rmSync} = require('node:fs');
const {tmpdir} = require('node:os');
const path = require('node:path');
const {spawn, spawnSync} = require('node:child_process');
const net = require('node:net');
const repo = path.resolve(__dirname, '..');

(async () => {
  const temporary = mkdtempSync(path.join(tmpdir(), 'goodeye-studio-'));
  let server, browser;
  try {
    const socket = net.createServer();
    await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
    const port = socket.address().port;
    await new Promise(resolve => socket.close(resolve));
    const url = `http://127.0.0.1:${port}`;
    const env = {...process.env, GOODEYE_HOME: path.join(temporary, 'store'), GOODEYE_PORT: String(port)};
    const cli = (...args) => {
      const result = spawnSync('python3', [path.join(repo, 'goodeye.py'), ...args], {env, encoding: 'utf8'});
      assert.equal(result.status, 0, result.stderr);
    };
    server = spawn('python3', [path.join(repo, 'goodeye.py'), 'serve'], {env, stdio: 'ignore'});
    let ready = false;
    for (let attempt = 0; attempt < 100; attempt++) {
      try { ready = (await fetch(url + '/api/health')).ok; } catch {}
      if (ready) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.ok(ready, 'Board did not start');
    cli('demo');
    const reasoning = path.join(temporary, 'reasoning.json');
    writeFileSync(reasoning, JSON.stringify({summary:'Second project', decisions:[{choice:'Same slot name', why:'Projects must remain separate'}]}));
    const demo = (await (await fetch(url + '/api/items')).json()).items.find(i => i.id === 'demo-banner');
    cli('submit', path.join(env.GOODEYE_HOME, 'assets', demo.versions[0].dir, demo.versions[0].file), '--id', 'other-hero',
        '--project', 'Other', '--reasoning', reasoning, '--slot', 'demo-hero');
    browser = await webkit.launch();
    const page = await browser.newPage({viewport: {width: 1512, height: 982}, colorScheme: 'light'});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url + '/#inbox');
    await page.waitForSelector('#fb');
    await page.evaluate(() => select('demo-banner'));
    await page.waitForSelector('#fb');
    assert.equal(await page.locator('#list .row').count(), 5);
    assert.equal(await page.locator('#list .slothead').count(), 2);
    await page.locator('#queue-search').fill('icon');
    assert.equal(await page.locator('#list .row').count(), 1);
    await page.locator('#queue-search').fill('jjk');
    assert.equal(await page.evaluate(() => item(sel.id).asset_id), 'demo-banner');
    assert.equal(await page.locator('#list .row').count(), 0);
    await page.locator('#queue-search').fill('');
    await page.locator('#project-filter').selectOption({label:'Other'});
    assert.equal(await page.locator('#list .row').count(), 1);
    assert.equal(await page.locator('#list .row').getAttribute('data-asset-id'), 'other-hero');
    await page.locator('#project-filter').selectOption('__all');
    console.log('PASS search, project filter, and separate slot groups');

    await page.locator('#list [data-asset-id="demo-banner"]').click();
    await page.locator('#fb').fill('Banner feedback');
    await page.locator('#list [data-asset-id="demo-icon"]').click();
    assert.equal(await page.locator('#fb').inputValue(), '');
    await page.locator('#fb').fill('Icon feedback');
    await page.locator('#list [data-asset-id="demo-banner"]').click();
    assert.equal(await page.locator('#fb').inputValue(), 'Banner feedback');
    await page.locator('[data-v="v1"]').click();
    assert.equal(await page.locator('#fb').inputValue(), '');
    await page.locator('[data-v="v2"]').click();
    assert.equal(await page.locator('#fb').inputValue(), 'Banner feedback');
    console.log('PASS feedback drafts stay with their asset and version');

    await page.locator('#shortcutsbtn').click();
    await page.keyboard.press('Alt+a');
    assert.equal(await page.evaluate(() => pendingDec), null);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#shortcuts').evaluate(el => el.open), false);
    await page.keyboard.press('/');
    assert.equal(await page.locator('#queue-search').evaluate(el => el === document.activeElement), true);
    await page.locator('#list [data-asset-id="demo-icon"]').focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(() => item(sel.id).asset_id), 'demo-icon');
    await page.locator('#list [data-asset-id="demo-banner"]').click();
    await page.locator('[data-v="v1"]').focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(() => sel.version), 'v1');
    await page.locator('[data-v="v2"]').click();
    await page.locator('.checks summary').click();
    assert.equal(await page.locator('.checks').evaluate(el => el.open), true);
    await page.locator('.checks summary').click();
    console.log('PASS keyboard controls, safe shortcut dialog, and placement details');

    for (const width of [1280, 1512]) {
      await page.setViewportSize({width, height: 800});
      await page.waitForTimeout(100);
      const reject = await page.locator('#main [data-d="rejected"]').boundingBox();
      const preview = await page.locator('.stage').boundingBox();
      const actions = await page.locator('.decide').boundingBox();
      assert.ok(reject.y + reject.height <= 800, 'Review actions below viewport');
      assert.ok(preview.y + preview.height <= actions.y + 1, 'Actions obscure preview');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    }
    await page.emulateMedia({colorScheme: 'dark', reducedMotion: 'reduce'});
    assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme), 'dark');
    console.log('PASS compact desktop layout and dark theme');
    await page.close();

    const phone = await browser.newPage({...devices['iPhone 13'], colorScheme:'light'});
    phone.on('pageerror', error => errors.push(error.message));
    await phone.addInitScript(() => localStorage.setItem('goodeye.coach', '1'));
    await phone.goto(url + '/#inbox');
    await phone.waitForSelector('#deck .card2');
    assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.ok((await phone.locator('#topbar').boundingBox()).height <= 70);
    await phone.locator('#qbtn').click();
    await phone.locator('#project-filter').selectOption({label:'Other'});
    await phone.locator('#qbtn').click();
    assert.equal(await phone.locator('#list .row').count(), 1);
    await phone.locator('#list .row').click();
    assert.equal(await phone.locator('#main h2').innerText(), 'other-hero');
    console.log('PASS phone drawer, filtering, and review navigation');
    assert.deepEqual(errors, []);
  } finally {
    if (browser) await browser.close();
    if (server) { server.kill(); await new Promise(resolve => server.once('exit', resolve)); }
    rmSync(temporary, {recursive:true, force:true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
