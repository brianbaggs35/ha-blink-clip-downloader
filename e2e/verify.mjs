import { chromium } from 'playwright';

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
const errors = [];
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
page.on('console', (msg) => { if (msg.type() === 'error') errors.push(`console: ${msg.text()}`); });

await page.goto('http://localhost:8199/', { waitUntil: 'networkidle' });
await page.waitForTimeout(500);
await page.screenshot({ path: '/tmp/shots/01-library.png' });

// Matches AppSidebar.vue's TABS array (see smoke.mjs, which is kept in
// sync with the same source of truth for the CI-run check) - this list
// had gone stale from before the vehicles/biometrics tabs existed, so
// this manual verification helper was never actually screenshotting them.
const tabs = ['status', 'usage', 'automations', 'ai', 'models', 'vehicles', 'biometrics'];
for (const tab of tabs) {
  await page.click(`[data-tab="${tab}"]`);
  await page.waitForTimeout(700);
  await page.screenshot({ path: `/tmp/shots/02-${tab}.png` });
}

// theme toggle
await page.click('button[title="Toggle dark/light theme"]');
await page.waitForTimeout(300);
await page.click('[data-tab="library"]');
await page.waitForTimeout(300);
await page.screenshot({ path: '/tmp/shots/03-library-light.png' });

console.log('ERRORS:', JSON.stringify(errors, null, 2));
await browser.close();
