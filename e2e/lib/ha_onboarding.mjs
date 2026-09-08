// Completes Home Assistant Core's first-run onboarding via its own
// `onboarding` integration's REST endpoints (homeassistant/components/
// onboarding/views.py in home-assistant/core) rather than driving the
// multi-screen wizard through the UI.
//
// This is deliberately isolated in its own module: there is no more
// "officially supported for automation" way to do this (researched before
// writing this file -- the one "skip onboarding via env vars" mechanism
// that turned up belongs to a different, unofficial, non-Supervisor
// devcontainer project, not applicable to a real Supervisor install). The
// `/api/onboarding/*` endpoints are first-party (shipped with Core, the
// same mechanism its own frontend uses) but are not part of the stable,
// documented-for-third-parties REST API surface, so they can shift
// between Core versions without notice -- if that ever happens, this is
// the one file that needs to change, not the integration test itself.
//
// Exact request/response shapes below were confirmed empirically against a
// real running instance, not guessed from documentation.

/**
 * @param {string} baseUrl e.g. "http://127.0.0.1:8123" -- must be the
 *   Supervisor-managed entry point (hassio_observer), not Core's own raw
 *   port; see ha_integration_setup.sh's cmd_wait_core for why.
 * @param {{name: string, username: string, password: string}} owner
 * @returns {Promise<void>} resolves once all four onboarding steps
 *   (user, core_config, analytics, integration) report done=true.
 */
export async function completeOnboarding(baseUrl, owner) {
  const clientId = `${baseUrl}/`;

  const status = await getJson(`${baseUrl}/api/onboarding`);
  if (status.every((step) => step.done)) {
    return; // Already onboarded (e.g. a re-run against a live instance).
  }

  const { auth_code: userAuthCode } = await postJson(
    `${baseUrl}/api/onboarding/users`,
    {
      name: owner.name,
      username: owner.username,
      password: owner.password,
      client_id: clientId,
      language: "en",
    },
  );

  const token = await exchangeAuthCode(baseUrl, userAuthCode, clientId);
  const authHeaders = { Authorization: `Bearer ${token.access_token}` };

  await postJson(`${baseUrl}/api/onboarding/core_config`, {}, authHeaders);
  await postJson(`${baseUrl}/api/onboarding/analytics`, {}, authHeaders);
  await postJson(
    `${baseUrl}/api/onboarding/integration`,
    { client_id: clientId, redirect_uri: `${baseUrl}/?auth_callback=1` },
    authHeaders,
  );

  const finalStatus = await getJson(`${baseUrl}/api/onboarding`);
  const unfinished = finalStatus.filter((step) => !step.done);
  if (unfinished.length > 0) {
    throw new Error(
      `Onboarding did not complete: still pending ${unfinished.map((s) => s.step).join(", ")}`,
    );
  }
}

async function exchangeAuthCode(baseUrl, code, clientId) {
  const res = await fetch(`${baseUrl}/auth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code,
      client_id: clientId,
    }),
  });
  if (!res.ok) {
    throw new Error(
      `Token exchange failed: ${res.status} ${await res.text()}`,
    );
  }
  return res.json();
}

async function getJson(url) {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`GET ${url} failed: ${res.status} ${await res.text()}`);
  }
  return res.json();
}

async function postJson(url, body, headers = {}) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`POST ${url} failed: ${res.status} ${await res.text()}`);
  }
  return res.json();
}
