#!/usr/bin/env node
// Merges the per-test coverage JSON files collected by coverage-fixtures.ts
// (one per Playwright test, raw window.__coverage__ dumps) and renders the
// same report formats vitest.config.ts's own coverage does, so the two are
// directly comparable.
//
// Deliberately not `nyc report`: nyc's CLI silently dropped all but ~6 of
// the 70 real source files when pointed at externally-collected coverage
// like this (confirmed by merging the exact same files with
// istanbul-lib-coverage directly, by hand, and getting the correct/full
// 70 — the bug is specific to nyc's own wrapper, not the underlying
// libraries or the data). This script calls those same underlying
// libraries (istanbul-lib-coverage/-report, istanbul-reports) directly,
// which nyc itself is built on top of, and are correct.
//
// Usage: node generate-coverage-report.mjs

import libCoverage from 'istanbul-lib-coverage'
import libReport from 'istanbul-lib-report'
import reports from 'istanbul-reports'
import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'

const RAW_DIR = '.nyc_output'
const REPORT_DIR = 'coverage-e2e'

let rawFiles
try {
  rawFiles = readdirSync(RAW_DIR).filter((f) => f.endsWith('.json'))
} catch {
  rawFiles = []
}
if (rawFiles.length === 0) {
  console.error(
    `No coverage files found in ${RAW_DIR}/ — expected one per Playwright test. ` +
      "Did the build run with VITE_COVERAGE=true (see package.json's test:e2e:coverage script)?",
  )
  process.exit(1)
}

const map = libCoverage.createCoverageMap({})
for (const file of rawFiles) {
  map.merge(JSON.parse(readFileSync(path.join(RAW_DIR, file), 'utf8')))
}

const context = libReport.createContext({ dir: REPORT_DIR, coverageMap: map })
for (const reporter of ['html', 'text-summary', 'lcov', 'cobertura']) {
  reports.create(reporter).execute(context)
}

console.log(`Coverage report for ${map.files().length} files (from ${rawFiles.length} tests) written to ${REPORT_DIR}/`)
