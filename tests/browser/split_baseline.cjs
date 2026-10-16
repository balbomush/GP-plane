// S1 supplement: real installed v0.4.4 Core/proxy, synthetic state only.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const [url, runtime, output] = process.argv.slice(2);
assert.equal(new URL(url).hostname, '127.0.0.1');
assert(!fs.existsSync(output), 'preserve earlier results');
fs.mkdirSync(output, { recursive: true });
const lock = JSON.parse(fs.readFileSync(path.join(__dirname, 'toolchain.lock.json')));
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
assert.equal(sha(fs.readFileSync(process.execPath)), lock.node.nodeExeSha256);
const modulePath = path.join(runtime, 'playwright-project/node_modules', lock.playwright.package);
assert.equal(JSON.parse(fs.readFileSync(path.join(modulePath, 'package.json'))).version, lock.playwright.version);
process.env.PLAYWRIGHT_BROWSERS_PATH = path.join(runtime, 'browsers');
const { chromium } = require(modulePath);
const report = { result: 'FAIL', synthetic: true, node: process.version,
  playwright: lock.playwright.version, script_sha256: sha(fs.readFileSync(__filename)), checks: [], cleanup: {} };
let browser, context, token;
async function api(endpoint, payload, auth = token) {
  const response = await fetch(url + endpoint, {
    method: payload === undefined ? 'GET' : 'POST',
    headers: { ...(auth ? { Authorization: 'Bearer ' + auth } : {}), 'Content-Type': 'application/json' },
    body: payload === undefined ? undefined : JSON.stringify(payload), signal: AbortSignal.timeout(15000),
  });
  return { status: response.status, value: await response.json() };
}
function check(name) { report.checks.push(name); }
(async () => {
  try {
    browser = await chromium.launch({ headless: true, timeout: 15000 });
    context = await browser.newContext({ viewport: { width: 1365, height: 900 } });
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.name));
    const denied = await api('/api/core/status', undefined, 'invalid-synthetic-token');
    assert.equal(denied.status, 401);
    assert.equal((await api('/api/auth/login', { username: 'admin', password: 'wrong-synthetic' }, null)).status, 401);
    check('invalid bearer and invalid login rejected');
    await page.goto(url, { waitUntil: 'domcontentloaded' });
    assert.equal(await page.locator('#login-password').inputValue(), '');
    await page.locator('#login-username').fill('admin');
    await page.locator('#login-password').fill('admin');
    await page.locator('#login-form button[type=submit]').click();
    await page.locator('#app-shell').waitFor({ state: 'visible' });
    token = await page.evaluate(() => localStorage.getItem('gp-control-plane-auth-token'));
    assert(token);
    check('real UI login and installed shell bootstrap');
    assert.equal((await api('/api/internal/web-views/status')).status, 404);
    const spec = await (await fetch(url + '/openapi.json')).json();
    assert.equal(spec.info.version, '0.4.4');
    assert(!Object.keys(spec.paths).some(key => key.startsWith('/api/internal/')));
    check('public OpenAPI version and internal proxy boundary');
    await page.evaluate(() => {
      const original = realtimeController._onEvent;
      window.__s1Events = [];
      realtimeController._onEvent = function(event, data) {
        const result = original.call(this, event, data);
        window.__s1Events.push(event);
        return result;
      };
    });
    for (const tab of ['finder', 'terminal', 'history', 'candidates', 'lists', 'settings']) {
      await page.locator('#tab-' + tab).click();
      await page.locator('#tab-panel-' + tab).waitFor({ state: 'visible' });
    }
    check('all six real shell tabs');
    const saved = await api('/api/web/presets/save', { scope: 'finder', name: 's1-synthetic', kind: 'user', domains: ['example.test', 'example.org'] });
    assert.equal(saved.status, 200);
    const presets = await api('/api/core/presets/domain-lists');
    assert(presets.value.lists.some(item => item.name === 's1-synthetic'));
    await page.waitForFunction(() => window.__s1Events.length > 0, undefined, { timeout: 20000 });
    check('real persisted preset and parsed real SSE event');
    await page.locator('#tab-settings').click();
    const backupResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/core/backups/create' && response.request().method() === 'POST');
    await page.locator('[data-action=create-backup]').click();
    assert.equal((await backupResponse).status(), 201, 'backup creation returns Created');
    const backups = await api('/api/core/backups/list');
    const snapshot = (backups.value.snapshots || backups.value.backups)[0];
    assert(snapshot);
    const id = snapshot.snapshot_id || snapshot.id;
    const archive = await fetch(url + '/api/core/backups/download-archive?snapshot_id=' + encodeURIComponent(id), { headers: { Authorization: 'Bearer ' + token }, signal: AbortSignal.timeout(15000) });
    assert.equal(archive.status, 200);
    const bytes = Buffer.from(await archive.arrayBuffer());
    assert(bytes.length > 0);
    assert.equal(bytes.readUInt16LE(0), 0x4b50);
    report.synthetic_archive = { bytes: bytes.length, sha256: sha(bytes) };
    check('UI backup create and real archive download');
    const restored = await api('/api/core/backups/restore', { snapshot_id: id });
    assert.equal(restored.status, 202, 'restore returns Accepted');
    assert.equal(restored.value.accepted, true);
    assert.equal(restored.value.status, 'success');
    assert.equal((await api('/api/core/backups/delete', { snapshot_id: id })).status, 200);
    check('synthetic backup restore and delete through product API');
    await page.locator('#settings-current-password').fill('admin');
    await page.locator('#settings-new-password').fill('synthetic-S1-password');
    await page.locator('#change-password-form button[type=submit]').click();
    await page.locator('#login-form').waitFor({ state: 'visible' });
    assert.equal((await api('/api/core/status')).status, 401);
    check('UI password rotation returns to login and revokes old bearer');
    await page.locator('#login-username').fill('admin');
    await page.locator('#login-password').fill('synthetic-S1-password');
    await page.locator('#login-form button[type=submit]').click();
    await page.locator('#app-shell').waitFor({ state: 'visible' });
    await page.locator('[data-action=logout]').click();
    await page.locator('#login-form').waitFor({ state: 'visible' });
    assert.equal(await page.evaluate(() => localStorage.getItem('gp-control-plane-auth-token')), null);
    assert.equal(errors.length, 0);
    check('relogin, logout, token disposal, no uncaught browser errors');
    await page.screenshot({ path: path.join(output, 'synthetic-login.png') });
    report.result = 'PASS';
  } catch (error) {
    report.error = String(error.message).slice(0, 1000);
  } finally {
    token = null;
    try { if (context) await context.close(); report.cleanup.context_closed = true; }
    catch { report.cleanup.context_closed = false; report.result = 'FAIL'; }
    try { if (browser) await browser.close(); report.cleanup.browser_closed = true; }
    catch { report.cleanup.browser_closed = false; report.result = 'FAIL'; }
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify({ result: report.result, checks: report.checks, error: report.error }));
    process.exitCode = report.result === 'PASS' ? 0 : 1;
  }
})();
