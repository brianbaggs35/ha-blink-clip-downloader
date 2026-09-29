import os from 'node:os'
import path from 'node:path'

/** Where the signed-in browser state lives for a BLINK_E2E_SIGNIN=1 run:
 *  written by global-signin.ts, read back through playwright.config.ts's
 *  `use.storageState`. Outside the repo, so a run leaves nothing to ignore. */
export const SIGNED_IN_STATE = path.join(os.tmpdir(), 'blink-e2e-signed-in.json')

/** The one login scripts/standalone_server.py's stand-in for Supervisor's
 *  /auth accepts (its _E2E_USERNAME / _E2E_PASSWORD). */
export const E2E_LOGIN = { username: 'e2e-user', password: 'e2e-password' }
