<script setup lang="ts">
import { onMounted, ref } from 'vue'
import Card from 'primevue/card'
import Tab from 'primevue/tab'
import TabList from 'primevue/tablist'
import TabPanel from 'primevue/tabpanel'
import TabPanels from 'primevue/tabpanels'
import Tabs from 'primevue/tabs'
import BlueprintsCard from './BlueprintsCard.vue'
import DashboardBuilderCard from './DashboardBuilderCard.vue'
import EntityReferenceCard from './EntityReferenceCard.vue'
import NotificationChannelsCard from './NotificationChannelsCard.vue'
import RecipeBuilder from './RecipeBuilder.vue'
import { AUTOMATION_RECIPES } from './recipes/automations'
import { SCRIPT_RECIPES } from './recipes/scripts'
import { getCameras } from '../../api/clips'

const cameras = ref<string[]>([])

onMounted(async () => {
  try {
    cameras.value = (await getCameras()).map((stat) => stat.camera)
  } catch {
    // Not worth a toast: every builder treats an empty camera list as "all
    // cameras", which is both the safe default and the common choice.
  }
})
</script>

<template>
  <div class="auto-content">
    <h2>Home Assistant</h2>
    <p>
      The add-on publishes sensors and fires events every poll cycle. Pick what you want below, set it up the way you
      want it, and copy the YAML straight into Home Assistant — no hand-editing of someone else's thresholds.
    </p>

    <Card class="auto-card">
      <template #content>
        <!-- lazy: only the open panel is rendered, so a builder is never
             computing YAML (or colliding on a duplicate selector) behind a
             tab nobody is looking at. -->
        <Tabs value="automations" lazy>
          <TabList>
            <Tab value="automations">Automations</Tab>
            <Tab value="scripts">Scripts &amp; Helpers</Tab>
            <Tab value="dashboards">Dashboards</Tab>
            <Tab value="blueprints">Blueprints</Tab>
            <Tab value="reference">Entities &amp; Events</Tab>
          </TabList>
          <TabPanels>
            <TabPanel value="automations">
              <RecipeBuilder :recipes="AUTOMATION_RECIPES" :cameras="cameras" storage-key="blink.automations.recipe" />
            </TabPanel>
            <TabPanel value="scripts">
              <RecipeBuilder :recipes="SCRIPT_RECIPES" :cameras="cameras" storage-key="blink.automations.script" />
            </TabPanel>
            <TabPanel value="dashboards">
              <DashboardBuilderCard :cameras="cameras" />
            </TabPanel>
            <TabPanel value="blueprints">
              <BlueprintsCard />
            </TabPanel>
            <TabPanel value="reference">
              <EntityReferenceCard />
            </TabPanel>
          </TabPanels>
        </Tabs>
      </template>
    </Card>

    <NotificationChannelsCard />

    <Card class="auto-card">
      <template #title>💡 Tips</template>
      <template #content>
        <ul>
          <li>
            Enable <strong>Watch HA Events</strong> in add-on settings so a clip downloads right after motion instead of
            on the next poll.
          </li>
          <li>
            Tune <strong>Post-Motion Download Delay</strong> (default 30 s) to how quickly your cameras finish uploading
            to Blink.
          </li>
          <li>
            The generated automations only ever <em>read</em> — nothing here can delete a clip or change an add-on
            setting.
          </li>
          <li>
            Clips default to <code>/share/blink-clips/</code>, separate from HA's <code>/config/snapshots/</code>.
          </li>
          <li>
            The Video.js player supports keyboard shortcuts: <code>Space</code> play/pause, <code>← →</code> skip 10 s,
            <code>F</code> fullscreen, <code>M</code> mute, <code>↑ ↓</code> prev/next clip.
          </li>
        </ul>
      </template>
    </Card>
  </div>
</template>

<style scoped>
/* Wider than the shared .auto-content reading width (820px), scoped to this
   page: the builder is a picker column plus a form plus a YAML preview, and
   at 820px the form collapses to one field per row and the preview wraps.
   AI/AI Usage/Models are prose and keep the narrower measure. */
.auto-content {
  max-width: 1120px;
}
.auto-card {
  margin-bottom: 1.5rem;
}
</style>
