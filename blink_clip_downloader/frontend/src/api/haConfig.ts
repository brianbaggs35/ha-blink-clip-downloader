import { apiPost } from './client'

/** What Home Assistant's config API can create. Everything else the
 * Automations tab generates — blueprints, helpers, dashboards, anything
 * that lives in configuration.yaml — has no API and stays copy-only. */
export type CreatableKind = 'automation' | 'script' | 'scene'

export interface CreateResult {
  created: boolean
  /** Present on success: the name Home Assistant lists it under.
   *
   * Not an entity id. Home Assistant builds an automation's and a scene's
   * entity id from its alias/name rather than from the id this sent, so
   * the two usually differ — and some aliases carry the threshold the
   * user picked, making the entity id unpredictable. The name is what
   * Home Assistant's own lists show. */
  name?: string
  /** Present on refusal: why, in words meant for the user. */
  message?: string
}

/** Create (or replace) one automation/script/scene in Home Assistant.
 *
 * `objectId` is the add-on's own stable id for the recipe, not something
 * derived from the YAML, so pressing the button twice updates the same
 * object instead of leaving a second copy behind.
 */
export function createInHomeAssistant(kind: CreatableKind, objectId: string, yaml: string): Promise<CreateResult> {
  return apiPost('/api/ha/config/create', { kind, object_id: objectId, yaml })
}
