<script setup lang="ts">
import Accordion from 'primevue/accordion'
import AccordionContent from 'primevue/accordioncontent'
import AccordionHeader from 'primevue/accordionheader'
import AccordionPanel from 'primevue/accordionpanel'
import CodeBlock from './CodeBlock.vue'
import { BLUEPRINTS, BLUEPRINT_INSTALL_STEPS } from './recipes/blueprints'
</script>

<template>
  <div class="blueprints">
    <p class="bp-intro">
      A blueprint is worth it when the same automation gets set up more than once — one per phone, one per camera group,
      one per threshold. Home Assistant then asks for the inputs in its own UI, and every copy stays in step with the
      blueprint.
    </p>

    <Accordion value="suspicious">
      <AccordionPanel v-for="bp in BLUEPRINTS" :key="bp.id" :value="bp.id">
        <AccordionHeader>
          <span class="bp-head">
            <span aria-hidden="true">{{ bp.icon }}</span>
            <span>{{ bp.name }}</span>
          </span>
        </AccordionHeader>
        <AccordionContent>
          <p class="bp-desc">{{ bp.description }}</p>
          <CodeBlock :code="bp.yaml" :filename="bp.filename" />
        </AccordionContent>
      </AccordionPanel>
    </Accordion>

    <h4 class="bp-step">Installing one</h4>
    <ol class="bp-steps">
      <li v-for="step in BLUEPRINT_INSTALL_STEPS" :key="step">{{ step }}</li>
    </ol>
  </div>
</template>

<style scoped>
.bp-intro,
.bp-desc {
  font-size: 0.83rem;
  color: var(--muted);
  line-height: 1.55;
  margin: 0 0 0.8rem;
}
.bp-head {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.bp-step {
  font-size: 0.85rem;
  font-weight: 700;
  color: var(--text);
  margin: 1.2rem 0 0.4rem;
}
.bp-steps {
  padding-left: 1.2rem;
  margin: 0;
}
.bp-steps li {
  font-size: 0.82rem;
  color: var(--muted);
  line-height: 1.6;
  margin-bottom: 0.3rem;
}
</style>
