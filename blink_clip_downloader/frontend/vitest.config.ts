import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config.ts'

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: ['./src/test-setup.ts'],
      // Reuse one VM-backed jsdom environment per worker while preserving
      // isolation between test files. This avoids creating 72 jsdom
      // environments during CI coverage runs.
      pool: 'vmThreads',
      // e2e/ holds @playwright/test specs (frontend/playwright.config.ts),
      // a different test runner entirely — Vitest's default file glob
      // would otherwise pick them up and try (and fail) to run them too.
      exclude: ['**/node_modules/**', '**/dist/**', 'e2e/**'],
      coverage: {
        provider: 'v8',
        // lcov is for SonarCloud's JS/TS analyzer (sonar.javascript.lcov.reportPaths
        // in sonar-project.properties) - cobertura is what Codecov consumes.
        reporter: ['text', 'text-summary', 'cobertura', 'lcov', 'html'],
        include: ['src/**/*.{ts,vue}'],
        // TypeScript-only declarations are erased before Vitest runs, so
        // api/types.ts has no executable statements that a test can cover.
        exclude: ['src/main.ts', 'src/test-setup.ts', 'src/vite-env.d.ts', 'src/api/types.ts', 'src/**/*.spec.ts'],
        // Mirrors the backend's pyproject.toml coverage gate (fail_under = 80)
        // so the frontend is held to the same bar as the Python package.
        thresholds: {
          lines: 80,
          statements: 80,
          functions: 80,
          branches: 80,
        },
      },
    },
  }),
)
