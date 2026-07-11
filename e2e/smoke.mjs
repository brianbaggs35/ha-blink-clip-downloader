#!/usr/bin/env node
// Playwright smoke check for the Blink Clip Downloader web UI.
//
// Loads the media server directly on its bare port (the same context
// scripts/smoke-test.sh boots it in) - there is no HA ingress/auth in front
// of it here, so no login flow to fake. Clicks through every nav tab and
// fails if the SPA throws a console/page error or a network request fails
// unexpectedly, catching JS-level breakage that curl/jq checks can't see.
//
// Usage: node smoke.mjs <base-url>

import { chromium } from "playwright";

const baseUrl = process.argv[2];
if (!baseUrl) {
  console.error("Usage: node smoke.mjs <base-url>");
  process.exit(1);
}

// Order matches the TABS array in frontend/src/components/layout/AppSidebar.vue.
const TABS = ["library", "automations", "status", "ai", "usage", "models"];

// Playwright's own machinery (and page.goto's navigation) can trigger a
// benign ERR_ABORTED when a request is superseded; only report failures
// that indicate an actual broken fetch in the app.
const IGNORED_FAILURE_TEXT = new Set(["net::ERR_ABORTED"]);

const issues = [];

const browser = await chromium.launch();
const page = await browser.newPage();

page.on("console", (msg) => {
  if (msg.type() === "error") {
    issues.push(`console error: ${msg.text()}`);
  }
});
page.on("pageerror", (err) => {
  issues.push(`page error: ${err.message}`);
});
page.on("requestfailed", (req) => {
  const failure = req.failure();
  if (failure && !IGNORED_FAILURE_TEXT.has(failure.errorText)) {
    issues.push(`request failed: ${req.url()} (${failure.errorText})`);
  }
});

try {
  console.log(`Loading ${baseUrl} ...`);
  await page.goto(baseUrl, { waitUntil: "load", timeout: 15000 });

  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]', {
    timeout: 10000,
  });
  console.log("SPA shell loaded, library tab active by default");

  // The smoke-test container boots with placeholder Blink credentials, so
  // real Blink auth always fails and the app shows its 2FA/error modal
  // (the auth store's polling in stores/auth.ts) - which intercepts clicks
  // on the rest of the page. That modal is real, correct behavior, not a
  // bug; a CSS override (rather than a one-off class removal) keeps it
  // hidden even as the poll keeps re-adding the "open" class, so the
  // nav-tab click loop below can still exercise the SPA. TwoFAOverlay,
  // HelpOverlay, PromptOverlay, and ConfirmDialog all share the same
  // ".modal-bg" root class with no per-component id, but only the
  // auth-triggered 2FA modal can be open unprompted here, so hiding all of
  // them is equivalent and avoids depending on an id that doesn't exist.
  await page.addStyleTag({ content: ".modal-bg{display:none!important}" });

  for (const tab of TABS) {
    console.log(`Clicking tab: ${tab}`);
    await page.click(`.app-nav-tab[data-tab="${tab}"]`);
    await page.waitForSelector(`.app-nav-tab.active[data-tab="${tab}"]`, {
      timeout: 5000,
    });
    // Let the tab's own data fetch (loadStatus/loadAIStatus/etc.) settle so
    // any error it throws shows up in the console/pageerror listeners above.
    await page.waitForTimeout(700);
  }

  if (issues.length > 0) {
    console.error(`Found ${issues.length} issue(s) during e2e smoke check:`);
    for (const issue of issues) console.error(` - ${issue}`);
    process.exit(1);
  }

  console.log(
    "Playwright e2e smoke check passed: all tabs loaded with no console/page errors.",
  );
} finally {
  await browser.close();
}
