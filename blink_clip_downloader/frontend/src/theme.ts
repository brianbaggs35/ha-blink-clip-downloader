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
    // Button/InputText/Select/Textarea's size="small" variant all read
    // font size and padding from this one shared token (confirmed via
    // Aura's own preset: each component only sets a *root* font-size for
    // sm/lg, never the default size, which just inherits the ambient
    // 16px body font — so this is the one lever that actually shrinks
    // them). Default Aura sm (0.875rem/0.625rem/0.375rem) still reads
    // noticeably larger than this app's own pre-existing hand-rolled
    // small-button scale (see base.css's .btn.sm: 0.78rem font,
    // 0.68rem/0.32rem padding) — every PrimeVue form control added
    // going forward should pass size="small" and rely on this override
    // to match, rather than the unstyled default (~16px/0.75rem, closer
    // to 40px tall than the ~28-30px the rest of the app uses).
    formField: {
      sm: {
        fontSize: '0.78rem',
        paddingX: '0.68rem',
        paddingY: '0.32rem',
      },
    },
  },
  components: {
    card: {
      colorScheme: {
        light: { root: contentColorScheme },
        dark: { root: contentColorScheme },
      },
      // Aura's default Card #title (1.25rem/500) reads noticeably larger
      // than every hand-rolled heading elsewhere in the app (.auto-content
      // h3 is 0.9rem) — only affects cards that actually use the #title
      // slot (VehiclesPage's description-card/camera-card, Biometrics'
      // person-card); most existing cards use a plain <h3> in the default
      // slot instead and are unaffected either way.
      title: {
        fontSize: '1rem',
        fontWeight: '600',
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
      // Same reasoning as card.title above — Aura's default (1.25rem/600)
      // is the single biggest piece of text in e.g. the "Clear AI usage
      // stats?" confirm dialog, out of proportion with everything else in it.
      title: {
        fontSize: '1rem',
        fontWeight: '600',
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
    // Aura's default ProgressSpinner cycles its stroke through 4 unrelated
    // colors (red/blue/green/yellow) via a 6s keyframe animation — reads as
    // a generic Material spinner, clashing with this app's single-accent
    // palette. Pointing all four color slots at the same accent shade
    // leaves the spin/dash animation intact but keeps the color constant.
    progressspinner: {
      colorScheme: {
        light: {
          root: {
            colorOne: '{primary.500}',
            colorTwo: '{primary.500}',
            colorThree: '{primary.500}',
            colorFour: '{primary.500}',
          },
        },
        dark: {
          root: {
            colorOne: '{primary.500}',
            colorTwo: '{primary.500}',
            colorThree: '{primary.500}',
            colorFour: '{primary.500}',
          },
        },
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
    // Tag has no size prop/variant to opt into (unlike the form controls
    // above) — its root.fontSize (0.875rem) and padding are hardcoded in
    // Aura's own preset, so the only way to bring it in line with the
    // app's existing small-badge scale (base.css's .badge: 0.72rem,
    // 0.14rem/0.6rem padding) is a direct root override here.
    tag: {
      root: {
        fontSize: '0.72rem',
        padding: '0.16rem 0.55rem',
      },
    },
    // Aura hardcodes root.width to a flat 25rem regardless of message
    // length, so a short detail like "Camera configs saved" leaves a wide
    // band of empty space between the text and the close button — reads as
    // misaligned/off-center rather than as a toast sized to its content.
    // fit-content with the old hand-rolled .toast class's own cap
    // (min(320px, 100vw - 3rem), see base.css) keeps short toasts tight
    // and long ones wrapping sanely instead of overflowing on mobile.
    toast: {
      root: {
        width: 'fit-content',
      },
    },
  },
})
