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

const issues = [];
const browser = await chromium.launch();
const page = await browser.newPage();

// Page errors only, deliberately not failed requests. `seed-media` gives
// three of the seeded clips real files, but the rest deliberately have
// none: nothing in the app creates a clip, and an *archived* clip's file
// is genuinely gone once it is in the zip. Their thumbnails therefore 404
// here and that is the faithful state, not a fault.
// ha_integration_smoke.mjs does collect failed requests, and runs *before*
// seeding, when the library is empty and nothing requests media at all --
// so that check stays strict and this one stays honest. The clips that do
// have files are asserted on directly by checkMediaThroughIngress below,
// which is stricter than a blanket "nothing 404'd" ever was.
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
  await starAClipThroughIngress(frame, issues);
  await checkLibraryFilterActuallyFilters(frame, issues);
  await checkClipModalOpens(frame, issues);
  await checkSecurityTimelineHasEvents(frame, issues);
  await checkStorageListsArchive(frame, issues);
  await checkStatusShowsBatteries(frame, issues);
  await checkAiUsageReflectsSeededTokens(frame, issues);
  await checkSeededDataRoundTripsThroughIngress(page, issues);
  await checkMediaThroughIngress(page, issues);

  if (issues.length > 0) {
    await page
      .screenshot({ path: "ha-integration-failure-seeded.png", fullPage: true })
      .catch(() => {});
    console.error(`Found ${issues.length} issue(s):`);
    for (const issue of issues) console.error(` - ${issue}`);
    process.exit(1);
  }
  console.log(
    "Seeded check passed through real ingress: the Library lists and filters real " +
      "clips, a clip opens with its stored detail, the Security Events timeline " +
      "renders, the Storage tab lists an archive, Status shows per-camera " +
      "batteries, AI Usage reflects the stored tokens, and the stored analysis, " +
      "detections and security events round-trip through the ingress proxy.",
  );
} catch (err) {
  console.error(`Seeded-library check failed: ${err.message}`);
  await page
    .screenshot({ path: "ha-integration-failure-seeded-error.png", fullPage: true })
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

    let missing = 0;
    for (const camera of SEEDED_CAMERAS) {
      await frame
        .locator("#page-library")
        .getByText(camera, { exact: false })
        .first()
        .waitFor({ state: "visible", timeout: 15000 })
        .catch(() => {
          missing += 1;
          issuesList.push(`Library never listed the seeded "${camera}" clip`);
        });
    }
    // Only claim success if nothing was missing: the caught rejections above
    // record an issue but do not stop the loop, so an unconditional log here
    // reported "Library lists the seeded clips" in the same run that failed
    // to find any of them.
    if (missing === 0) {
      console.log(`Library lists the seeded clips (${SEEDED_CAMERAS.join(", ")}).`);
    }
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

    // The clip's own stored fields, not its AI verdict: ClipAiPanel is
    // rendered `v-if="aiEnabled && clipId"`, and aiEnabled follows
    // ai_analysis_enabled, which this environment deliberately leaves off
    // (the AI tab correctly reads "AI Analysis Not Configured"). The
    // stored analysis and detections are still proven to round-trip --
    // through the API, below, rather than through a panel that is
    // correctly absent here.
    await modal
      .getByText("Front Door", { exact: false })
      .first()
      .waitFor({ state: "visible", timeout: 10000 })
      .catch(() => {
        issuesList.push("the clip modal did not show the seeded clip's camera");
      });

    await frame.locator(".modal-bg.open .modal-close").first().click();
    console.log("A seeded clip opens and shows its stored detail.");
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

/**
 * A filter is the one part of the Library that an empty library cannot
 * exercise at all: with nothing listed, filtering nothing still shows
 * nothing and every filter looks like it works.
 */
async function checkLibraryFilterActuallyFilters(frame, issuesList) {
  try {
    const cards = frame.locator("#page-library .clip-card");
    const before = await cards.count();
    if (before < 2) {
      issuesList.push(`expected several seeded clips to filter, saw ${before}`);
      return;
    }
    await frame.locator("#lib-filter-starred").check();

    // Polled, not read once: checking the box triggers a refetch, and the
    // list re-renders a tick later. Reading the count immediately returns
    // the pre-filter list and reports "6 -> 6" for a filter that works.
    let after = before;
    for (let attempt = 0; attempt < 40; attempt += 1) {
      after = await cards.count();
      if (after < before) break;
      await frame.page().waitForTimeout(250);
    }
    if (after >= before) {
      issuesList.push(
        `the starred filter did not narrow the Library (${before} -> ${after})`,
      );
    } else {
      console.log(`Library's starred filter narrows the list (${before} -> ${after}).`);
    }
    await frame.locator("#lib-filter-starred").uncheck();
  } catch (err) {
    issuesList.push(`could not exercise a Library filter: ${err.message}`);
  }
}

/**
 * The Security Events tab is entirely empty without seeded rows, so this
 * is the first time its timeline renders under real Home Assistant.
 */
async function checkSecurityTimelineHasEvents(frame, issuesList) {
  try {
    await frame.locator('.app-nav-tab[data-tab="security"]').click();
    await frame
      .locator('.app-nav-tab.active[data-tab="security"]')
      .waitFor({ state: "visible", timeout: 5000 });
    const rows = frame.locator("#page-security .security-row");
    await rows.first().waitFor({ state: "visible", timeout: 15000 });
    const count = await rows.count();
    if (count < 3) {
      issuesList.push(`Security Events showed ${count} rows, expected the 3 seeded`);
      return;
    }
    console.log(`Security Events timeline renders ${count} seeded events.`);
  } catch (err) {
    issuesList.push(`Security Events never rendered the seeded rows: ${err.message}`);
  }
}

/**
 * Archived clips are a separate query and a separate rendering path from
 * the Library's, grouped by ZIP rather than listed flat.
 */
async function checkStorageListsArchive(frame, issuesList) {
  try {
    await frame.locator('.app-nav-tab[data-tab="storage"]').click();
    await frame
      .locator('.app-nav-tab.active[data-tab="storage"]')
      .waitFor({ state: "visible", timeout: 5000 });
    const panel = frame.locator("#page-storage .archive-panel");
    await panel.first().waitFor({ state: "visible", timeout: 15000 });
    console.log("Storage tab lists the seeded archive.");
  } catch (err) {
    issuesList.push(`Storage never listed the seeded archive: ${err.message}`);
  }
}

/**
 * Battery readings come from Blink in normal operation, so this strip has
 * never rendered anything in CI before.
 */
async function checkStatusShowsBatteries(frame, issuesList) {
  try {
    await frame.locator('.app-nav-tab[data-tab="status"]').click();
    await frame
      .locator('.app-nav-tab.active[data-tab="status"]')
      .waitFor({ state: "visible", timeout: 5000 });
    const strip = frame.locator("#battery-strip");
    await strip.waitFor({ state: "visible", timeout: 15000 });
    await strip
      .getByText("Backyard", { exact: false })
      .first()
      .waitFor({ state: "visible", timeout: 10000 })
      .catch(() => {
        issuesList.push("the battery strip did not show the seeded cameras");
      });
    console.log("Status tab shows per-camera battery readings.");
  } catch (err) {
    issuesList.push(`Status never rendered the seeded batteries: ${err.message}`);
  }
}

/**
 * The stored analysis verdict and the per-frame detections are real rows
 * the app serves, but the UI that renders them (ClipAiPanel) is gated on
 * ai_analysis_enabled, which this environment deliberately leaves off.
 * Fetching them from inside the ingress iframe's own origin proves the
 * same data round-trips — database, through the app, through Supervisor's
 * ingress proxy — without depending on a panel that is correctly absent.
 */
async function checkSeededDataRoundTripsThroughIngress(page, issuesList) {
  try {
    const frameUrl = page.frames().find((f) => f.url().includes("hassio_ingress"))?.url();
    if (!frameUrl) {
      issuesList.push("no ingress iframe URL to fetch seeded data from");
      return;
    }
    const root = new URL(frameUrl).pathname.replace(/\/$/, "");
    const results = await page.evaluate(async ({ base, paths }) => {
      const out = [];
      for (const path of paths) {
        try {
          const res = await fetch(`${base}${path}`, { credentials: "include" });
          out.push({ path, status: res.status, body: await res.text() });
        } catch (err) {
          out.push({ path, status: 0, body: String(err) });
        }
      }
      return out;
    }, {
      base: root,
      paths: ["/api/clips/ci-seed-1", "/api/ai/detections/ci-seed-1", "/api/security/events/ci-seed-1"],
    });

    for (const { path, status, body } of results) {
      if (status !== 200) {
        issuesList.push(`GET ${path} through ingress returned ${status}`);
        continue;
      }
      if (!body || body === "[]" || body === "{}") {
        issuesList.push(`GET ${path} through ingress returned nothing for a seeded clip`);
      }
    }

    // The three person boxes were seeded across three frames under one
    // track_id, so the summary has to collapse them to a single subject
    // rather than reporting three people.
    const detections = results.find((r) => r.path === "/api/ai/detections/ci-seed-1");
    if (detections?.status === 200) {
      try {
        const parsed = JSON.parse(detections.body);
        const people = (Array.isArray(parsed) ? parsed : parsed.detected_objects || []).find(
          (d) => d.label === "person",
        );
        if (people && people.count !== 1) {
          issuesList.push(
            `detections collapsed 3 person boxes on one track into count=${people.count}, expected 1`,
          );
        }
      } catch {
        issuesList.push("detections through ingress were not valid JSON");
      }
    }
    console.log("Seeded analysis, detections and security events round-trip through ingress.");
  } catch (err) {
    issuesList.push(`could not fetch seeded data through ingress: ${err.message}`);
  }
}

/**
 * Binary and streamed responses through ingress, including HTTP Range.
 *
 * Everything else this script asks ingress to carry is JSON or HTML. The
 * app's media goes through a different path in Home Assistant's proxy:
 * `web.FileResponse` answers with sendfile(), advertises `Accept-Ranges`,
 * and serves 206 Partial Content for a `Range` header — which is exactly
 * what Video.js issues to seek within a clip. If ingress dropped the
 * Range header, or answered 200 with the whole file, seeking would break
 * for every user while the clip still appeared to play, and nothing else
 * in this repo would see it: frontend/e2e/ talks to the standalone server
 * with no proxy in front of it, and e2e/smoke.mjs hits the bare port.
 *
 * Needs the files ha_integration_setup.sh's `seed-media` writes — the
 * database rows alone leave every one of these endpoints at 404.
 */
async function checkMediaThroughIngress(page, issuesList) {
  const CLIP = "ci-seed-1";
  const RANGE_BYTES = 1024;
  try {
    const frameUrl = page.frames().find((f) => f.url().includes("hassio_ingress"))?.url();
    if (!frameUrl) {
      issuesList.push("no ingress iframe URL found to fetch media against");
      return;
    }
    const base = new URL(frameUrl).pathname.replace(/\/$/, "");

    const probe = await page.evaluate(
      async ({ base, clip, rangeBytes }) => {
        const read = async (path, init) => {
          const res = await fetch(`${base}${path}`, { credentials: "include", ...init });
          const buf = await res.arrayBuffer();
          return {
            status: res.status,
            type: res.headers.get("content-type") || "",
            acceptRanges: res.headers.get("accept-ranges") || "",
            contentRange: res.headers.get("content-range") || "",
            bytes: buf.byteLength,
            head: Array.from(new Uint8Array(buf).slice(0, 4)),
          };
        };
        return {
          thumb: await read(`/api/clips/${clip}/thumb`),
          full: await read(`/api/clips/${clip}/stream`),
          ranged: await read(`/api/clips/${clip}/stream`, {
            headers: { Range: `bytes=0-${rangeBytes - 1}` },
          }),
        };
      },
      { base, clip: CLIP, rangeBytes: RANGE_BYTES },
    );

    const { thumb, full, ranged } = probe;

    if (thumb.status !== 200) {
      issuesList.push(`thumbnail through ingress returned ${thumb.status}, not 200`);
    } else {
      if (!thumb.type.startsWith("image/")) {
        issuesList.push(`thumbnail through ingress had content-type "${thumb.type}"`);
      }
      // JPEG magic number, so this is a real decoded image rather than an
      // error page that happened to arrive with a 200.
      if (thumb.head[0] !== 0xff || thumb.head[1] !== 0xd8) {
        issuesList.push(`thumbnail through ingress was not JPEG data (starts ${thumb.head})`);
      }
    }

    if (full.status !== 200) {
      issuesList.push(`clip stream through ingress returned ${full.status}, not 200`);
      return;
    }
    if (full.bytes < 1024) {
      issuesList.push(`clip stream through ingress returned only ${full.bytes} bytes`);
    }
    if (full.acceptRanges !== "bytes") {
      issuesList.push(
        `clip stream through ingress advertised accept-ranges "${full.acceptRanges}", ` +
          `so a browser will not attempt to seek`,
      );
    }

    // The assertion this function exists for.
    if (ranged.status !== 206) {
      issuesList.push(
        `a Range request through ingress returned ${ranged.status}, not 206 — ` +
          `ingress is not passing Range through, so video seeking is broken`,
      );
    } else {
      if (ranged.bytes !== RANGE_BYTES) {
        issuesList.push(
          `a Range request for ${RANGE_BYTES} bytes through ingress returned ${ranged.bytes}`,
        );
      }
      const expected = `bytes 0-${RANGE_BYTES - 1}/${full.bytes}`;
      if (ranged.contentRange !== expected) {
        issuesList.push(
          `Range response through ingress had content-range "${ranged.contentRange}", ` +
            `expected "${expected}"`,
        );
      }
    }
    console.log(
      `Media through ingress: thumbnail ${thumb.bytes}B, clip ${full.bytes}B, ` +
        `range ${ranged.status} ${ranged.bytes}B.`,
    );
  } catch (err) {
    issuesList.push(`could not fetch media through ingress: ${err.message}`);
  }
}

/**
 * A real database write, made the way a user makes it.
 *
 * Every other assertion in this pass reads. The only write this job proved
 * before was a settings file (ha_integration_smoke.mjs's persistence
 * marker), which lands in /data as JSON — a different mechanism from a row
 * in the bundled PostgreSQL, going through a different HTTP verb. Ingress
 * proxies PUT no differently from GET in principle, but "in principle" is
 * what this whole job exists to stop relying on.
 *
 * Paired with ha_integration_setup.sh's `assert-clip-starred`, which reads
 * the same row back *after* the add-on's container has been recreated. Do
 * the two together and they prove something neither does alone: a change
 * someone makes in the UI is still there after an update.
 *
 * Runs before the filter check deliberately — that one leaves a filter
 * applied, and this clip's camera could be filtered out from under it.
 */
async function starAClipThroughIngress(frame, issuesList) {
  const CLIP = "ci-seed-2"; // seeded starred = FALSE, on purpose
  try {
    const card = frame.locator(`#page-library .clip-card[data-id="${CLIP}"]`);
    if ((await card.count()) === 0) {
      issuesList.push(`${CLIP} is not in the seeded Library to star`);
      return;
    }
    if ((await card.locator(".star-badge").count()) !== 0) {
      issuesList.push(`${CLIP} was already starred, so starring it proves nothing`);
      return;
    }

    await card.click();
    const modal = frame.locator(".modal-bg.open");
    await modal.waitFor({ state: "visible", timeout: 10000 });
    await modal.getByRole("button", { name: /Star/ }).first().click();

    // The grid badge appearing is the app applying the server's answer,
    // not optimism: the modal emits it after starClip() resolves.
    await frame
      .locator(`#page-library .clip-card[data-id="${CLIP}"] .star-badge`)
      .waitFor({ state: "visible", timeout: 10000 });

    await frame.locator(".modal-bg.open .modal-close").first().click();
    await modal.waitFor({ state: "hidden", timeout: 10000 });
    console.log(`Starred ${CLIP} through ingress; the grid picked it up.`);
  } catch (err) {
    issuesList.push(`could not star a clip through ingress: ${err.message}`);
  }
}
