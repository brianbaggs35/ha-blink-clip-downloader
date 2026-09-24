// What each kind of markable asset is called, shown as, and watched for.
// The wording of `watchedFor` mirrors the rules the analysis prompt sends
// for that type (prompt_segments.py's _ASSET_RULES) and how the security
// layer treats it (security/assets.py's handled_routinely), so what the tab
// promises is what analysis does.

import type { AssetType } from '../../api/types'

export interface AssetTypeInfo {
  value: AssetType
  label: string
  /** PrimeIcons class. */
  icon: string
  /** Offered as the name when the type is picked and no name is typed yet. */
  defaultName: string
  watchedFor: string
}

/** "Something else": the catch-all, and what an unknown type renders as. */
const OTHER: AssetTypeInfo = {
  value: 'other',
  label: 'Something else',
  icon: 'pi pi-tag',
  defaultName: '',
  watchedFor: 'Anything else worth watching. Anyone handling or moving it is flagged.',
}

export const ASSET_TYPES: AssetTypeInfo[] = [
  {
    value: 'door',
    label: 'Door',
    icon: 'pi pi-sign-in',
    defaultName: 'Front door',
    watchedFor:
      "Knocking, ringing and deliveries are routine. Trying the handle, forcing it, peering in or hanging around isn't.",
  },
  {
    value: 'window',
    label: 'Window',
    icon: 'pi pi-window-maximize',
    defaultName: 'Window',
    watchedFor: 'Anyone touching it, prying at it or peering in closely is flagged.',
  },
  {
    value: 'garage',
    label: 'Garage door',
    icon: 'pi pi-warehouse',
    defaultName: 'Garage door',
    watchedFor: "Opening and closing it is routine. Forcing it, or someone lingering at it, isn't.",
  },
  {
    value: 'gate',
    label: 'Gate',
    icon: 'pi pi-objects-column',
    defaultName: 'Gate',
    watchedFor: "Walking through is routine. Forcing or climbing it, or lingering at it, isn't.",
  },
  {
    value: 'package_area',
    label: 'Package spot',
    icon: 'pi pi-box',
    defaultName: 'Package spot',
    watchedFor:
      "Deliveries, and collecting your own, are routine. Someone carrying off a parcel they didn't bring isn't — and the spot is checked for looking different afterwards.",
  },
  {
    value: 'mailbox',
    label: 'Mailbox',
    icon: 'pi pi-envelope',
    defaultName: 'Mailbox',
    watchedFor: "Mail being delivered is routine. Someone else taking things out, or damaging it, isn't.",
  },
  {
    value: 'bicycle',
    label: 'Bike or scooter',
    icon: 'pi pi-compass',
    defaultName: 'Bike',
    watchedFor: "Anyone handling, unlocking or moving it is flagged, and it's checked for being gone afterwards.",
  },
  {
    value: 'equipment',
    label: 'Equipment',
    icon: 'pi pi-wrench',
    defaultName: 'Equipment',
    watchedFor: 'Grills, generators, AC units, tools: anyone handling or moving one is flagged.',
  },
  OTHER,
]

const BY_VALUE = new Map(ASSET_TYPES.map((info) => [info.value, info]))

/** The catalogue entry for *type*, or "Something else" for one this build
 * doesn't know — a newer backend's type must still render. */
export function assetTypeInfo(type: string): AssetTypeInfo {
  return BY_VALUE.get(type as AssetType) ?? OTHER
}

/** Zone colours, chosen to stay distinct from each other and visible on
 * both day and infrared footage. The photo underneath is the same in either
 * theme, so these don't change with it. */
export const ZONE_COLORS = [
  '#22d3ee',
  '#f59e0b',
  '#a78bfa',
  '#34d399',
  '#f472b6',
  '#60a5fa',
  '#fb7185',
  '#facc15',
  '#4ade80',
  '#c084fc',
  '#2dd4bf',
  '#fb923c',
]

export function zoneColor(index: number): string {
  return ZONE_COLORS[((index % ZONE_COLORS.length) + ZONE_COLORS.length) % ZONE_COLORS.length]
}
