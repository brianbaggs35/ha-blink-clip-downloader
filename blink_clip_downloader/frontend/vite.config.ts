import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import istanbul from 'vite-plugin-istanbul'

// The add-on's Home Assistant ingress mount point is a per-install, dynamic
// path prefix decided at request time by media_server.py (see the
// __HAROOT__ placeholder in index.html) — asset URLs must stay relative so
// the built app works under any ingress prefix without a rebuild.
const BASE = './'

// vite build writes straight into the sibling Python package's static
// directory: works identically for `npm run build` in a bare checkout and
// for the Docker frontend-builder stage (which only needs this directory
// path to be creatable, not for blink_downloader/ to already exist).
const OUT_DIR = fileURLToPath(new URL('../blink_downloader/static', import.meta.url))

// Only set by `npm run test:e2e:coverage` — a plain `npm run build` (what
// the Dockerfile's frontend-builder stage runs) must never carry Istanbul's
// coverage counters into the actual shipped add-on image.
const COVERAGE = process.env.VITE_COVERAGE === 'true'

// https://vite.dev/config/
export default defineConfig({
  base: BASE,
  plugins: [
    vue(),
    // Instruments source for Istanbul-style coverage collection, read back
    // out of the running page as `window.__coverage__` by
    // e2e/coverage-fixtures.ts — mirrors vitest.config.ts's own
    // include/exclude so the two coverage reports cover the same surface.
    // forceBuildInstrument is required because Playwright's e2e suite runs
    // against a real `vite build` output (via standalone_server.py), not
    // `vite dev` — the plugin only instruments dev-server output otherwise.
    COVERAGE &&
      istanbul({
        include: 'src/**/*',
        exclude: ['src/main.ts', 'src/test-setup.ts', 'src/vite-env.d.ts', '**/*.spec.ts', 'node_modules'],
        extension: ['.ts', '.vue'],
        forceBuildInstrument: true,
      }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: OUT_DIR,
    emptyOutDir: true,
    // Only the coverage build needs sourcemaps at all (istanbul reads them
    // to map instrumented output back to source); 'hidden' generates the
    // .map files without adding `//# sourceMappingURL=` comments to the
    // served JS, which also quiets vite-plugin-istanbul's own console
    // notice that it auto-enabled sourcemaps. The plain/shipped build
    // (COVERAGE false) is unaffected either way.
    sourcemap: COVERAGE ? 'hidden' : false,
  },
  server: {
    // Dev-mode only: proxy API calls to a real backend (e.g. `local-test/run.sh`,
    // which serves on 8099) so `npm run dev` gives HMR against live data
    // without going through media_server.py's per-request HTML rewriting.
    proxy: {
      '/api': 'http://localhost:8099',
      '/health': 'http://localhost:8099',
    },
  },
})
