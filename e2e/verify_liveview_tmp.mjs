import { chromium } from 'playwright';
import { mkdirSync } from 'fs';

mkdirSync('/tmp/shots', { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
const errors = [];
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
page.on('console', (msg) => { if (msg.type() === 'error') errors.push(`console: ${msg.text()}`); });

await page.goto('http://localhost:8199/', { waitUntil: 'networkidle' });
await page.waitForTimeout(500);

const tabTexts = await page.locator('.app-nav-tab').allTextContents();
console.log('TAB ORDER:', JSON.stringify(tabTexts));

await page.click('[data-tab="liveview"]');
await page.waitForTimeout(600);
await page.screenshot({ path: '/tmp/shots/liveview-desktop.png' });

const bodyText = await page.locator('#page-liveview').innerText();
console.log('LIVEVIEW PAGE TEXT:', JSON.stringify(bodyText));

await page.setViewportSize({ width: 390, height: 844 });
await page.waitForTimeout(300);
await page.screenshot({ path: '/tmp/shots/liveview-mobile.png' });

const overflow = await page.evaluate(() => {
  const el = document.querySelector('#page-liveview');
  return el ? el.scrollWidth - el.clientWidth : null;
});
console.log('MOBILE HORIZONTAL OVERFLOW (px, should be 0):', overflow);

const overflowY = await page.evaluate(() => {
  const el = document.querySelector('#page-liveview');
  if (!el) return null;
  return getComputedStyle(el).overflowY;
});
console.log('COMPUTED overflow-y (should be auto):', overflowY);

console.log('ERRORS:', JSON.stringify(errors, null, 2));
await browser.close();
