import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import RecipeFieldInput from './RecipeFieldInput.vue'
import type { FieldValue, RecipeField } from './recipes/types'

function mountField(field: RecipeField, value: FieldValue, cameras: string[] = []) {
  const wrapper = mount(RecipeFieldInput, {
    props: {
      field,
      cameras,
      modelValue: value,
      // A real two-way harness: a no-op handler silently breaks any test
      // that expects a second interaction to build on the first.
      'onUpdate:modelValue': (v: FieldValue) => wrapper.setProps({ modelValue: v }),
    },
  })
  return wrapper
}

describe('RecipeFieldInput', () => {
  it('renders a number field with its suffix and label', async () => {
    const wrapper = mountField(
      { key: 'threshold', label: 'Notify above', type: 'number', default: 85, suffix: '%' },
      85,
    )
    expect(wrapper.text()).toContain('Notify above')
    const input = wrapper.find('input')
    expect((input.element as HTMLInputElement).value).toContain('85')
  })

  it('passes a decimal step through instead of rounding it away', () => {
    const wrapper = mountField(
      { key: 'confidence', label: 'Confidence', type: 'number', default: 0.6, step: 0.05 },
      0.6,
    )
    expect((wrapper.find('input').element as HTMLInputElement).value).toBe('0.6')
  })

  it('treats a cleared number box as zero rather than breaking the preview', async () => {
    const wrapper = mountField({ key: 'n', label: 'N', type: 'number', default: 5 }, 5)
    await wrapper.findComponent({ name: 'InputNumber' }).vm.$emit('update:modelValue', null)
    expect(wrapper.props('modelValue')).toBe(0)
  })

  it('renders a text field and emits what is typed', async () => {
    const wrapper = mountField(
      { key: 'service', label: 'Notify service', type: 'text', default: 'notify.notify' },
      'notify.notify',
    )
    await wrapper.find('input').setValue('notify.mobile_app_x')
    expect(wrapper.props('modelValue')).toBe('notify.mobile_app_x')
  })

  it('falls back to an empty string when a text input clears itself', async () => {
    const wrapper = mountField({ key: 't', label: 'T', type: 'text', default: 'x' }, 'x')
    await wrapper.findComponent({ name: 'InputText' }).vm.$emit('update:modelValue', null)
    expect(wrapper.props('modelValue')).toBe('')
  })

  it('renders a toggle and flips it', async () => {
    const wrapper = mountField({ key: 'critical', label: 'Critical', type: 'toggle', default: false }, false)
    await wrapper.find('input[type="checkbox"]').setValue(true)
    expect(wrapper.props('modelValue')).toBe(true)
  })

  it('offers the live camera list for a camera-sourced multiselect', () => {
    const wrapper = mountField(
      { key: 'cameras', label: 'Cameras', type: 'multiselect', default: [], source: 'cameras' },
      [],
      ['Front Door', 'Back Yard'],
    )
    const options = wrapper.findComponent({ name: 'MultiSelect' }).props('options') as {
      label: string
    }[]
    expect(options.map((o) => o.label)).toEqual(['Front Door', 'Back Yard'])
  })

  it('uses the recipe options for a multiselect that is not camera-sourced', async () => {
    const wrapper = mountField(
      {
        key: 'sources',
        label: 'Sources',
        type: 'multiselect',
        default: [],
        options: [{ label: 'Motion', value: 'pir' }],
      },
      [],
    )
    const multi = wrapper.findComponent({ name: 'MultiSelect' })
    expect(multi.props('options')).toEqual([{ label: 'Motion', value: 'pir' }])
    await multi.vm.$emit('update:modelValue', null)
    expect(wrapper.props('modelValue')).toEqual([])
  })

  it('renders a select of the declared options', () => {
    const wrapper = mountField(
      {
        key: 'mode',
        label: 'Deliver by',
        type: 'select',
        default: 'tts',
        options: [
          { label: 'Speaking', value: 'tts' },
          { label: 'Notifying', value: 'notify' },
        ],
      },
      'tts',
    )
    expect(wrapper.findComponent({ name: 'Select' }).props('options')).toHaveLength(2)
  })

  it('renders a masked time field', () => {
    const wrapper = mountField({ key: 'at', label: 'Send at', type: 'time', default: '08:00' }, '08:00')
    expect(wrapper.findComponent({ name: 'InputMask' }).props('mask')).toBe('99:99')
  })

  it('shows help text only when the field has some', () => {
    expect(
      mountField({ key: 'a', label: 'A', type: 'text', default: '', help: 'why' }, '').text(),
    ).toContain('why')
    expect(
      mountField({ key: 'a', label: 'A', type: 'text', default: '' }, '').find('.recipe-field-help')
        .exists(),
    ).toBe(false)
  })

  it('coerces a stored value of the wrong shape for a number field', () => {
    const asString = mountField(
      { key: 'n', label: 'N', type: 'number', default: 5 },
      '12' as unknown as FieldValue,
    )
    expect(asString.findComponent({ name: 'InputNumber' }).props('modelValue')).toBe(12)

    const nonsense = mountField(
      { key: 'n', label: 'N', type: 'number', default: 5 },
      'abc' as unknown as FieldValue,
    )
    expect(nonsense.findComponent({ name: 'InputNumber' }).props('modelValue')).toBe(0)
  })

  it('shows an empty box for a non-string value on a text field', () => {
    const wrapper = mountField(
      { key: 't', label: 'T', type: 'text', default: '' },
      7 as unknown as FieldValue,
    )
    expect(wrapper.findComponent({ name: 'InputText' }).props('modelValue')).toBe('')
  })

  it('tolerates a value of the wrong shape for the field type', () => {
    // Mid-edit a list field can briefly hold anything the parent stored.
    const wrapper = mountField(
      { key: 'cameras', label: 'Cameras', type: 'multiselect', default: [] },
      'not a list' as unknown as FieldValue,
    )
    expect(wrapper.findComponent({ name: 'MultiSelect' }).props('modelValue')).toEqual([])
  })

  it('stores a multiselect choice', async () => {
    const wrapper = mountField(
      { key: 'cameras', label: 'Cameras', type: 'multiselect', default: [], source: 'cameras' },
      [],
      ['Front Door'],
    )
    await wrapper.findComponent({ name: 'MultiSelect' }).vm.$emit('update:modelValue', ['Front Door'])
    expect(wrapper.props('modelValue')).toEqual(['Front Door'])
  })

  it('stores a select choice', async () => {
    const wrapper = mountField(
      {
        key: 'mode',
        label: 'Deliver by',
        type: 'select',
        default: 'tts',
        options: [
          { label: 'Speaking', value: 'tts' },
          { label: 'Notifying', value: 'notify' },
        ],
      },
      'tts',
    )
    await wrapper.findComponent({ name: 'Select' }).vm.$emit('update:modelValue', 'notify')
    expect(wrapper.props('modelValue')).toBe('notify')
  })

  it('stores a typed time', async () => {
    const wrapper = mountField({ key: 'at', label: 'At', type: 'time', default: '' }, '')
    await wrapper.findComponent({ name: 'InputMask' }).vm.$emit('update:modelValue', '07:30')
    expect(wrapper.props('modelValue')).toBe('07:30')
  })

  it('prefers the field placeholder over the generic one', () => {
    const multi = mountField(
      { key: 'c', label: 'C', type: 'multiselect', default: [], placeholder: 'Every camera' },
      [],
    )
    expect(multi.findComponent({ name: 'MultiSelect' }).props('placeholder')).toBe('Every camera')

    const time = mountField(
      { key: 't', label: 'T', type: 'time', default: '', placeholder: '22:00' },
      '',
    )
    expect(time.findComponent({ name: 'InputMask' }).props('placeholder')).toBe('22:00')
  })

  it('renders a number field with no suffix at all', () => {
    const wrapper = mountField({ key: 'n', label: 'N', type: 'number', default: 3 }, 3)
    expect(wrapper.findComponent({ name: 'InputNumber' }).props('suffix')).toBeFalsy()
  })
})
