# Vue 3 + TypeScript + Vite

## PrimeVue license

PrimeVue 5 reads the client-side license from `VITE_PRIMEVUE_LICENSE_KEY` at
build time. No `.env` file is required; export the variable before starting
Vite:

```bash
export VITE_PRIMEVUE_LICENSE_KEY='your-license-key'
npm run dev
```

Alternatively, put the same variable in an ignored `.env.local` file and
restart Vite after changing it. The key is intentionally included in the
browser bundle because PrimeVue verifies it in the client.

This template should help get you started developing with Vue 3 and TypeScript in Vite. The template uses Vue 3 `<script setup>` SFCs, check out the [script setup docs](https://v3.vuejs.org/api/sfc-script-setup.html#sfc-script-setup) to learn more.

Learn more about the recommended Project Setup and IDE Support in the [Vue Docs TypeScript Guide](https://vuejs.org/guide/typescript/overview.html#project-setup).
