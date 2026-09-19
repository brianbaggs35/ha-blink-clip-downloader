<script setup lang="ts">
import { computed, ref } from 'vue'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import MultiSelect from 'primevue/multiselect'
import SelectButton from 'primevue/selectbutton'
import ToggleSwitch from 'primevue/toggleswitch'
import CodeBlock from './CodeBlock.vue'
import {
  type DashboardOptions,
  DEFAULT_DASHBOARD_OPTIONS,
  castScriptYaml,
  cameraSetupSheet,
  dashboardYaml,
  kioskUrl,
} from './recipes/dashboard'

const props = defineProps<{ cameras: string[] }>()

const MODES = [
  { label: 'Camera entities', value: 'cameras' },
  { label: 'Embed this tab', value: 'iframe' },
]

const mode = ref<DashboardOptions['mode']>(DEFAULT_DASHBOARD_OPTIONS.mode)
const selectedCameras = ref<string[]>([])
const addonUrl = ref(DEFAULT_DASHBOARD_OPTIONS.addonUrl)
const columns = ref(DEFAULT_DASHBOARD_OPTIONS.columns)
const viewTitle = ref(DEFAULT_DASHBOARD_OPTIONS.viewTitle)
const viewPath = ref(DEFAULT_DASHBOARD_OPTIONS.viewPath)
const includeStorage = ref(true)
const includeStatus = ref(true)
const mediaPlayer = ref('media_player.nest_hub')

const options = computed<DashboardOptions>(() => ({
  // Empty means every camera, the same convention the add-on's own camera
  // options use.
  cameras: selectedCameras.value.length ? selectedCameras.value : props.cameras,
  addonUrl: addonUrl.value,
  columns: columns.value,
  viewTitle: viewTitle.value,
  viewPath: viewPath.value,
  includeStorage: includeStorage.value,
  includeStatus: includeStatus.value,
  mode: mode.value,
}))

const setupSheet = computed(() => cameraSetupSheet(options.value))
const lovelace = computed(() => dashboardYaml(options.value))
const castScript = computed(() => castScriptYaml(options.value, mediaPlayer.value))
const embedUrl = computed(() => kioskUrl(addonUrl.value))
</script>

<template>
  <div class="dash-builder">
    <div class="dash-fields">
      <div class="dash-field">
        <span class="field-label">Tiles come from</span>
        <SelectButton
          v-model="mode"
          :options="MODES"
          option-label="label"
          option-value="value"
          :allow-empty="false"
          aria-label="Dashboard tile source"
        />
        <span class="dash-help">
          {{
            mode === 'cameras'
              ? 'Real camera entities: a little setup, then everything that can show a camera can show these — including a Nest Hub.'
              : 'No Home Assistant setup at all — the dashboard embeds this tab. The viewing browser has to reach the add-on directly.'
          }}
        </span>
      </div>

      <div class="dash-field">
        <label for="dash-url" class="field-label">Add-on URL</label>
        <InputText id="dash-url" v-model="addonUrl" class="dash-wide" />
        <span class="dash-help">The direct-access port (default 8099), not the ingress address.</span>
      </div>

      <div v-if="mode === 'cameras'" class="dash-field">
        <label for="dash-cameras-input" class="field-label">Cameras</label>
        <MultiSelect
          v-model="selectedCameras"
          input-id="dash-cameras-input"
          :options="cameras"
          placeholder="All cameras"
          display="chip"
          filter
          class="dash-wide"
        />
      </div>

      <div v-if="mode === 'cameras'" class="dash-field">
        <label for="dash-columns" class="field-label">Tiles per row</label>
        <InputNumber
          v-model="columns"
          input-id="dash-columns"
          :min="1"
          :max="4"
          show-buttons
          button-layout="horizontal"
          class="dash-wide"
        />
      </div>

      <div class="dash-field">
        <label for="dash-title" class="field-label">View title</label>
        <InputText id="dash-title" v-model="viewTitle" class="dash-wide" />
      </div>

      <div class="dash-field">
        <label for="dash-path" class="field-label">View path</label>
        <InputText id="dash-path" v-model="viewPath" class="dash-wide" />
        <span class="dash-help">Used by the cast script below to name the view.</span>
      </div>

      <div class="dash-field">
        <label for="dash-storage" class="field-label">Include storage gauges</label>
        <ToggleSwitch v-model="includeStorage" input-id="dash-storage" />
      </div>

      <div class="dash-field">
        <label for="dash-status" class="field-label">Include a status card</label>
        <ToggleSwitch v-model="includeStatus" input-id="dash-status" />
      </div>
    </div>

    <template v-if="mode === 'cameras'">
      <h4 class="dash-step">1. Add one Generic Camera per Blink camera</h4>
      <p class="dash-note">
        Home Assistant's Generic Camera integration is set up in the UI only, so these cannot be generated for you — but
        the fiddly part, the exact snapshot URL, is below. Home Assistant fetches the image itself, which is why these
        tiles work on a cast display that can't reach the add-on.
      </p>
      <CodeBlock :code="setupSheet" filename="blink-camera-urls.txt" />

      <h4 class="dash-step">2. Paste the view into your dashboard</h4>
    </template>
    <template v-else>
      <h4 class="dash-step">Paste the view into your dashboard</h4>
      <p class="dash-note">
        The card embeds <code>{{ embedUrl }}</code> — this tab's Security Feed with the navigation hidden. An
        <code>http://</code> add-on inside an <code>https://</code> dashboard is blocked as mixed content, so this route
        suits a local-only Home Assistant.
      </p>
    </template>
    <p class="dash-note">
      Three-dot menu on the dashboard → Edit dashboard → three-dot menu → Raw configuration editor. Merge the
      <code>views:</code> entry into what is already there.
    </p>
    <CodeBlock :code="lovelace" filename="blink-dashboard.yaml" />

    <h4 class="dash-step">Put it on a Nest Hub or Chromecast</h4>
    <div class="dash-field dash-field-inline">
      <label for="dash-player" class="field-label">Display</label>
      <InputText id="dash-player" v-model="mediaPlayer" class="dash-wide" />
    </div>
    <CodeBlock :code="castScript" filename="blink-cast-cameras.yaml" />
    <Message severity="info" size="small" :closable="false" class="dash-cast-note">
      Casting a dashboard is Home Assistant's own feature and it needs your Home Assistant to be reachable over HTTPS
      (Nabu Casa Cloud counts). Camera-entity tiles are the route to use here: the display asks Home Assistant for the
      images, not the add-on.
    </Message>
  </div>
</template>

<style scoped>
.dash-fields {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(240px, 100%), 1fr));
  gap: 0.9rem 1rem;
  margin-bottom: 1.2rem;
}
.dash-field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  min-width: 0;
}
.dash-field-inline {
  max-width: 320px;
  margin-bottom: 0.6rem;
}
.dash-wide {
  width: 100%;
}
.dash-help {
  font-size: 0.72rem;
  color: var(--muted);
  line-height: 1.45;
}
.dash-step {
  font-size: 0.85rem;
  font-weight: 700;
  color: var(--text);
  margin: 1.1rem 0 0.35rem;
}
.dash-note {
  font-size: 0.8rem;
  color: var(--muted);
  line-height: 1.55;
  margin: 0 0 0.5rem;
}
.dash-cast-note {
  margin-top: 0.4rem;
}
</style>
