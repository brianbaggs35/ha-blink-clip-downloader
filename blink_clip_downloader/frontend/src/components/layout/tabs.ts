// The navigation tabs. A plain module rather than the sidebar's own
// `<script>` block: App.vue reads these as runtime values to validate a
// `?tab=` parameter, and a named export from a `.vue` file is invisible to
// static analysis (CodeQL read `TAB_NAMES` as always undefined).
import type { IconName } from '../icons/paths'

export type TabName =
  | 'library'
  | 'liveview'
  | 'securityfeed'
  | 'automations'
  | 'syncmodule'
  | 'status'
  | 'ai'
  | 'usage'
  | 'models'
  | 'security'
  | 'assistant'
  | 'vehicles'
  | 'assets'
  | 'biometrics'
  | 'storage'
  | 'backup'

export const TABS: { name: TabName; label: string; icon: IconName }[] = [
  { name: 'library', label: 'Library', icon: 'tab-library' },
  { name: 'liveview', label: 'Live View', icon: 'tab-liveview' },
  { name: 'securityfeed', label: 'Security Feed', icon: 'tab-securityfeed' },
  { name: 'automations', label: 'Automations', icon: 'tab-automations' },
  { name: 'syncmodule', label: 'Sync Module', icon: 'tab-syncmodule' },
  { name: 'status', label: 'Status', icon: 'tab-status' },
  { name: 'ai', label: 'AI', icon: 'tab-ai' },
  { name: 'usage', label: 'AI Usage', icon: 'tab-usage' },
  { name: 'models', label: 'Models', icon: 'tab-models' },
  { name: 'security', label: 'Security Events', icon: 'tab-security' },
  { name: 'assistant', label: 'Security Assistant', icon: 'tab-assistant' },
  { name: 'vehicles', label: 'Vehicles', icon: 'tab-vehicles' },
  { name: 'assets', label: 'Assets', icon: 'tab-assets' },
  { name: 'biometrics', label: 'Biometrics', icon: 'tab-biometrics' },
  { name: 'storage', label: 'Storage', icon: 'tab-storage' },
  { name: 'backup', label: 'Backup & Restore', icon: 'tab-backup' },
]

/** Every tab id, for validating a `?tab=` parameter without a second
 * copy of the list (see App.vue's kiosk mode). */
export const TAB_NAMES: TabName[] = TABS.map((tab) => tab.name)
