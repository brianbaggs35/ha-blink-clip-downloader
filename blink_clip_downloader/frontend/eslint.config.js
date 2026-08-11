import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import pluginVue from 'eslint-plugin-vue'
import { defineConfigWithVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'
import eslintConfigPrettier from 'eslint-config-prettier'
import globals from 'globals'

export default defineConfigWithVueTs(
  { ignores: ['dist/**', '../blink_downloader/static/**', 'coverage/**', 'coverage-e2e/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/recommended'],
  vueTsConfigs.recommended,
  // Disables stylistic rules that would otherwise fight Prettier (line
  // breaks, attribute-per-line, etc.) — Prettier owns formatting, ESLint
  // owns correctness.
  eslintConfigPrettier,
  {
    languageOptions: {
      globals: { ...globals.browser },
    },
    rules: {
      // Structural replacement for the backend XSS-escaping tests removed
      // during the Vue migration: if nothing ever uses v-html on
      // user-controlled data (camera name, tags, clip id, source), Vue's
      // default template-interpolation auto-escaping makes that bug class
      // impossible rather than something tests must keep re-verifying.
      'vue/no-v-html': 'error',
    },
  },
  {
    // Test files legitimately define small ad-hoc host components inline
    // (e.g. to exercise a composable) — one-component-per-file is a
    // production-code convention, not a correctness rule.
    files: ['**/*.spec.ts'],
    rules: {
      'vue/one-component-per-file': 'off',
    },
  },
  {
    // frontend/e2e/ runs under Node (Playwright's test runner, plus
    // generate-coverage-report.mjs), not a browser — unlike vite.config.ts/
    // vitest.config.ts, which @vue/eslint-config-typescript already
    // special-cases, plain files here need globals.node added explicitly
    // or `process`/`__dirname`-style references trip no-undef.
    files: ['e2e/**/*.{ts,mjs}'],
    languageOptions: {
      globals: { ...globals.node },
    },
  },
)
