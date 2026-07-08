import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config'

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'jsdom',
      globals: true,
      coverage: {
        provider: 'v8',
        reporter: ['text', 'text-summary'],
        include: ['src/**/*.{ts,vue}'],
        exclude: ['src/main.ts', 'src/vite-env.d.ts', 'src/**/*.spec.ts'],
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
