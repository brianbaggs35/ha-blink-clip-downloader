import js from "@eslint/js";
import globals from "globals";

// These scripts gate two CI jobs (smoke-test and ha-integration) and cannot
// be run locally — ha_integration_smoke.mjs needs a whole Home Assistant
// Supervisor in front of it — so a mistake here is only ever discovered by
// a ~20-minute CI run. That has happened twice: a `const` declared below
// the top-level `try` that a hoisted function inside it read, which is a
// temporal-dead-zone error at runtime and invisible to `node --check`.
// `no-use-before-define` with `variables: true` catches it only when the
// function reading the constant is written *above* the declaration; the
// second time the function sat below it, so the rule passed while the call
// from the `try` still ran first. The `no-restricted-syntax` selector below
// covers both orders by refusing any top-level declaration after the
// top-level `try` at all -- nothing declared there is ever needed later,
// and anything the `try` reaches is not yet initialized when it runs.
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
      "no-restricted-syntax": [
        "error",
        {
          selector: "Program > TryStatement ~ VariableDeclaration",
          message:
            "Declare module-level constants above the top-level try: the functions it " +
            "calls run before this line is reached, so reading this throws.",
        },
      ],
    },
  },
  { ignores: ["node_modules/**"] },
];
