<script setup lang="ts">
// Arm or disarm the system at set times of the week. The list is read and
// replaced whole (like the camera configurations); the add-on decides when a
// schedule is due, so this card only edits the list and reports what each
// schedule last did.
import { computed, onMounted, ref, useId } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
// Imported under a name that is not an HTML tag: a template parsed as HTML
// reads `<Select>` as a `<select>` with no id, and cannot see its `input-id`
// belongs to the label beside it.
import PvSelect from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import ToggleSwitch from 'primevue/toggleswitch'
import { describeApiError } from '../../api/client'
import { getArmingSchedules, saveArmingSchedules } from '../../api/syncModule'
import type { ArmingSchedule, ArmingScheduleOutcome, ArmingSchedulesResponse, SyncModuleInfo } from '../../api/types'
import { useConfirm } from '../../composables/useConfirm'
import { useToastStore } from '../../stores/toast'

const props = defineProps<{ modules: SyncModuleInfo[] }>()

/** The most schedules the add-on keeps (arming_schedule.py's MAX_SCHEDULES). */
const MAX_SCHEDULES = 20
const SYSTEM = 'system'
const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const PRESETS = [
  { label: 'Every day', days: [0, 1, 2, 3, 4, 5, 6] },
  { label: 'Weekdays', days: [0, 1, 2, 3, 4] },
  { label: 'Weekends', days: [5, 6] },
]
const ACTION_OPTIONS = [
  { label: 'Arm', value: 'arm' as const },
  { label: 'Disarm', value: 'disarm' as const },
]

const toast = useToastStore()
const confirm = useConfirm()

const schedules = ref<ArmingSchedule[]>([])
const outcomes = ref<Record<string, ArmingScheduleOutcome>>({})
const clock = ref({ now: '', timezone: '' })
const loading = ref(true)
const loadFailed = ref(false)
const saving = ref(false)

const dialogOpen = ref(false)
const editingId = ref('')
const draft = ref<ArmingSchedule>(blank())
const target = ref(SYSTEM)
const formError = ref('')

const ids = { name: useId(), time: useId(), target: useId() }

function blank(): ArmingSchedule {
  return {
    id: '',
    name: '',
    enabled: true,
    action: 'arm',
    time: '22:00',
    days: [0, 1, 2, 3, 4, 5, 6],
    scope: 'system',
    sync_module: '',
    network_id: '',
  }
}

function apply(result: ArmingSchedulesResponse) {
  schedules.value = result.schedules
  outcomes.value = result.outcomes
  clock.value = result.clock
}

// No stale-response guard: the card cannot start a second read or a save
// while one is in flight (Add and the rows are not offered until a read has
// succeeded, and "Try again" only appears once a read has failed).
async function load() {
  loading.value = true
  loadFailed.value = false
  try {
    apply(await getArmingSchedules())
  } catch {
    loadFailed.value = true
  } finally {
    loading.value = false
  }
}

/** Saves *next* as the whole list; true when the server kept it. */
async function persist(next: ArmingSchedule[]): Promise<boolean> {
  saving.value = true
  try {
    const result = await saveArmingSchedules(next)
    apply(result)
    return true
  } catch (err) {
    toast.show(describeApiError(err, 'Could not save the schedules.'), true)
    return false
  } finally {
    saving.value = false
  }
}

const atLimit = computed(() => schedules.value.length >= MAX_SCHEDULES)

const byNumber = (a: number, b: number) => a - b

function describeDays(days: number[]): string {
  const key = [...days].sort(byNumber).join(',')
  const preset = PRESETS.find((p) => p.days.join(',') === key)
  return preset
    ? preset.label
    : [...days]
        .sort(byNumber)
        .map((d) => DAY_LABELS[d])
        .join(', ')
}

function describeTarget(schedule: ArmingSchedule): string {
  return schedule.scope === 'sync_module' ? schedule.sync_module : 'Entire system'
}

function summary(schedule: ArmingSchedule): string {
  const verb = schedule.action === 'arm' ? 'Arm' : 'Disarm'
  return `${verb} ${describeTarget(schedule)} at ${schedule.time}`
}

function lastRun(schedule: ArmingSchedule): string {
  const outcome = outcomes.value[schedule.id]
  if (!outcome) return 'Has not run yet'
  const when = `${outcome.date} ${outcome.at.slice(11, 16)}`
  if (outcome.outcome === 'done') return `Last ran ${when}. ${outcome.detail}`
  return `${outcome.outcome === 'failed' ? 'Failed' : 'Missed'} ${when}. ${outcome.detail}`
}

function lastRunFailed(schedule: ArmingSchedule): boolean {
  const outcome = outcomes.value[schedule.id]
  return outcome !== undefined && outcome.outcome !== 'done'
}

// A schedule saved for a sync module that is no longer on the account still
// has to be shown (and kept) rather than silently turned into "Entire
// system", so its name is offered as a choice of its own.
const targetOptions = computed(() => {
  const options = [{ label: 'Entire system', value: SYSTEM }]
  const names = props.modules.map((m) => m.name)
  for (const name of names) options.push({ label: name, value: `module:${name}` })
  const saved = draft.value.scope === 'sync_module' ? draft.value.sync_module : ''
  if (saved && !names.includes(saved)) options.push({ label: `${saved} (not found)`, value: `module:${saved}` })
  return options
})

function openAdd() {
  editingId.value = ''
  draft.value = blank()
  target.value = SYSTEM
  formError.value = ''
  dialogOpen.value = true
}

function openEdit(schedule: ArmingSchedule) {
  editingId.value = schedule.id
  draft.value = { ...schedule, days: [...schedule.days] }
  target.value = schedule.scope === 'sync_module' ? `module:${schedule.sync_module}` : SYSTEM
  formError.value = ''
  dialogOpen.value = true
}

function toggleDay(day: number) {
  const days = draft.value.days
  draft.value.days = days.includes(day) ? days.filter((d) => d !== day) : [...days, day].sort(byNumber)
}

function chooseDays(days: number[]) {
  draft.value.days = [...days]
}

/** What still stands between the form and saving, said plainly. */
const missing = computed(() => {
  const needs: string[] = []
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(draft.value.time)) needs.push('set a time')
  if (!draft.value.days.length) needs.push('pick at least one day')
  return needs
})

async function saveDraft() {
  if (saving.value) return
  if (missing.value.length) {
    formError.value = `To save: ${missing.value.join(' and ')}.`
    return
  }
  const entry: ArmingSchedule = { ...draft.value, name: draft.value.name.trim() }
  if (target.value === SYSTEM) {
    entry.scope = 'system'
    entry.sync_module = ''
    entry.network_id = ''
  } else {
    const name = target.value.slice('module:'.length)
    const module = props.modules.find((m) => m.name === name)
    entry.scope = 'sync_module'
    entry.sync_module = name
    entry.network_id = module?.network_id != null ? String(module.network_id) : ''
  }
  const next = editingId.value
    ? schedules.value.map((s) => (s.id === editingId.value ? entry : s))
    : [...schedules.value, entry]
  if (await persist(next)) {
    dialogOpen.value = false
    toast.show('Schedule saved')
  }
}

async function setEnabled(schedule: ArmingSchedule, enabled: boolean) {
  if (saving.value) return
  await persist(schedules.value.map((s) => (s.id === schedule.id ? { ...s, enabled } : s)))
}

async function remove(schedule: ArmingSchedule) {
  if (saving.value) return
  if (!(await confirm(`${summary(schedule)} will no longer run.`, 'Delete this schedule?'))) return
  if (await persist(schedules.value.filter((s) => s.id !== schedule.id))) toast.show('Schedule deleted')
}

onMounted(load)
</script>

<template>
  <section class="arming-schedules" aria-labelledby="arming-schedules-title" data-testid="arming-schedules">
    <div class="as-header">
      <div>
        <h3 id="arming-schedules-title">Schedules</h3>
        <p class="as-intro">
          Arm or disarm on set days and times. Times are read on the add-on's own clock<template v-if="clock.now">
            — it is {{ clock.now }}{{ clock.timezone ? ` ${clock.timezone}` : '' }} there now</template
          >.
        </p>
      </div>
      <Button
        label="Add schedule"
        icon="pi pi-plus"
        size="small"
        :disabled="loading || loadFailed || atLimit"
        @click="openAdd"
      />
    </div>

    <Message v-if="loadFailed" severity="error" size="small" :closable="false">
      Could not load the schedules.
      <Button label="Try again" size="small" text @click="load" />
    </Message>
    <p v-else-if="loading" class="as-empty">Loading schedules…</p>
    <p v-else-if="!schedules.length" class="as-empty">
      No schedules yet. Add one to arm the system every night, or disarm it each morning.
    </p>
    <ul v-else class="as-list">
      <li v-for="schedule in schedules" :key="schedule.id" class="as-row" :class="{ 'as-off': !schedule.enabled }">
        <ToggleSwitch
          :model-value="schedule.enabled"
          :disabled="saving"
          :aria-label="`${schedule.enabled ? 'Turn off' : 'Turn on'} ${schedule.name || summary(schedule)}`"
          @update:model-value="setEnabled(schedule, $event as boolean)"
        />
        <div class="as-body">
          <div class="as-title">{{ schedule.name || summary(schedule) }}</div>
          <div class="as-detail">
            <template v-if="schedule.name">{{ summary(schedule) }} · </template>{{ describeDays(schedule.days) }}
          </div>
          <div class="as-last" :class="{ 'as-last-bad': lastRunFailed(schedule) }">{{ lastRun(schedule) }}</div>
        </div>
        <div class="as-actions">
          <Button
            icon="pi pi-pencil"
            text
            rounded
            size="small"
            :aria-label="`Edit ${schedule.name || summary(schedule)}`"
            @click="openEdit(schedule)"
          />
          <Button
            icon="pi pi-trash"
            text
            rounded
            size="small"
            severity="danger"
            :aria-label="`Delete ${schedule.name || summary(schedule)}`"
            :disabled="saving"
            @click="remove(schedule)"
          />
        </div>
      </li>
    </ul>

    <Dialog
      v-model:visible="dialogOpen"
      modal
      :header="editingId ? 'Edit schedule' : 'Add schedule'"
      :draggable="false"
      :style="{ width: 'min(30rem, 96vw)' }"
    >
      <form class="as-form" @submit.prevent="saveDraft">
        <div class="as-field">
          <label :for="ids.name">Name (optional)</label>
          <InputText :id="ids.name" v-model="draft.name" maxlength="48" placeholder="Bedtime" />
        </div>

        <div class="as-field">
          <span :id="`${ids.target}-action`">What it does</span>
          <SelectButton
            v-model="draft.action"
            :options="ACTION_OPTIONS"
            option-label="label"
            option-value="value"
            :allow-empty="false"
            :aria-labelledby="`${ids.target}-action`"
          />
        </div>

        <div class="as-field">
          <label :for="ids.target">Applies to</label>
          <PvSelect
            v-model="target"
            :input-id="ids.target"
            :options="targetOptions"
            option-label="label"
            option-value="value"
          />
        </div>

        <div class="as-field">
          <label :for="ids.time">Time (24-hour)</label>
          <InputText :id="ids.time" v-model="draft.time" type="time" />
        </div>

        <fieldset class="as-field as-fieldset">
          <legend>Days</legend>
          <div class="as-days">
            <Button
              v-for="(label, day) in DAY_LABELS"
              :key="label"
              :label="label"
              size="small"
              :outlined="!draft.days.includes(day)"
              :aria-pressed="draft.days.includes(day)"
              @click="toggleDay(day)"
            />
          </div>
          <div class="as-presets">
            <Button
              v-for="preset in PRESETS"
              :key="preset.label"
              :label="preset.label"
              size="small"
              text
              @click="chooseDays(preset.days)"
            />
          </div>
        </fieldset>

        <Message v-if="formError" severity="error" size="small" :closable="false">{{ formError }}</Message>
      </form>
      <template #footer>
        <Button label="Cancel" text :disabled="saving" @click="dialogOpen = false" />
        <Button label="Save" :loading="saving" :disabled="saving" @click="saveDraft" />
      </template>
    </Dialog>
  </section>
</template>

<style scoped>
.arming-schedules {
  margin-top: 1.5rem;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
  padding: 1.25rem;
  min-width: 0;
}
.as-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.75rem;
}
.as-header h3 {
  margin: 0 0 0.25rem;
}
.as-intro {
  margin: 0;
  color: var(--muted);
  font-size: 0.9rem;
}
.as-empty {
  color: var(--muted);
  margin: 1rem 0 0;
}
.as-list {
  list-style: none;
  margin: 1rem 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}
.as-row {
  display: flex;
  align-items: center;
  gap: 0.9rem;
  padding: 0.75rem 0;
  border-top: 1px solid var(--border);
}
.as-off .as-body {
  opacity: 0.6;
}
.as-body {
  flex: 1;
  min-width: 0;
}
.as-title {
  font-weight: 600;
  overflow-wrap: anywhere;
}
.as-detail,
.as-last {
  color: var(--muted);
  font-size: 0.875rem;
}
.as-last-bad {
  color: var(--danger);
}
.as-actions {
  display: flex;
  flex-shrink: 0;
}
.as-form {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.as-field {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}
.as-fieldset {
  border: 0;
  margin: 0;
  padding: 0;
  min-width: 0;
}
.as-fieldset legend {
  padding: 0;
  margin-bottom: 0.4rem;
}
.as-days {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
}
.as-presets {
  display: flex;
  flex-wrap: wrap;
}
</style>
