import { createApp } from 'vue'
import { createPinia } from 'pinia'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { AppTheme } from './theme'
import 'primeicons/primeicons.css'
// Self-hosted (bundled by Vite, not a Google Fonts CDN request) so this
// doesn't need the CSP script/style/font-src loosening a <link> to Google
// Fonts would — see base.css's --font-sans stack and CLAUDE.md's note on
// why a CDN webfont was rejected for the pre-Vue UI.
import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/inter/600.css'
import '@fontsource/inter/700.css'
import '@fontsource/inter/800.css'
import './assets/styles/base.css'
import App from './App.vue'

createApp(App)
  .use(createPinia())
  .use(PrimeVue, {
    theme: {
      preset: AppTheme,
      // Reuses the existing theme store's body.dark/body.light toggle
      // (see stores/theme.ts) instead of PrimeVue's own default
      // `.p-dark`/media-query strategy, so there's a single source of
      // truth for dark/light across hand-rolled and PrimeVue components.
      options: { darkModeSelector: '.dark' },
    },
  })
  .use(ConfirmationService)
  .use(ToastService)
  .mount('#app')
