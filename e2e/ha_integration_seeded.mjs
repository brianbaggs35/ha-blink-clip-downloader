#!/usr/bin/env node
// Second browser pass over the real Home Assistant ingress panel, run
// after ha_integration_setup.sh's `seed-data` has put rows in the add-on's
// own PostgreSQL.
//
// Why this exists as a separate pass rather than more assertions inside
// ha_integration_smoke.mjs: that script verifies every tab in its *empty*
// state, which is a real and worthwhile condition — it is exactly what a
// fresh install looks like, and it is the only state this job could reach
// before, since no Blink account means no clips. Seeding first would have
// invalidated those assertions; seeding after keeps both. Between them the
// job now covers the two states a user is ever actually in.
//
// The rows are inserted straight into PostgreSQL because nothing in the
// app creates a clip — clips only ever arrive from the Blink API, which
// this environment deliberately cannot reach. The same reasoning (and the
// same trick) is already used by frontend/e2e/ for security_events.
//
// Usage: node ha_integration_seeded.mjs <ha-base-url> <addon-slug> <panel-title>

import { chromium } from "playwright";

const [baseUrl, addonSlug, panelTitle] = process.argv.slice(2);
if (!baseUrl || !addonSlug || !panelTitle) {
  console.error(
    'Usage: node ha_integration_seeded.mjs <ha-base-url> <addon-slug> "<panel-title>"',
  );
  process.exit(1);
}

// Must match ha_integration_setup.sh's cmd_seed_data. If the two drift,
// these checks fail loudly rather than passing on absent data.
const OWNER = {
  username: "ci-integration-test",
  password: "ci-integration-test-password-1",
};
const SEEDED_CAMERAS = ["Front Door", "Driveway", "Backyard"];
const SEEDED_SUSPICIOUS_SUMMARY = "A person is standing at the front door.";

const issues = [];
const browser = await chromium.launch();
const page = await browser.newPage();

// Page errors only, deliberately not failed requests: the seeded rows
// describe clips whose files do not exist on disk (they never came from
// Blink), so their thumbnail and stream requests legitimately 404 here.
// ha_integration_smoke.mjs does collect failed requests, and runs *before*
// seeding, when the library is empty and nothing requests media at all --
// so that check stays strict and this one stays honest.
page.on("pageerror", (err) => issues.push(`page error: ${err.message}`));

try {
  await page.goto(baseUrl, { waitUntil: "load", timeout: 20000 });
  await page.locator('input[name="username"]').fill(OWNER.username);
  await page.locator('input[name="password"]').fill(OWNER.password);
  await page.getByRole("button", { name: /log in/i }).click();
  await page.waitForURL(/\/home\//, { timeout: 20000 });

  await page.getByText(panelTitle, { exact: true }).click();
  await page.waitForURL(new RegExp(addonSlug), { timeout: 15000 });

  const frame = await waitForIngressFrame(page);
  if (!frame) throw new Error("no ingress iframe appeared");

  await checkLibraryListsSeededClips(frame, issues);
  await checkClipModalOpens(frame, issues);
  await checkAiUsageReflectsSeededTokens(frame, issues);

  if (issues.length > 0) {
    await page
      .screenshot({ path: "ha-integration-seeded-failure.png", fullPage: true })
      .catch(() => {});
    console.error(`Found ${issues.length} issue(s):`);
    for (const issue of issues) console.error(` - ${issue}`);
    process.exit(1);
  }
  console.log(
    "Seeded-library check passed: the Library lists real clips through ingress, " +
      "a clip opens with its stored AI verdict, and AI Usage reflects the stored tokens.",
  );
} catch (err) {
  console.error(`Seeded-library check failed: ${err.message}`);
  await page
    .screenshot({ path: "ha-integration-seeded-error.png", fullPage: true })
    .catch(() => {});
  process.exitCode = 1;
} finally {
  await browser.close();
}

async function waitForIngressFrame(page) {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const frame = page.frames().find((f) => f.url().includes("hassio_ingress"));
    if (frame) return frame;
    await page.waitForTimeout(500);
  }
  return null;
}

/**
 * The Library rendering a real list is the single biggest thing this job
 * could not see before. An empty Library exercises almost none of the
 * page: no cards, no per-clip thumbnails or metadata, no filter actually
 * filtering anything.
 */
async function checkLibraryListsSeededClips(frame, issuesList) {
  try {
    await frame.locator('.app-nav-tab[data-tab="library"]').click();
    await frame
      .locator('.app-nav-tab.active[data-tab="library"]')
      .waitFor({ state: "visible", timeout: 5000 });

    for (const camera of SEEDED_CAMERAS) {
      await frame
        .locator("#page-library")
        .getByText(camera, { exact: false })
        .first()
        .waitFor({ state: "visible", timeout: 15000 })
        .catch(() => {
          issuesList.push(`Library never listed the seeded "${camera}" clip`);
        });
    }
    console.log(`Library lists the seeded clips (${SEEDED_CAMERAS.join(", ")}).`);
  } catch (err) {
    issuesList.push(`could not verify the seeded Library: ${err.message}`);
  }
}

/**
 * Opening a clip is the one path that reads a *single* clip's full record
 * back out of PostgreSQL and renders its stored AI verdict — a round trip
 * an empty library can never reach.
 */
async function checkClipModalOpens(frame, issuesList) {
  try {
    const card = frame.locator("#page-library .clip-card").first();
    if ((await card.count()) === 0) {
      issuesList.push("no clip card to open in the seeded Library");
      return;
    }
    await card.click();
    const modal = frame.locator(".modal-bg.open");
    await modal.waitFor({ state: "visible", timeout: 10000 });
    await modal
      .getByText(SEEDED_SUSPICIOUS_SUMMARY, { exact: false })
      .first()
      .waitFor({ state: "visible", timeout: 10000 })
      .catch(() => {
        issuesList.push(
          "the clip modal did not show the seeded AI summary stored for that clip",
        );
      });
    await frame.locator(".modal-bg.open .modal-close").first().click();
    console.log("A seeded clip opens and shows its stored AI verdict.");
  } catch (err) {
    issuesList.push(`could not open a seeded clip: ${err.message}`);
  }
}

/**
 * The seeded analysis rows carry real token counts, so AI Usage has to
 * stop showing its "no data" state and start aggregating. That aggregation
 * is pure server-side SQL this job otherwise never runs with any rows in
 * it.
 */
async function checkAiUsageReflectsSeededTokens(frame, issuesList) {
  try {
    // The tab's id is "usage", not "aiusage" — the sidebar label and the
    // element id differ, and App.vue's #page-usage is the authority.
    await frame.locator('.app-nav-tab[data-tab="usage"]').click();
    await frame
      .locator('.app-nav-tab.active[data-tab="usage"]')
      .waitFor({ state: "visible", timeout: 5000 });
    const usagePage = frame.locator("#page-usage");
    await usagePage.waitFor({ state: "visible", timeout: 10000 });

    const emptyState = usagePage.getByText("No AI Usage Data", { exact: false });
    if (await emptyState.isVisible().catch(() => false)) {
      issuesList.push(
        "AI Usage still shows its empty state even though analysis rows were seeded",
      );
      return;
    }
    console.log("AI Usage reflects the seeded analysis rows.");
  } catch (err) {
    issuesList.push(`could not verify AI Usage against seeded rows: ${err.message}`);
  }
}
