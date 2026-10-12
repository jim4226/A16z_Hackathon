// Browser regression: first launch, private key UI, create/copy/export and reconnect.
// npm install --no-save playwright@1.62.1 && npx playwright install chromium
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { spawn } = require('node:child_process');

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'rcubed-browser-'));
  let server, browser;
  try {
    let url = process.env.RCUBED_TEST_URL;
    if (!url) {
      const port = await new Promise(resolve => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); }); });
      server = spawn(process.env.RCUBED_TEST_PYTHON || 'python', ['-m', 'morph.cli', 'studio', '--port', String(port)], {
        cwd: path.resolve(__dirname, '..'), windowsHide: true, stdio: 'ignore',
        env: { ...process.env, PYTHONUTF8: '1', MORPH_CONFIG_DIR: path.join(temp, 'config'), MORPH_HOME: path.join(temp, 'projects'),
          MORPH_OFFLINE: '1', MORPH_LOAD_DOTENV: '0', PYTHON_KEYRING_BACKEND: 'keyring.backends.null.Keyring',
          ANTHROPIC_API_KEY: '', KEEWANO_API_KEY: '', KEEWANO_PAT: '', KEEWANO_PROJECT_ID: '' },
      });
      server.on('error', e => { throw e; }); url = `http://127.0.0.1:${port}`;
      let available = false;
      for (let i = 0; i < 120; i++) {
        try { if ((await fetch(url)).ok) { available = true; break; } } catch {}
        if (server.exitCode !== null) throw new Error('Local Studio exited during startup');
        await new Promise(r => setTimeout(r, 500));
      }
      assert(available, 'Local Studio did not start');
    }
    browser = await chromium.launch({ headless: true, ...(process.env.RCUBED_BROWSER_CHANNEL ? { channel: process.env.RCUBED_BROWSER_CHANNEL } : {}) });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
    const external = [], errors = [];
    await context.route('**/*', route => { const u = new URL(route.request().url());
      if (u.hostname === '127.0.0.1' || u.hostname === 'localhost' || u.protocol === 'data:') return route.continue();
      external.push(u.origin); return route.abort();
    });
    const page = await context.newPage(); page.on('pageerror', e => errors.push(e.message));
    await page.goto(url); await page.locator('#newProjectDialog[open]').waitFor();
    assert.equal(await page.title(), 'R Cubed Studio');
    await page.getByRole('button', { name: 'Connect Claude', exact: true }).click();
    await page.locator('#providerStatus').filter({ hasText: 'offline rules' }).waitFor();
    assert.equal(await page.locator('#providerKey').getAttribute('type'), 'password');
    await page.locator('#providerKey').fill('sk-ant-browser-fixture-not-a-real-key');
    await page.locator('#providerStorage').selectOption('session');
    await page.getByRole('button', { name: 'Save key', exact: true }).click();
    await page.locator('#providerStatus').filter({ hasText: 'this session only' }).waitFor();
    assert.equal(await page.locator('#providerKey').inputValue(), '');
    assert.equal(await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } })).then(t => t.includes('sk-ant-')), false);
    const exposed = await page.evaluate(async () => JSON.stringify(await (await fetch('/api/provider')).json()));
    assert(!exposed.includes('sk-ant-'), 'credential exposed in status');
    await page.locator('#providerModel').fill('unsaved-model-selection');
    await page.locator('#sourcingEnabled').check();
    await page.getByRole('button', { name: 'Test connection', exact: true }).click();
    await page.locator('#settingsMessage').filter({ hasText: 'Enable network access' }).waitFor();
    await page.waitForFunction(() => !document.querySelector('#providerTest').disabled);
    assert.equal(await page.locator('#providerModel').inputValue(), 'unsaved-model-selection');
    assert.equal(await page.locator('#sourcingEnabled').isChecked(), true);
    if (process.env.RCUBED_SCREENSHOT) await page.screenshot({ path: process.env.RCUBED_SCREENSHOT, fullPage: true });
    await page.getByRole('button', { name: 'Remove key', exact: true }).click();
    await page.locator('#providerStatus').filter({ hasText: 'offline rules' }).waitFor();
    await page.locator('#settingsDialog [data-close]').click();
    await page.getByRole('button', { name: 'New project', exact: true }).click();
    await page.locator('#newProjectName').fill('browser-example');
    await page.getByRole('button', { name: 'Create project', exact: true }).click();
    await page.waitForFunction(() => window.morph?.state.project === 'browser-example' && window.morph.state.current && !window.morph.state.busy, null, { timeout: 180000 });
    assert.equal(await page.locator('#presentBtn').isVisible(), false);
    await page.locator('#input').fill('make it work for a strawberry'); await page.locator('#send').click();
    await page.waitForFunction(() => window.morph?.state.current?.rev === 'r002' && !window.morph.state.busy, null, { timeout: 180000 });
    const [download] = await Promise.all([page.waitForEvent('download'), page.getByRole('link', { name: 'Export revision' }).click()]);
    assert(download.suggestedFilename().endsWith('.zip')); assert.equal(await download.failure(), null);
    await page.getByRole('button', { name: 'New project', exact: true }).click();
    await page.locator('#newProjectName').fill('browser-copy'); await page.locator('#newProjectTemplate').selectOption('duplicate');
    await page.getByRole('button', { name: 'Create project', exact: true }).click();
    await page.waitForFunction(() => window.morph?.state.project === 'browser-copy' && !window.morph.state.busy, null, { timeout: 30000 });
    await page.reload();
    await page.waitForFunction(() => window.morph?.state.project === 'browser-copy' && window.morph.state.current);
    await page.evaluate(() => window.morph.onEvent({ type: 'node', node: 'wiring', status: 'error' }));
    assert.equal(await page.locator('[data-node="wiring"] .ns span').textContent(), 'failed');
    assert.deepEqual(external, [], 'unexpected external browser request'); assert.deepEqual(errors, []);
    console.log('Browser smoke passed: onboarding, session key privacy, offline test, new/edit/copy, export, refresh, failed-stage labels.');
  } finally {
    if (browser) await browser.close();
    if (server) { server.kill(); await new Promise(resolve => { if (server.exitCode !== null) resolve(); else { server.once('exit', resolve); setTimeout(resolve, 3000); } }); }
    // Keep failed test state for diagnosis. No credentials are real or persisted by this test.
  }
})().catch(e => { console.error(e.message); process.exitCode = 1; });
