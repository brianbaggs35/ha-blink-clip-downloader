import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

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

// https://vite.dev/config/
export default defineConfig({
  base: BASE,
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: OUT_DIR,
    emptyOutDir: true,
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
