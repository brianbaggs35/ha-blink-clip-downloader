// Inline SVG path/circle data for the app's hand-authored icon set, ported
// from the pre-Vue embedded HTML. Kept as a lookup table (rather than one
// .vue file per icon) since these are simple, static shape lists reused
// across many components — see AppIcon.vue.
export interface IconDef {
  filled?: boolean
  paths?: string[]
  circles?: { cx: number; cy: number; r: number }[]
  rects?: { x: number; y: number; width: number; height: number; rx?: number }[]
}

export const ICONS = {
  brand: {
    paths: ['M3 7a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z', 'M15 10.5 21 7v10l-6-3.5z'],
  },
  'tab-library': {
    paths: ['M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z'],
  },
  'tab-status': { paths: ['M3 12h4l2 7 4-14 2 7h6'] },
  'tab-usage': { paths: ['M5 20V11M12 20V4M19 20v-7'] },
  'tab-models': {
    paths: ['M12 2 2 7l10 5 10-5-10-5z', 'M2 12l10 5 10-5', 'M2 17l10 5 10-5'],
  },
  'tab-automations': { filled: true, paths: ['M13 2 4 14h6l-1 8 9-12h-6l1-8z'] },
  'tab-ai': {
    paths: ['M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3'],
    rects: [
      { x: 6, y: 6, width: 12, height: 12, rx: 2 },
      { x: 10, y: 10, width: 4, height: 4 },
    ],
  },
  'tab-vehicles': {
    paths: ['M3 16v-4l2.2-5.5A2 2 0 0 1 7.06 5h9.88a2 2 0 0 1 1.86 1.5L21 12v4', 'M3 16h18', 'M3 12h18'],
    rects: [
      { x: 2, y: 16, width: 4, height: 3, rx: 1 },
      { x: 18, y: 16, width: 4, height: 3, rx: 1 },
    ],
    circles: [
      { cx: 7, cy: 16, r: 1.6 },
      { cx: 17, cy: 16, r: 1.6 },
    ],
  },
  'tab-biometrics': {
    paths: [
      'M4 8V6a2 2 0 0 1 2-2h2',
      'M20 8V6a2 2 0 0 1-2-2h-2',
      'M4 16v2a2 2 0 0 0 2 2h2',
      'M20 16v2a2 2 0 0 1-2 2h-2',
      'M9 10.5c0-1.7 1.3-3 3-3s3 1.3 3 3v1.5c0 2-1.3 3.5-3 4.5',
      'M9 15c1-.7 1.5-1.7 1.5-3v-1.5',
    ],
  },
  'theme-light': { paths: ['M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z'] },
  'theme-dark': {
    paths: ['M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4'],
    circles: [{ cx: 12, cy: 12, r: 4 }],
  },
  help: {
    paths: ['M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2-3 4', 'M12 17.5v.1'],
    circles: [{ cx: 12, cy: 12, r: 9 }],
  },
  'notif-on': { paths: ['M6 8a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6', 'M10 20a2 2 0 0 0 4 0'] },
  'notif-off': {
    paths: [
      'M6.3 6.3C6.1 6.9 6 7.6 6 8c0 5-2 6-2 6h11',
      'M8.7 3A6 6 0 0 1 18 8c0 3.09.78 4.6 1.32 5.4',
      'M10 20a2 2 0 0 0 4 0',
      'M4 4l16 16',
    ],
  },
  refresh: { paths: ['M21 12a9 9 0 1 1-2.6-6.35', 'M21 3v6h-6'] },
  sync: { paths: ['M12 3v12', 'M7 10l5 5 5-5', 'M5 21h14'] },
  close: { paths: ['M18 6 6 18', 'M6 6l12 12'] },
  'error-circle': {
    paths: ['M12 8v5', 'M12 16v.1'],
    circles: [{ cx: 12, cy: 12, r: 9 }],
  },
  'no-thumb': {
    paths: ['M17 10l5-3v10l-5-3z'],
    rects: [{ x: 2, y: 5, width: 15, height: 14, rx: 2 }],
  },
  'tab-storage': {
    paths: ['M3 8h18', 'M10 12h4'],
    rects: [
      { x: 3, y: 4, width: 18, height: 4, rx: 1 },
      { x: 4, y: 8, width: 16, height: 12, rx: 1 },
    ],
  },
  'empty-box': {
    paths: [
      'M22 12h-6l-2 3h-4l-2-3H2',
      'M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z',
    ],
  },
} satisfies Record<string, IconDef>

// keyof typeof ICONS only yields the real literal union of icon names (not
// plain `string`) because ICONS above has no type annotation — `satisfies`
// validates each entry against IconDef without widening the object's own
// inferred type the way `: Record<string, IconDef>` would.
export type IconName = keyof typeof ICONS
