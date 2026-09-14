import js from "@eslint/js";
import globals from "globals";

// These scripts gate two CI jobs (smoke-test and ha-integration) and cannot
// be run locally — ha_integration_smoke.mjs needs a whole Home Assistant
// Supervisor in front of it — so a mistake here is only ever discovered by
// a ~20-minute CI run. That happened: a `const` declared below the
// top-level `try` that a hoisted function inside it read, which is a
// temporal-dead-zone error at runtime and invisible to `node --check`.
// `no-use-before-define` with `variables: true` is the rule that catches it.
export default [
  js.configs.recommended,
  {
    files: ["**/*.mjs"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: { ...globals.node, ...globals.browser },
    },
    rules: {
      // functions: false — hoisted function declarations genuinely may be
      // called before their definition, and these files are written that
      // way throughout (helpers live below the top-level flow that uses
      // them). Variables and classes are the half that actually breaks.
      "no-use-before-define": [
        "error",
        { functions: false, variables: true, classes: true },
      ],
      "no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  },
  { ignores: ["node_modules/**"] },
];
