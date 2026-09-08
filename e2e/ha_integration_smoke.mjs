#!/usr/bin/env node
// Playwright verification for the real Home Assistant Supervisor
// integration test (.github/workflows/ha-integration.yaml). By the time
// this runs, scripts/ci/ha_integration_setup.sh has already made
// Supervisor discover, build, install, and start this add-on for real -
// this script's job is to prove a real user could actually reach and use
// it: log in to Home Assistant, open the add-on's sidebar panel, and
// confirm the resulting iframe is genuinely proxied through HA's ingress
// (not a same-origin coincidence) with real app content inside it.
//
// Unlike e2e/smoke.mjs (which loads the add-on directly on its bare port,
// with no HA in front of it at all), the ingress assertion here is the
// entire point - see assertRealIngress() below. Direct-port access is used
// only as an earlier, secondary readiness probe elsewhere in this job
// (ha_integration_setup.sh's cmd_start), never here as proof ingress
// itself works.
//
// Two further checks exist specifically because nothing else in this
// repo's CI can exercise them - neither e2e/smoke.mjs (bare docker run,
// no Supervisor at all) nor frontend/e2e/'s standalone-server suite (no
// real HA Core to talk to) can reach either code path:
//   1. checkHaNotification() - clicks the Automations tab's real "Send
//      test HA notification" button and confirms Home Assistant's own
//      API actually accepted it. This is the add-on's homeassistant_api
//      integration (config.yaml's `homeassistant_api: true`, backed by a
//      real Supervisor-issued token) genuinely round-tripping through
//      Supervisor to Core, not a mock.
//   2. checkAddonLogTab() - navigates to Home Assistant's OWN Settings >
//      Apps > <this add-on> > Log page (not the app's ingress UI at all)
//      and confirms real container log output renders there. Proves
//      Supervisor is actually capturing this add-on's logs and Core's
//      frontend can fetch/display them - the same surface a real user
//      would check first when troubleshooting a broken install.
// Both selectors/URLs below were confirmed by hand against a real running
// instance, not guessed from documentation - see CLAUDE.md's note on this
// workflow for why that matters here specifically (HA's frontend is
// heavily shadow-DOM based, and has silently renamed UI sections between
// versions - "Add-ons" is labelled "Apps" as of the version this job
// currently pins).
//
// Usage: node ha_integration_smoke.mjs <ha-base-url> <addon-slug> <panel-title> <addon-name>
//   e.g. node ha_integration_smoke.mjs http://127.0.0.1:8123 \
//          local_blink_clip_downloader "Blink Clips" "Blink Clip Downloader"

import { chromium } from "playwright";
import { completeOnboarding } from "./lib/ha_onboarding.mjs";
import { TAB_CHECKS } from "./smoke.mjs";

const [baseUrl, addonSlug, panelTitle, addonName] = process.argv.slice(2);
if (!baseUrl || !addonSlug || !panelTitle || !addonName) {
  console.error(
    'Usage: node ha_integration_smoke.mjs <ha-base-url> <addon-slug> "<panel-title>" "<addon-name>"',
  );
  process.exit(1);
}

// A representative subset of TAB_CHECKS, not the full set smoke.mjs
// covers - this job exists to prove ingress + the app work, not to
// re-run the whole per-tab data-rendering audit a second time. Picked to
// span the shapes TAB_CHECKS distinguishes: always-mounted/no fetch
// (library), a data-driven tab with its own loading state (status), one
// of this add-on's Supervisor/Blink-API-touching tabs (syncmodule), and
// automations (static content, but also where checkHaNotification()
// below needs to be - listed last so that check can assume it's already
// the active tab).
const TABS_TO_VERIFY = ["library", "status", "syncmodule", "automations"];

const OWNER = {
  name: "CI Integration Test",
  username: "ci-integration-test",
  password: "ci-integration-test-password-1",
};

const issues = [];

console.log(`Completing onboarding at ${baseUrl} ...`);
await completeOnboarding(baseUrl, OWNER);
console.log("Onboarding complete.");

const browser = await chromium.launch();
const page = await browser.newPage();

page.on("console", (msg) => {
  if (msg.type() === "error") issues.push(`console error: ${msg.text()}`);
});
page.on("pageerror", (err) => issues.push(`page error: ${err.message}`));

async function saveFailureArtifacts(label) {
  try {
    await page.screenshot({
      path: `ha-integration-failure-${label}.png`,
      fullPage: true,
    });
    console.error(`Saved failure screenshot: ha-integration-failure-${label}.png`);
  } catch (err) {
    console.error(`Could not capture failure screenshot: ${err.message}`);
  }
}

try {
  console.log(`Logging in at ${baseUrl} ...`);
  await page.goto(baseUrl, { waitUntil: "load", timeout: 20000 });
  await page.locator('input[name="username"]').fill(OWNER.username);
  await page.locator('input[name="password"]').fill(OWNER.password);
  await page.getByRole("button", { name: /log in/i }).click();
  await page.waitForURL(/\/home\//, { timeout: 20000 });
  console.log("Logged in, landed on the real HA dashboard.");

  // Real Material-web list-item markup nests a <span slot="headline"> that
  // intercepts pointer events over the clickable <a> itself, and that span
  // is not a plain descendant .getByText() can chain off of reliably
  // (confirmed empirically: a scoped `panelLink.getByText(...)` times out
  // even though the same text is found instantly page-wide - some of this
  // markup crosses shadow-DOM boundaries in a way that breaks the scoped
  // chain but not an unscoped page-level search). Two other approaches
  // confirmed NOT to work against a real running instance: a plain
  // .click() on the <a> times out waiting for the intercepting span to
  // get out of the way (it never does), and {force: true} on the <a> to
  // skip that check does not reliably trigger the frontend's real
  // navigation handler either (silently no-ops on a freshly onboarded
  // user's very first click). Clicking the visible label text itself,
  // found page-wide, works correctly every time - it's a normal,
  // non-forced click on the exact element a real user would click, first
  // verifying via the href that a real sidebar link exists at all (for a
  // clear failure message if the ingress panel was never added).
  const panelLinkExists = await page
    .locator(`a[href="/${addonSlug}"]`)
    .count();
  if (panelLinkExists === 0) {
    throw new Error(
      `No sidebar link to /${addonSlug} found - the add-on's ingress panel isn't in the HA sidebar ` +
        `(check that ha_integration_setup.sh's enable-ingress-panel step actually ran and succeeded)`,
    );
  }
  console.log(`Clicking the "${panelTitle}" sidebar panel...`);
  await page.getByText(panelTitle, { exact: true }).click();
  await page.waitForURL(new RegExp(addonSlug), { timeout: 15000 });

  await assertRealIngress(page, issues);

  const frame = page.frameLocator("iframe").first();
  await frame
    .locator('.app-nav-tab[data-tab="library"]')
    .waitFor({ state: "visible", timeout: 20000 });
  console.log("Real app content rendered inside the ingress iframe.");

  for (const tab of TABS_TO_VERIFY) {
    const check = TAB_CHECKS[tab];
    const navBtn = frame.locator(`.app-nav-tab[data-tab="${tab}"]`);
    if ((await navBtn.count()) === 0) {
      if (check.optional) {
        console.log(`Tab "${tab}" not present (optional) - skipping`);
        continue;
      }
      issues.push(`[${tab}] nav tab missing from the sidebar inside ingress`);
      continue;
    }
    console.log(`Clicking tab through ingress: ${tab}`);
    await navBtn.click();
    await frame
      .locator(`.app-nav-tab.active[data-tab="${tab}"]`)
      .waitFor({ state: "visible", timeout: 5000 });
    try {
      await frame
        .locator(`#page-${tab}`)
        .getByText(check.loadedText, { exact: false })
        .first()
        .waitFor({ state: "visible", timeout: 12000 });
      console.log(`  content rendered: "${check.loadedText}"`);
    } catch {
      issues.push(
        `[${tab}] expected to see "${check.loadedText}" render through ingress, but it never appeared`,
      );
    }
  }

  // TABS_TO_VERIFY ends with "automations", so that tab is already active.
  await checkHaNotification(frame, issues);

  // Escapes the ingress iframe entirely - everything from here on is
  // Home Assistant's own top-level UI, not the app's.
  await checkAddonLogTab(page, baseUrl, addonSlug, addonName, issues);

  if (issues.length > 0) {
    await saveFailureArtifacts("issues");
    console.error(`Found ${issues.length} issue(s):`);
    for (const issue of issues) console.error(` - ${issue}`);
    process.exit(1);
  }

  console.log(
    "Home Assistant integration check passed: onboarded, logged in, opened the real ingress panel, " +
      "confirmed real app content through it, verified a real round trip through Home Assistant's " +
      "own API, and confirmed Supervisor-captured logs render in HA's own Settings UI.",
  );
} catch (err) {
  console.error(`Integration check failed: ${err.message}`);
  await saveFailureArtifacts("error");
  process.exitCode = 1;
} finally {
  await browser.close();
}

/**
 * The whole point of this script over e2e/smoke.mjs: prove the app is
 * reached through HA's real ingress proxy, not merely that *some* iframe
 * happens to show up. Home Assistant's ingress URLs are always
 * /api/hassio_ingress/<token>/ (confirmed empirically against a real
 * Supervisor instance) - anything else means the sidebar panel silently
 * fell back to something other than a genuine ingress-proxied route, which
 * would be a real bug worth failing loudly on rather than treating as
 * close enough.
 */
async function assertRealIngress(page, issuesList) {
  const iframe = page.locator("iframe").first();
  const src = await iframe.getAttribute("src").catch(() => null);
  if (!src || !/^\/api\/hassio_ingress\//.test(src)) {
    issuesList.push(
      `Panel iframe src was "${src}" - expected a real /api/hassio_ingress/<token>/ ingress URL. ` +
        `This must never be the add-on's direct port/origin.`,
    );
  } else {
    console.log(`Confirmed genuine ingress iframe: ${src}`);
  }
}

/**
 * Clicks the Automations tab's real "Send test HA notification" button
 * (NotificationChannelsCard.vue) and confirms the exact success message
 * the backend returns on a genuine 2xx from Home Assistant's own API
 * (notification_channels.py's send_test_ha_notification, called through
 * media_server.py's POST /api/notifications/test-ha). This can only
 * succeed with a real Supervisor-issued SUPERVISOR_TOKEN and a real Core
 * instance behind it - there is nothing to mock here, which is exactly
 * why it's worth asserting on in this job specifically.
 */
async function checkHaNotification(frame, issuesList) {
  console.log(
    'Sending a real test notification through Home Assistant\'s own API ("Send test HA notification")...',
  );
  try {
    await frame
      .getByRole("button", { name: "Send test HA notification" })
      .click();
    await frame
      .getByText("Test notification sent to Home Assistant.", {
        exact: false,
      })
      .waitFor({ state: "visible", timeout: 10000 });
    console.log(
      "  confirmed: the add-on's homeassistant_api call reached real Home Assistant Core.",
    );
  } catch (err) {
    issuesList.push(
      `Clicking "Send test HA notification" never showed the expected success message ` +
        `(${err.message}) - the add-on's homeassistant_api integration (its Supervisor-token-backed ` +
        `call into Home Assistant Core) may be broken.`,
    );
  }
}

/**
 * Home Assistant's OWN Settings > Apps > <add-on> > Log page - not the
 * app's ingress UI at all. Proves Supervisor is genuinely capturing this
 * add-on's container output and that Core's frontend can fetch and
 * render it, the same place a real user facing a broken install would
 * look first. Navigation confirmed by hand against a real running
 * instance (see this file's header comment) - notably, this HA frontend
 * version labels the add-ons section "Apps", not "Add-ons".
 */
async function checkAddonLogTab(page, baseUrl, addonSlug, addonName, issuesList) {
  console.log(`Checking Home Assistant's own Settings > Apps > "${addonName}" > Log page...`);
  try {
    await page.goto(baseUrl, { waitUntil: "load", timeout: 15000 });
    await page.waitForURL(/\/home\//, { timeout: 15000 });
    await page.getByText("Settings", { exact: true }).click();
    await page.waitForURL(/\/config\/dashboard/, { timeout: 10000 });
    await page.getByText("Apps", { exact: true }).click();
    await page.waitForURL(/\/config\/apps/, { timeout: 10000 });
    await page.getByText(addonName, { exact: false }).first().click();
    await page.waitForURL(new RegExp(`/config/app/${addonSlug}/info`), {
      timeout: 10000,
    });
    await page.getByRole("link", { name: "Log" }).click();
    await page.waitForURL(new RegExp(`/config/app/${addonSlug}/logs`), {
      timeout: 10000,
    });
    // The log viewer takes a moment to stream content in after the tab
    // mounts (confirmed empirically - an immediate check reliably finds
    // nothing, while this same text reliably appears within a few
    // seconds), so this must poll rather than check once.
    await page
      .getByText("blink_downloader", { exact: false })
      .first()
      .waitFor({ state: "visible", timeout: 15000 });
    console.log(
      "  confirmed: real container log output rendered in Home Assistant's own Log tab.",
    );
  } catch (err) {
    issuesList.push(
      `Home Assistant's Settings > Apps > "${addonName}" > Log page never showed real log content ` +
        `(${err.message}) - Supervisor's log capture for this add-on, or Core's own log viewer, may ` +
        `be broken.`,
    );
  }
}
