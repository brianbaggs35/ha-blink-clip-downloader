import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import Dialog from 'primevue/dialog'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import ToggleSwitch from 'primevue/toggleswitch'
import ArmingScheduleCard from './ArmingScheduleCard.vue'
import { useConfirmStore } from '../../stores/confirm'
import { useToastStore } from '../../stores/toast'
import type { ArmingSchedule, ArmingScheduleOutcome, ArmingSchedulesResponse, SyncModuleInfo } from '../../api/types'

function jsonResponse(body: unknown, ok = true, status = 200) {
  return {
    ok,
    status,
    statusText: ok ? 'OK' : 'Bad Request',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body)),
  } as Response
}

function schedule(overrides: Partial<ArmingSchedule> = {}): ArmingSchedule {
  return {
    id: 'aaaaaaaaaaaa',
    name: '',
    enabled: true,
    action: 'arm',
    time: '22:00',
    days: [0, 1, 2, 3, 4, 5, 6],
    scope: 'system',
    sync_module: '',
    network_id: '',
    ...overrides,
  }
}

function payload(
  schedules: ArmingSchedule[] = [],
  outcomes: Record<string, ArmingScheduleOutcome> = {},
): ArmingSchedulesResponse {
  return { schedules, outcomes, clock: { now: '21:45', timezone: 'CET' } }
}

function module(name: string, networkId: number | string | null = 1): SyncModuleInfo {
  return {
    name,
    network_id: networkId,
    serial: null,
    version: null,
    status: 'online',
    online: true,
    armed: true,
    region_id: null,
    local_storage: false,
    cameras: [],
  }
}

const MODULES = [module('Home', 12345), module('Shed', null)]

/** Answers GET with *state* and records every PUT, which it echoes back. */
function stubApi(initial: ArmingSchedulesResponse = payload(), putResult?: () => Promise<Response>) {
  let state = initial
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      if (url !== '/api/sync-modules/schedules') return Promise.reject(new Error(`unexpected fetch: ${url}`))
      if (init?.method === 'PUT') {
        if (putResult) return putResult()
        const sent = JSON.parse(String(init.body)).schedules as ArmingSchedule[]
        state = payload(
          sent.map((s) => ({ ...s, id: s.id || 'bbbbbbbbbbbb' })),
          state.outcomes,
        )
        return Promise.resolve(jsonResponse(state))
      }
      return Promise.resolve(jsonResponse(state))
    }),
  )
}

const puts = () => vi.mocked(fetch).mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === 'PUT')
const lastPut = (): ArmingSchedule[] =>
  JSON.parse(String((puts().at(-1)![1] as RequestInit).body)).schedules as ArmingSchedule[]

async function mountCard(modules: SyncModuleInfo[] = MODULES) {
  const wrapper = mount(ArmingScheduleCard, { props: { modules }, attachTo: document.body })
  await flushPromises()
  return wrapper
}

const body = () => new DOMWrapper(document.body)
const button = (label: string) =>
  body()
    .findAll('button')
    .find((b) => b.text().trim() === label)!
const byLabel = (label: string) => body().find(`button[aria-label="${label}"]`)

describe('ArmingScheduleCard', () => {
  beforeEach(() => setActivePinia(createPinia()))
  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  it('shows the clock the times are read on and an empty state', async () => {
    stubApi()
    const wrapper = await mountCard()
    expect(wrapper.text()).toContain('it is 21:45 CET there now')
    expect(wrapper.text()).toContain('No schedules yet')
  })

  it('shows the time without a zone name when the add-on reports none', async () => {
    stubApi({ ...payload(), clock: { now: '08:00', timezone: '' } })
    const wrapper = await mountCard()
    expect(wrapper.text()).toContain('it is 08:00 there now')
  })

  it('says nothing about the clock before it is known', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise(() => {})),
    )
    const wrapper = mount(ArmingScheduleCard, { props: { modules: MODULES } })
    expect(wrapper.text()).toContain('Loading schedules…')
    expect(wrapper.text()).not.toContain('there now')
    expect(wrapper.find('button').attributes('disabled')).toBeDefined()
  })

  it('offers a retry when the schedules cannot be read, and recovers', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new Error('down')))
    const wrapper = await mountCard()
    expect(wrapper.text()).toContain('Could not load the schedules')
    expect(wrapper.find('button').attributes('disabled')).toBeDefined()
    stubApi(payload([schedule()]))
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Try again')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('Could not load')
    expect(wrapper.text()).toContain('Arm Entire system at 22:00')
  })

  it('describes each schedule and what it last did', async () => {
    stubApi(
      payload(
        [
          schedule({ id: 'a1', name: 'Bedtime', days: [0, 1, 2, 3, 4] }),
          schedule({
            id: 'a2',
            action: 'disarm',
            time: '07:30',
            days: [5, 6],
            scope: 'sync_module',
            sync_module: 'Home',
          }),
          schedule({ id: 'a3', days: [0, 2] }),
          schedule({ id: 'a4', days: [0, 1, 2, 3, 4, 5, 6], enabled: false }),
          schedule({ id: 'a5' }),
        ],
        {
          a1: { date: '2026-11-17', at: '2026-11-17T22:00:03', outcome: 'done', detail: 'Armed 1 sync module' },
          a2: { date: '2026-11-17', at: '2026-11-17T07:30:00', outcome: 'failed', detail: 'Blink did not respond' },
          a3: {
            date: '2026-11-16',
            at: '2026-11-16T22:05:00',
            outcome: 'missed',
            detail: 'The add-on was not running',
          },
        },
      ),
    )
    const wrapper = await mountCard()
    const text = wrapper.text()
    expect(text).toContain('Bedtime')
    expect(text).toContain('Arm Entire system at 22:00 · Weekdays')
    expect(text).toContain('Last ran 2026-11-17 22:00. Armed 1 sync module')
    expect(text).toContain('Disarm Home at 07:30')
    expect(text).toContain('Weekends')
    expect(text).toContain('Failed 2026-11-17 07:30. Blink did not respond')
    expect(text).toContain('Mon, Wed')
    expect(text).toContain('Missed 2026-11-16 22:05. The add-on was not running')
    expect(text).toContain('Every day')
    expect(text).toContain('Has not run yet')
    expect(wrapper.findAll('.as-last-bad')).toHaveLength(2)
    expect(wrapper.findAll('.as-off')).toHaveLength(1)
  })

  it('adds a schedule for the whole system', async () => {
    stubApi()
    const wrapper = await mountCard()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Add schedule')!
      .trigger('click')
    await flushPromises()
    expect(body().text()).toContain('Add schedule')
    const name = body().find<HTMLInputElement>('input[placeholder="Bedtime"]')
    await name.setValue('  Night  ')
    await body().find<HTMLInputElement>('input[type="time"]').setValue('23:15')
    await button('Mon').trigger('click')
    await button('Sun').trigger('click')
    await button('Sun').trigger('click')
    await button('Save').trigger('click')
    await flushPromises()
    expect(lastPut()).toEqual([
      expect.objectContaining({
        id: '',
        name: 'Night',
        action: 'arm',
        time: '23:15',
        days: [1, 2, 3, 4, 5, 6],
        scope: 'system',
        sync_module: '',
        network_id: '',
      }),
    ])
    expect(useToastStore().message).toBe('Schedule saved')
    expect(body().find('.p-dialog').exists()).toBe(false)
    expect(wrapper.text()).toContain('Arm Entire system at 23:15')
  })

  it('adds a disarm for one sync module, sending the id it was shown', async () => {
    stubApi()
    const wrapper = await mountCard()
    await wrapper.find('button').trigger('click')
    await flushPromises()
    wrapper.findComponent(SelectButton).vm.$emit('update:modelValue', 'disarm')
    wrapper.findComponent(Select).vm.$emit('update:modelValue', 'module:Home')
    await flushPromises()
    await button('Weekdays').trigger('click')
    await button('Save').trigger('click')
    await flushPromises()
    expect(lastPut()[0]).toMatchObject({
      action: 'disarm',
      scope: 'sync_module',
      sync_module: 'Home',
      network_id: '12345',
      days: [0, 1, 2, 3, 4],
    })
  })

  it('sends no network id for a sync module that has none', async () => {
    stubApi()
    const wrapper = await mountCard()
    await wrapper.find('button').trigger('click')
    await flushPromises()
    wrapper.findComponent(Select).vm.$emit('update:modelValue', 'module:Shed')
    await flushPromises()
    await button('Save').trigger('click')
    await flushPromises()
    expect(lastPut()[0]).toMatchObject({ sync_module: 'Shed', network_id: '' })
  })

  it('will not save without a day or a time, and says what is missing', async () => {
    stubApi()
    const wrapper = await mountCard()
    await wrapper.find('button').trigger('click')
    await flushPromises()
    await button('Weekends').trigger('click')
    for (const day of ['Sat', 'Sun']) await button(day).trigger('click')
    await body().find<HTMLInputElement>('input[type="time"]').setValue('')
    await button('Save').trigger('click')
    await flushPromises()
    expect(body().text()).toContain('To save: set a time and pick at least one day.')
    expect(puts()).toHaveLength(0)
  })

  it('edits a schedule in place and keeps its id', async () => {
    stubApi(payload([schedule({ id: 'a1', name: 'Bedtime' }), schedule({ id: 'a2', time: '06:00' })]))
    const wrapper = await mountCard()
    await byLabel('Edit Bedtime').trigger('click')
    await flushPromises()
    expect(body().text()).toContain('Edit schedule')
    expect(body().find<HTMLInputElement>('input[placeholder="Bedtime"]').element.value).toBe('Bedtime')
    await body().find<HTMLInputElement>('input[type="time"]').setValue('21:00')
    await button('Save').trigger('click')
    await flushPromises()
    expect(lastPut().map((s) => [s.id, s.time])).toEqual([
      ['a1', '21:00'],
      ['a2', '06:00'],
    ])
    expect(wrapper.text()).toContain('21:00')
  })

  it("keeps a schedule's sync module even when the account no longer has it", async () => {
    stubApi(payload([schedule({ id: 'a1', scope: 'sync_module', sync_module: 'Old', network_id: '9' })]))
    const wrapper = await mountCard()
    await byLabel('Edit Arm Old at 22:00').trigger('click')
    await flushPromises()
    expect(wrapper.findComponent(Select).props('options')).toContainEqual({
      label: 'Old (not found)',
      value: 'module:Old',
    })
    await button('Save').trigger('click')
    await flushPromises()
    expect(lastPut()[0]).toMatchObject({ sync_module: 'Old', network_id: '' })
    wrapper.unmount()
  })

  it('shows why a save was refused and leaves the dialog open', async () => {
    stubApi(payload(), () => Promise.resolve(jsonResponse({ error: 'At most 20 schedules are kept' }, false, 400)))
    const wrapper = await mountCard()
    await wrapper.find('button').trigger('click')
    await flushPromises()
    await button('Save').trigger('click')
    await flushPromises()
    expect(useToastStore().message).toBe('At most 20 schedules are kept')
    expect(body().text()).toContain('Add schedule')
  })

  it('ignores a second Save while one is on its way', async () => {
    let finish: (r: Response) => void = () => {}
    stubApi(
      payload(),
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve
        }),
    )
    const wrapper = await mountCard()
    await wrapper.find('button').trigger('click')
    await flushPromises()
    await button('Save').trigger('click')
    // The button disables itself, so a second submit can only be Enter in a field.
    await body().find('form').trigger('submit')
    expect(puts()).toHaveLength(1)
    finish(jsonResponse(payload([schedule()])))
    await flushPromises()
  })

  it('turns a schedule on and off', async () => {
    stubApi(payload([schedule({ id: 'a1' }), schedule({ id: 'a2' })]))
    const wrapper = await mountCard()
    wrapper.findComponent(ToggleSwitch).vm.$emit('update:modelValue', false)
    await flushPromises()
    expect(lastPut().map((s) => [s.id, s.enabled])).toEqual([
      ['a1', false],
      ['a2', true],
    ])
    expect(wrapper.findAll('.as-off')).toHaveLength(1)
    expect(wrapper.find('input[role="switch"]').attributes('aria-label')).toBe('Turn on Arm Entire system at 22:00')
  })

  it('ignores a second switch while a save is on its way', async () => {
    let finish: (r: Response) => void = () => {}
    stubApi(
      payload([schedule({ id: 'a1' })]),
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve
        }),
    )
    const wrapper = await mountCard()
    const toggle = wrapper.findComponent(ToggleSwitch)
    toggle.vm.$emit('update:modelValue', false)
    toggle.vm.$emit('update:modelValue', false)
    expect(puts()).toHaveLength(1)
    finish(jsonResponse(payload([schedule({ id: 'a1', enabled: false })])))
    await flushPromises()
  })

  it('deletes a schedule once that is confirmed', async () => {
    stubApi(payload([schedule({ id: 'a1', name: 'Bedtime' }), schedule({ id: 'a2' })]))
    const wrapper = await mountCard()
    await byLabel('Delete Bedtime').trigger('click')
    const confirm = useConfirmStore()
    expect(confirm.open).toBe(true)
    expect(confirm.title).toBe('Delete this schedule?')
    confirm.settle(true)
    await flushPromises()
    expect(lastPut().map((s) => s.id)).toEqual(['a2'])
    expect(useToastStore().message).toBe('Schedule deleted')
    expect(wrapper.text()).not.toContain('Bedtime')
  })

  it('keeps the schedule when the delete is declined', async () => {
    stubApi(payload([schedule({ id: 'a1', name: 'Bedtime' })]))
    await mountCard()
    await byLabel('Delete Bedtime').trigger('click')
    useConfirmStore().settle(false)
    await flushPromises()
    expect(puts()).toHaveLength(0)
  })

  it('stays quiet when a delete cannot be saved', async () => {
    stubApi(payload([schedule({ id: 'a1', name: 'Bedtime' })]), () => Promise.reject(new Error('down')))
    const wrapper = await mountCard()
    await byLabel('Delete Bedtime').trigger('click')
    useConfirmStore().settle(true)
    await flushPromises()
    expect(useToastStore().message).toBe('Could not save the schedules.')
    expect(wrapper.text()).toContain('Bedtime')
  })

  it('ignores a delete while a save is on its way', async () => {
    let finish: (r: Response) => void = () => {}
    stubApi(
      payload([schedule({ id: 'a1', name: 'Bedtime' })]),
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve
        }),
    )
    const wrapper = await mountCard()
    wrapper.findComponent(ToggleSwitch).vm.$emit('update:modelValue', false)
    const remove = wrapper
      .findAllComponents({ name: 'Button' })
      .find((b) => b.attributes('aria-label') === 'Delete Bedtime')!
    remove.vm.$emit('click')
    await flushPromises()
    expect(useConfirmStore().open).toBe(false)
    finish(jsonResponse(payload([schedule({ id: 'a1', enabled: false, name: 'Bedtime' })])))
    await flushPromises()
  })

  it('stops offering new schedules at the limit', async () => {
    stubApi(payload(Array.from({ length: 20 }, (_, i) => schedule({ id: `a${i}`.padEnd(12, '0') }))))
    const wrapper = await mountCard()
    expect(wrapper.find('button').attributes('disabled')).toBeDefined()
  })

  it('closes the dialog on Cancel', async () => {
    stubApi()
    const wrapper = await mountCard()
    await wrapper.find('button').trigger('click')
    await flushPromises()
    await button('Cancel').trigger('click')
    await flushPromises()
    expect(body().find('.p-dialog').exists()).toBe(false)
  })

  it('closes the dialog when it is dismissed, and starts the next one fresh', async () => {
    stubApi(payload([schedule({ id: 'a1', name: 'Bedtime' })]))
    const wrapper = await mountCard()
    await byLabel('Edit Bedtime').trigger('click')
    await flushPromises()
    wrapper.findComponent(Dialog).vm.$emit('update:visible', false)
    await flushPromises()
    expect(body().find('.p-dialog').exists()).toBe(false)
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Add schedule')!
      .trigger('click')
    await flushPromises()
    expect(body().find<HTMLInputElement>('input[placeholder="Bedtime"]').element.value).toBe('')
  })
})
