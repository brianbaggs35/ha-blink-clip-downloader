#!/usr/bin/env node
// Playwright smoke check for the Blink Clip Downloader web UI.
//
// Loads the media server directly on its bare port (the same context
// scripts/smoke-test.sh boots it in) - there is no HA ingress/auth in front
// of it here, so no login flow to fake. Clicks through every nav tab and:
//   1. Confirms each tab's own Vue component actually mounted and rendered
//      real content (not just that the nav highlight moved) by waiting for
//      a tab-specific piece of text that only appears once that page's own
//      data fetch has resolved and its template branched into its actual
//      (here: no-data/not-connected, since this container boots with
//      placeholder Blink credentials) state.
//   2. Confirms the tab's loading spinner is gone once that content has
//      appeared, catching a stuck/partial render.
//   3. Fails (with a timeout, not a silent pass) if a tab never settles.
// Also fails if the SPA throws a console/page error or a network request
// fails unexpectedly, catching JS-level breakage curl/jq checks can't see.
//
// Usage: node smoke.mjs <base-url>

import { chromium } from "playwright";

const baseUrl = process.argv[2];
if (!baseUrl) {
  console.error("Usage: node smoke.mjs <base-url>");
  process.exit(1);
}

// Order matches the TABS array in frontend/src/components/layout/AppSidebar.vue.
// For each tab: `loadedText` is text that only appears once that page's own
// component has mounted and its data fetch (if any) has resolved into real
// content - not just boilerplate present before the fetch settles. This
// container has fake Blink credentials and no AI provider configured, so
// every data-driven tab is expected to land on its "no data yet" state, not
// populated content - that's still a real, meaningful render to prove.
// `loadingSelector`, when set, is asserted to be gone once `loadedText`
// appears (scoped to that tab, since some tabs also nest independently
// loading child cards outside their own loading branch).
const TAB_CHECKS = {
  library: {
    // Always mounted (not v-if-gated) - its own fetch starts at app boot,
    // so by the time this tab is checked its content has very likely
    // already settled, but the wait below still holds it to the same bar.
    loadedText: "No clips found",
    loadingSelector: null,
  },
  automations: {
    // Purely static reference content - no fetch, nothing to wait on, but
    // still confirms the component actually rendered its real template
    // rather than nothing at all.
    loadedText: "HA Automation Examples",
    loadingSelector: null,
  },
  status: {
    loadedText: "Blink Connection",
    loadingSelector: ".loading-indicator",
  },
  ai: {
    loadedText: "AI Analysis Not Configured",
    loadingSelector: ".loading-indicator",
  },
  usage: {
    loadedText: "No AI Usage Data",
    loadingSelector: ".loading-indicator",
  },
  models: {
    loadedText: "AI Providers & Models",
    loadingSelector: null,
  },
  vehicles: {
    loadedText: "No cameras found",
    loadingSelector: ".loading-indicator",
  },
  biometrics: {
    loadedText: "No one enrolled yet",
    loadingSelector: ".loading-indicator",
    // Hidden from the nav entirely on hosts where face recognition can't
    // run (see vision.py's torch_cpu_compatible()) - absence is correct
    // behavior there, not a failure, so this tab is allowed to not exist.
    optional: true,
  },
  storage: {
    loadedText: "No archived clips yet",
    loadingSelector: ".loading-indicator",
  },
};

// Order matches the TABS array in frontend/src/components/layout/AppSidebar.vue.
const TABS = Object.keys(TAB_CHECKS);

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

// Waits for `loadedText` to actually render inside tab `name`'s own page
// container (not merely present anywhere in the DOM - every page, active
// or not, stays mounted-but-hidden via CSS, so an unscoped text search
// could pass against a *different*, inactive tab's stale content). A
// timeout here is a real failure, not a race to paper over: it means the
// tab's data fetch never resolved into its expected UI within a generous
// window, exactly the "still spinning" class of bug a fixed sleep would
// silently let through.
async function waitForTabContent(name, { loadedText, loadingSelector }) {
  const container = page.locator(`#page-${name}`);
  try {
    await container
      .getByText(loadedText, { exact: false })
      .first()
      .waitFor({ state: "visible", timeout: 12000 });
  } catch {
    issues.push(
      `[${name}] expected to see "${loadedText}" render, but it never appeared within 12s ` +
        `(tab may be stuck loading, or broken)`,
    );
    return;
  }
  if (loadingSelector) {
    const stillLoading = await container
      .locator(loadingSelector)
      .first()
      .isVisible()
      .catch(() => false);
    if (stillLoading) {
      issues.push(
        `[${name}] loading indicator is still visible even after its content rendered ` +
          `- possible partial/inconsistent render`,
      );
    }
  }
}

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
    const navBtn = page.locator(`.app-nav-tab[data-tab="${tab}"]`);
    if ((await navBtn.count()) === 0) {
      const check = TAB_CHECKS[tab];
      if (check.optional) {
        console.log(
          `Tab "${tab}" not present in nav (expected on hosts where its feature can't run) - skipping`,
        );
        continue;
      }
      issues.push(`[${tab}] nav tab is missing from the sidebar entirely`);
      continue;
    }

    console.log(`Clicking tab: ${tab}`);
    await navBtn.click();
    await page.waitForSelector(`.app-nav-tab.active[data-tab="${tab}"]`, {
      timeout: 5000,
    });

    await waitForTabContent(tab, TAB_CHECKS[tab]);
    console.log(`  content rendered: "${TAB_CHECKS[tab].loadedText}"`);
  }

  if (issues.length > 0) {
    console.error(`Found ${issues.length} issue(s) during e2e smoke check:`);
    for (const issue of issues) console.error(` - ${issue}`);
    process.exit(1);
  }

  console.log(
    "Playwright e2e smoke check passed: every tab rendered its real content with no console/page errors.",
  );
} finally {
  await browser.close();
}
