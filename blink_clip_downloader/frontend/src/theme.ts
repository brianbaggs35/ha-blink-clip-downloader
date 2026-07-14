import { definePreset } from '@primeuix/themes'
import Aura from '@primeuix/themes/aura'

// Aura's own Card/InputText/Select/Textarea/FileUpload/Checkbox/Dialog
// presets reference semantic tokens (content.*, form.field.*,
// overlay.modal.*) directly on a single flat `root` (or `icon`) object,
// with no colorScheme.light/colorScheme.dark split of their own — unlike
// Button/Tag/Message/ToggleSwitch/Toast, which do define one. That flat
// object only ever gets generated once, under `:root, :host`, so its
// computed value is whatever e.g. content.background resolves to there
// (the light scheme, since darkModeSelector('.dark') is never applied to
// <html> — only <body>, see App.vue) — and because that computed value is
// then just *inherited* by descendants rather than re-evaluated against
// each element's own (correctly dark) semantic tokens, every instance of
// these seven components stayed permanently light-themed regardless of the
// active theme. Confirmed by hand, for each affected semantic category:
// --p-content-background/--p-form-field-background/--p-overlay-modal-
// background themselves correctly change under `.dark` at any element, but
// --p-card-background/--p-inputtext-background/--p-checkbox-background/
// --p-dialog-background etc. do not, and only ever have the one (light)
// declaration in the generated stylesheet. Re-declaring the exact same
// token references under an explicit colorScheme.light/dark here forces
// PrimeVue to emit a second, `.dark`-scoped declaration too, which is what
// actually makes the derived value re-evaluate correctly.
const contentColorScheme = {
  background: '{content.background}',
  color: '{content.color}',
}
const formFieldColorScheme = {
  background: '{form.field.background}',
  disabledBackground: '{form.field.disabled.background}',
  color: '{form.field.color}',
  disabledColor: '{form.field.disabled.color}',
}
// Same bug, same fix, for Checkbox (form.field.*) and Dialog (overlay.modal.*).
const checkboxIconColorScheme = {
  color: '{form.field.color}',
  disabledColor: '{form.field.disabled.color}',
}
const overlayModalColorScheme = {
  background: '{overlay.modal.background}',
  color: '{overlay.modal.color}',
}

// Custom PrimeVue theme for the redesigned UI: a vivid violet accent on a
// refined near-black (dark) / soft neutral (light) surface scale, replacing
// Aura's default blue/slate palette. Kept in its own module (rather than
// inline in main.ts) so it can grow independently as more of the app adopts
// PrimeVue components.
export const AppTheme = definePreset(Aura, {
  semantic: {
    primary: {
      50: '#f4f2ff',
      100: '#e9e5ff',
      200: '#d3caff',
      300: '#b3a0ff',
      400: '#9575ff',
      500: '#7c5cff',
      600: '#6d47f5',
      700: '#5b39d1',
      800: '#4a2fa8',
      900: '#3c2985',
      950: '#241659',
    },
    colorScheme: {
      light: {
        surface: {
          0: '#ffffff',
          50: '#f7f7fb',
          100: '#eeeef5',
          200: '#e2e2ec',
          300: '#cbcbdb',
          400: '#a3a3ba',
          500: '#75758f',
          600: '#54546b',
          700: '#3d3d52',
          800: '#26263a',
          900: '#17172a',
          950: '#0d0d1a',
        },
      },
      dark: {
        surface: {
          0: '#ffffff',
          50: '#eceef5',
          100: '#c8cadb',
          200: '#9a9db8',
          300: '#6b6f8f',
          400: '#454870',
          500: '#2b2d4d',
          600: '#1e2039',
          700: '#171829',
          800: '#121320',
          900: '#0c0d16',
          950: '#08090f',
        },
      },
    },
  },
  components: {
    card: {
      colorScheme: {
        light: { root: contentColorScheme },
        dark: { root: contentColorScheme },
      },
    },
    checkbox: {
      colorScheme: {
        light: { root: formFieldColorScheme, icon: checkboxIconColorScheme },
        dark: { root: formFieldColorScheme, icon: checkboxIconColorScheme },
      },
    },
    dialog: {
      colorScheme: {
        light: { root: overlayModalColorScheme },
        dark: { root: overlayModalColorScheme },
      },
    },
    fileupload: {
      colorScheme: {
        light: { root: contentColorScheme },
        dark: { root: contentColorScheme },
      },
    },
    inputtext: {
      colorScheme: {
        light: { root: formFieldColorScheme },
        dark: { root: formFieldColorScheme },
      },
    },
    select: {
      colorScheme: {
        light: { root: formFieldColorScheme },
        dark: { root: formFieldColorScheme },
      },
    },
    textarea: {
      colorScheme: {
        light: { root: formFieldColorScheme },
        dark: { root: formFieldColorScheme },
      },
    },
  },
})
