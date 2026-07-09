import { definePreset } from '@primeuix/themes'
import Aura from '@primeuix/themes/aura'

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
})
