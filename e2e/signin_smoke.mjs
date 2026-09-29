#!/usr/bin/env node
// Playwright check that the packaged add-on asks for a sign-in on its direct
// port, run against a container booted with its *shipped defaults*.
//
// scripts/smoke-test.sh and CI's smoke-test job turn Direct Access Sign-In
// off (they read the API with no session to sign in with), so nothing else
// ever boots the image the way a user's install starts: sign-in on. This is
// that check, and only that: the sign-in unit tests and frontend/e2e's
// direct-access-signin.spec.ts already cover the gate's rules against a
// bare MediaServer, so what is asserted here is what only the real image can
// show --
//   * the option's default is honoured by the real startup path (the
//     container's options.json below omits it);
//   * the gate holds over a real published port, where the peer is not
//     Supervisor's ingress proxy;
//   * the login page renders in a real browser under the served CSP and
//     security headers, and a failed sign-in grants nothing;
//   * the access token opens the few endpoints Home Assistant calls and
//     nothing else.
// What it cannot show is the sign-in *succeeding*: that goes through
// Supervisor's /auth, which does not exist outside Home Assistant (see
// ha-integration.yaml for the job that has a real Supervisor).
//
// Usage: node signin_smoke.mjs <base-url> <access-token>
//   <access-token> is the container's own (/data/web_access.json), which
//   the CI job reads out with `docker exec`.

import { chromium, request } from "playwright";

const [baseUrl, accessToken] = process.argv.slice(2);
if (!baseUrl || !accessToken) {
  console.error("Usage: node signin_smoke.mjs <base-url> <access-token>");
  process.exit(1);
}

// Accent colours of the login page's light and dark themes (access.py). The
// browser's default button is neither, so this fails if the page's inline
// <style> is blocked by the CSP and the form comes up unstyled.
const LOGIN_BUTTON_COLOURS = ["rgb(31, 111, 235)", "rgb(76, 141, 255)"];

const failures = [];
function expect(name, ok, detail = "") {
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${!ok && detail ? ` (${detail})` : ""}`);
  if (!ok) failures.push(detail ? `${name}: ${detail}` : name);
}

// redirects off: the 303 to /login *is* the behaviour under test.
const api = await request.newContext({ baseURL: baseUrl, maxRedirects: 0 });
const bearer = { Authorization: `Bearer ${accessToken}` };

async function jsonOf(res) {
  try {
    return await res.json();
  } catch {
    return null;
  }
}

async function checkOpenPaths() {
  const health = await api.get("/health");
  expect("/health is open", health.status() === 200, `HTTP ${health.status()}`);

  const login = await api.get("/login");
  const html = await login.text();
  expect(
    "/login serves the sign-in form",
    login.status() === 200 && html.includes('name="password"'),
    `HTTP ${login.status()}`,
  );
  const headers = login.headers();
  expect(
    "the login page carries the CSP and nosniff",
    (headers["content-security-policy"] ?? "").includes("default-src 'self'") &&
      headers["x-content-type-options"] === "nosniff",
    JSON.stringify(headers),
  );
  expect(
    "the login page refuses framing on its own",
    headers["x-frame-options"] === "SAMEORIGIN",
    headers["x-frame-options"] ?? "no X-Frame-Options",
  );

  // A dashboard card frames the kiosk page and, once a sign-in expires, the
  // login page it is redirected to; only a page that says kiosk=1 may be.
  const kioskLogin = await api.get("/login?next=%2F%3Fkiosk%3D1&kiosk=1");
  expect(
    "the kiosk login page can sit in a dashboard card",
    kioskLogin.status() === 200 && !("x-frame-options" in kioskLogin.headers()),
    `HTTP ${kioskLogin.status()}`,
  );

  const favicon = await api.get("/favicon.svg");
  expect(
    "the favicon is not behind the sign-in",
    ![303, 401, 403].includes(favicon.status()),
    `HTTP ${favicon.status()}`,
  );
}

async function checkGate() {
  const index = await api.get("/");
  expect(
    "an anonymous page load is sent to the login page",
    index.status() === 303 && (index.headers().location ?? "").startsWith("/login?next="),
    `HTTP ${index.status()} ${index.headers().location ?? ""}`,
  );

  const kiosk = await api.get("/?kiosk=1&tab=securityfeed");
  expect(
    "a dashboard card is sent to a login page it may be framed as",
    kiosk.status() === 303 && (kiosk.headers().location ?? "").includes("kiosk=1"),
    `HTTP ${kiosk.status()} ${kiosk.headers().location ?? ""}`,
  );

  for (const path of ["/api/clips", "/api/stats", "/api/access", "/api/ai/camera-configs"]) {
    const res = await api.get(path);
    const body = await jsonOf(res);
    expect(
      `GET ${path} is refused with a 401 the UI can act on`,
      res.status() === 401 && body?.login_required === true,
      `HTTP ${res.status()}`,
    );
  }

  for (const path of ["/api/download-now", "/api/access/token/regenerate"]) {
    const res = await api.post(path);
    expect(`POST ${path} is refused`, res.status() === 401, `HTTP ${res.status()}`);
  }
}

async function checkToken() {
  // The one thing the token is for: Home Assistant fetching a snapshot.
  // Whatever the route answers with no Blink connection, it is not the gate.
  const snapshot = await api.get("/api/security-feed/snapshot/Nowhere", { headers: bearer });
  expect(
    "the token gets a snapshot request past the gate",
    ![401, 403].includes(snapshot.status()),
    `HTTP ${snapshot.status()}`,
  );
  const viaQuery = await api.get(
    `/api/security-feed/snapshot/Nowhere?token=${encodeURIComponent(accessToken)}`,
  );
  expect(
    "so does ?token=, which a Generic Camera URL has to use",
    ![401, 403].includes(viaQuery.status()),
    `HTTP ${viaQuery.status()}`,
  );

  // ...and nothing else. A token copied out of someone's YAML must not read
  // the library, read itself back, or replace itself.
  for (const [method, path] of [
    ["GET", "/api/clips"],
    ["GET", "/api/access"],
    ["POST", "/api/access/token/regenerate"],
  ]) {
    const res = await api.fetch(path, { method, headers: bearer });
    expect(
      `the token does not open ${method} ${path}`,
      res.status() === 403,
      `HTTP ${res.status()}`,
    );
  }

  const wrong = await api.get("/api/security-feed/snapshot/Nowhere", {
    headers: { Authorization: "Bearer not-the-token" },
  });
  expect("a wrong token is just anonymous", wrong.status() === 401, `HTTP ${wrong.status()}`);
}

async function checkLoginInBrowser() {
  const browser = await chromium.launch();
  const issues = [];
  try {
    const context = await browser.newContext({ baseURL: baseUrl });
    const page = await context.newPage();
    // A CSP violation is a console error, so a login page whose inline
    // <style> the policy blocks fails here even though it still submits.
    // Chrome also logs every non-2xx response as "Failed to load resource",
    // including the 503 this test provokes on purpose; the statuses that
    // matter are asserted directly, so that noise is not an issue.
    page.on("console", (msg) => {
      if (msg.type() === "error" && !msg.text().startsWith("Failed to load resource")) {
        issues.push(`console error: ${msg.text()}`);
      }
    });
    page.on("pageerror", (err) => issues.push(`page error: ${err.message}`));

    await page.goto("/");
    expect("opening the app lands on the login page", page.url().includes("/login?next=%2F"), page.url());
    await page.getByRole("heading", { name: "Blink Clips" }).waitFor({ timeout: 10000 });

    const button = page.getByRole("button", { name: "Sign in" });
    const colour = await button.evaluate((el) => getComputedStyle(el).backgroundColor);
    expect("the login form is styled under the CSP", LOGIN_BUTTON_COLOURS.includes(colour), colour);

    await page.getByLabel("Username").fill("nobody");
    await page.getByLabel("Password").fill("not-a-real-password");
    const posted = page.waitForResponse(
      (res) => res.request().method() === "POST" && new URL(res.url()).pathname === "/login",
    );
    await button.click();
    const response = await posted;

    // No Supervisor in this container, so there is nobody to ask: the page
    // has to say so rather than pretend the password was wrong or fall over.
    expect("a sign-in with no Supervisor to ask is a 503", response.status() === 503, `HTTP ${response.status()}`);
    const alert = await page.getByRole("alert").innerText();
    expect("and it says why, in the page", alert.includes("Supervisor"), alert);
    expect(
      "the username is kept for the next try",
      (await page.getByLabel("Username").inputValue()) === "nobody",
    );
    const cookies = await context.cookies();
    expect(
      "a failed sign-in sets no session",
      !cookies.some((cookie) => cookie.name === "blink_session"),
      JSON.stringify(cookies.map((cookie) => cookie.name)),
    );
    const afterwards = await page.request.get("/api/clips", { maxRedirects: 0 });
    expect("and grants nothing", afterwards.status() === 401, `HTTP ${afterwards.status()}`);
  } finally {
    await browser.close();
  }
  expect("the login page raised no browser errors", issues.length === 0, issues.join("; "));
}

try {
  await checkOpenPaths();
  await checkGate();
  await checkToken();
  await checkLoginInBrowser();
} finally {
  await api.dispose();
}

if (failures.length > 0) {
  console.error(`\n${failures.length} check(s) failed:`);
  for (const failure of failures) console.error(`  - ${failure}`);
  process.exit(1);
}
console.log("\nSign-in smoke check passed.");
