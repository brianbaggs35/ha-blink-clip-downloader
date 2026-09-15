// Write a .gz alongside every compressible built asset.
//
// media_server.py serves blink_downloader/static/assets through aiohttp's
// add_static, which checks for a precompressed "<file>.gz" sibling and
// serves it with Content-Encoding: gzip when the browser accepts it,
// falling back to the plain file otherwise. So this needs no server-side
// change and degrades to exactly today's behaviour if it never runs.
//
// It is worth doing because the bundle is not small: Video.js and PrimeVue
// are compiled into it (see CLAUDE.md's "Build & serving"), and the whole
// payload compresses by about three quarters. Over the LAN that is barely
// noticeable; over Nabu Casa, a VPN, or mobile data — how plenty of people
// reach Home Assistant — it is the difference between a snappy first load
// and a slow one.
//
// Deliberately only text-shaped assets: .woff/.woff2 are already
// compressed, and gzipping them again wastes build time to produce a
// larger file. index.html is served by _handle_index (which rewrites the
// ingress path at request time), not by add_static, so a .gz of it would
// never be looked at.

import { gzipSync } from 'node:zlib'
import { readFileSync, writeFileSync, readdirSync, statSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ASSETS = resolve(HERE, '..', '..', 'blink_downloader', 'static', 'assets')

const COMPRESSIBLE = /\.(js|css|svg|json|map)$/

// Below this, the gzip header costs more than the compression saves.
const MIN_BYTES = 1024

function main() {
  let files
  try {
    files = readdirSync(ASSETS)
  } catch {
    // No build output to compress — `npm run build` failing is its own
    // error, and this should not invent a second one.
    console.log('gzip-static-assets: no assets directory, nothing to do')
    return
  }

  let done = 0
  let before = 0
  let after = 0
  for (const name of files) {
    if (!COMPRESSIBLE.test(name) || name.endsWith('.gz')) continue
    const path = join(ASSETS, name)
    if (!statSync(path).isFile()) continue
    const raw = readFileSync(path)
    if (raw.length < MIN_BYTES) continue
    const gz = gzipSync(raw, { level: 9 })
    // Only keep it if it actually helps; aiohttp would otherwise serve a
    // bigger file than the original.
    if (gz.length >= raw.length) continue
    writeFileSync(`${path}.gz`, gz)
    done += 1
    before += raw.length
    after += gz.length
  }

  const pct = before ? Math.round((1 - after / before) * 100) : 0
  console.log(
    `gzip-static-assets: ${done} file(s), ${(before / 1024).toFixed(0)} KB -> ` +
      `${(after / 1024).toFixed(0)} KB on the wire (${pct}% smaller)`,
  )
}

main()
