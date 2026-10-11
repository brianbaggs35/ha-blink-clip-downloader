import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import RecognitionResultsCard from './RecognitionResultsCard.vue'
import { useRefreshStore } from '../../stores/refresh'
import type { FaceRecognitionStats } from '../../api/types'
import { deferred, errorResponse, jsonResponse } from './testing'

const NO_REASONS = {
  unfamiliar: 0,
  unclear_ignorable: 0,
  unclear: 0,
  not_approved: 0,
  extra_people: 0,
  unexamined: 0,
}

/** A panel with real activity in it; each test overrides what it is about. */
function stats(overrides: Partial<FaceRecognitionStats> = {}): FaceRecognitionStats {
  return {
    days: 30,
    recognition_enabled: true,
    ignore_unclear_faces: false,
    clips_checked: 80,
    clips_with_faces: 64,
    clips_recognized: 31,
    clips_set_aside: 0,
    faces_seen: 412,
    faces_matched: 301,
    faces_unclear: 84,
    held_back: { ...NO_REASONS, unfamiliar: 9, unclear_ignorable: 24 },
    ...overrides,
  }
}

function stubStats(...bodies: (FaceRecognitionStats | Response | Promise<Response>)[]) {
  const fetchMock = vi.fn()
  for (const body of bodies) {
    fetchMock.mockImplementationOnce(() =>
      body instanceof Promise || 'ok' in body ? Promise.resolve(body) : Promise.resolve(jsonResponse(body)),
    )
  }
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

async function mountCard() {
  const wrapper = mount(RecognitionResultsCard)
  await flushPromises()
  return wrapper
}

const card = (wrapper: Awaited<ReturnType<typeof mountCard>>) => wrapper.find('.recognition-results-card')
const reasons = (wrapper: Awaited<ReturnType<typeof mountCard>>) =>
  wrapper.findAll('[data-reason]').map((li) => li.attributes('data-reason'))

describe('RecognitionResultsCard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  describe('when there is nothing useful to say', () => {
    it('stays out of the way until the first answer arrives', async () => {
      const pending = deferred<Response>()
      stubStats(pending.promise)
      const wrapper = await mountCard()
      expect(card(wrapper).exists()).toBe(false)

      pending.resolve(jsonResponse(stats()))
      await flushPromises()
      expect(card(wrapper).exists()).toBe(true)
    })

    it('is not shown when recognition is off and nothing was ever recorded', async () => {
      stubStats(stats({ recognition_enabled: false, clips_checked: 0, clips_with_faces: 0 }))
      expect(card(await mountCard()).exists()).toBe(false)
    })

    it('says it could not load when there is nothing else to show', async () => {
      stubStats(errorResponse(500))
      const wrapper = await mountCard()
      expect(wrapper.text()).toContain('Failed to load recognition results.')
      // No answer, so no figure to put in the subtitle: the usual window.
      expect(wrapper.text()).toContain('last 30 days')
    })

    it('explains where the figures will come from once recognition is on', async () => {
      stubStats(stats({ clips_checked: 0, clips_with_faces: 0, clips_recognized: 0 }))
      const wrapper = await mountCard()
      expect(wrapper.text()).toContain('Nothing to show yet')
      expect(wrapper.find('.results-tiles').exists()).toBe(false)
    })

    it('still shows what was recorded after recognition was switched off', async () => {
      stubStats(stats({ recognition_enabled: false }))
      const wrapper = await mountCard()
      expect(wrapper.find('.results-tiles').exists()).toBe(true)
    })

    it('says so when clips were checked but no face was ever in one', async () => {
      stubStats(stats({ clips_checked: 12, clips_with_faces: 0, clips_recognized: 0 }))
      const wrapper = await mountCard()
      expect(wrapper.text()).toContain('checked 12 clips and saw no faces in any of them')
      expect(wrapper.find('.results-tiles').exists()).toBe(false)
    })

    it('says "clip" for a single clip checked', async () => {
      stubStats(stats({ clips_checked: 1, clips_with_faces: 0, clips_recognized: 0 }))
      expect(await mountCard().then((w) => w.text())).toContain('checked 1 clip and saw')
    })
  })

  describe('the figures', () => {
    it('counts the clips with faces, the ones recognized and the ones that were not', async () => {
      stubStats(stats())
      const wrapper = await mountCard()
      const tiles = wrapper.findAll('.results-tile').map((t) => t.text())
      expect(tiles[0]).toBe('Clips with faces64')
      expect(tiles[1]).toBe('Recognized31 (48%)')
      expect(tiles[2]).toBe('Not recognized33')
      expect(wrapper.text()).toContain('last 30 days')
    })

    it('rounds the share recognized', async () => {
      stubStats(stats({ clips_with_faces: 3, clips_recognized: 2, held_back: { ...NO_REASONS, unfamiliar: 1 } }))
      expect((await mountCard()).findAll('.results-tile')[1]!.text()).toContain('(67%)')
    })

    it('says which faces matched and which matched nobody, clear or not', async () => {
      stubStats(stats())
      const wrapper = await mountCard()
      expect(wrapper.find('.faces-line').text()).toBe(
        '412 faces seen: 301 matched a household member, 111 matched nobody (84 unclear, 27 clear).',
      )
    })

    it('keeps it short when every face that matched nobody was unclear', async () => {
      stubStats(stats({ faces_seen: 10, faces_matched: 7, faces_unclear: 3 }))
      expect((await mountCard()).find('.faces-line').text()).toBe(
        '10 faces seen: 7 matched a household member, 3 matched nobody, all of them unclear.',
      )
    })

    it('does not break the unmatched faces down when none were unclear', async () => {
      stubStats(stats({ faces_seen: 10, faces_matched: 7, faces_unclear: 0 }))
      expect((await mountCard()).find('.faces-line').text()).toBe(
        '10 faces seen: 7 matched a household member, 3 matched nobody.',
      )
    })

    it('is glad to report that every face matched', async () => {
      stubStats(stats({ faces_seen: 6, faces_matched: 6, faces_unclear: 0, held_back: NO_REASONS }))
      expect((await mountCard()).find('.faces-line').text()).toBe('6 faces seen, all matched a household member.')
    })

    it('counts a single face in the singular', async () => {
      stubStats(stats({ faces_seen: 1, faces_matched: 1, faces_unclear: 0 }))
      expect((await mountCard()).find('.faces-line').text()).toBe('1 face seen, all matched a household member.')
    })

    it('explains what "unclear" means only when some faces were', async () => {
      stubStats(stats())
      expect((await mountCard()).text()).toContain('Unclear means blurred, cut off by the edge of the frame')

      stubStats(stats({ faces_unclear: 0 }))
      const without = await mountCard()
      expect(without.text()).not.toContain('Unclear means')
      expect(without.text()).toContain('counted once for every sampled frame')
    })
  })

  describe('why clips did not count as recognized', () => {
    it('lists only the reasons that happened, with what to do about each', async () => {
      stubStats(
        stats({
          held_back: { ...NO_REASONS, unfamiliar: 9, not_approved: 1, extra_people: 2, unexamined: 4, unclear: 3 },
        }),
      )
      const wrapper = await mountCard()
      expect(reasons(wrapper)).toEqual(['unfamiliar', 'unclear', 'not_approved', 'extra_people', 'unexamined'])
      const text = wrapper.find('.results-reasons').text()
      expect(text).toContain('A clear face matched nobody')
      expect(text).toContain("isn't enrolled yet")
      expect(text).toContain('Unclear faces with no recognized person to put them down to')
      expect(text).toContain("Someone enrolled isn't approved")
      expect(text).toContain('Approve them on their card above')
      expect(text).toContain('More people than faces recognized')
      expect(text).toContain("A frame couldn't be examined")
      expect(text).toContain('The add-on log says why')
      expect(wrapper.find('[data-reason="unfamiliar"] .reason-count').text()).toBe('9')
    })

    it('counts how many clips there are to explain', async () => {
      stubStats(stats())
      expect((await mountCard()).find('.results-heading').text()).toBe("Why 33 clips didn't count as recognized")
    })

    it('says "clip" when one clip was held back', async () => {
      stubStats(stats({ clips_recognized: 63, held_back: { ...NO_REASONS, unfamiliar: 1 } }))
      expect((await mountCard()).find('.results-heading').text()).toBe("Why 1 clip didn't count as recognized")
    })

    it('says how turning on Ignore Unclear Faces would change things when it is off', async () => {
      stubStats(stats({ ignore_unclear_faces: false }))
      const hint = (await mountCard()).find('[data-reason="unclear_ignorable"]').text()
      expect(hint).toContain('Unclear faces matched nobody')
      expect(hint).toContain('Turn on Ignore Unclear Faces')
    })

    it('does not ask for what is already done when it is on', async () => {
      stubStats(stats({ ignore_unclear_faces: true }))
      const hint = (await mountCard()).find('[data-reason="unclear_ignorable"]').text()
      expect(hint).toContain('Analyzed before Ignore Unclear Faces was turned on')
      expect(hint).not.toContain('Turn on')
    })

    it('has no list at all when every clip with faces was recognized', async () => {
      stubStats(stats({ clips_recognized: 64, held_back: NO_REASONS }))
      const wrapper = await mountCard()
      expect(wrapper.find('.results-reasons-section').exists()).toBe(false)
      expect(wrapper.findAll('.results-tile')[2]!.text()).toBe('Not recognized0')
    })
  })

  describe('clips recognized by setting unclear faces aside', () => {
    it('is left out when none were', async () => {
      stubStats(stats({ clips_set_aside: 0 }))
      expect((await mountCard()).find('.set-aside').exists()).toBe(false)
    })

    it('says how many clips it helped', async () => {
      stubStats(stats({ ignore_unclear_faces: true, clips_set_aside: 12 }))
      expect((await mountCard()).find('.set-aside').text()).toBe(
        'Ignore Unclear Faces counted 12 clips as recognized by setting unclear faces aside.',
      )
    })

    it('counts a single clip in the singular', async () => {
      stubStats(stats({ ignore_unclear_faces: true, clips_set_aside: 1 }))
      expect((await mountCard()).find('.set-aside').text()).toContain('counted 1 clip as recognized')
    })
  })

  describe('keeping up', () => {
    it('reloads when the sidebar refreshes, and shows the new figures', async () => {
      const fetchMock = stubStats(stats(), stats({ clips_recognized: 40 }))
      const wrapper = await mountCard()
      expect(wrapper.findAll('.results-tile')[1]!.text()).toContain('31')

      useRefreshStore().bump()
      await flushPromises()
      expect(fetchMock).toHaveBeenCalledTimes(2)
      expect(wrapper.findAll('.results-tile')[1]!.text()).toContain('40')
    })

    it('keeps the last figures when a reload fails', async () => {
      stubStats(stats(), errorResponse(500))
      const wrapper = await mountCard()

      useRefreshStore().bump()
      await flushPromises()
      expect(wrapper.text()).not.toContain('Failed to load')
      expect(wrapper.findAll('.results-tile')[1]!.text()).toContain('31')
    })

    it('ignores an answer that arrives after a newer one', async () => {
      const slow = deferred<Response>()
      stubStats(slow.promise, stats({ clips_recognized: 40 }))
      const wrapper = mount(RecognitionResultsCard)
      await flushPromises()

      useRefreshStore().bump()
      await flushPromises()
      expect(wrapper.findAll('.results-tile')[1]!.text()).toContain('40')

      slow.resolve(jsonResponse(stats({ clips_recognized: 5 })))
      await flushPromises()
      expect(wrapper.findAll('.results-tile')[1]!.text()).toContain('40')
    })
  })
})
