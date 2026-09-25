import { onMounted, onUnmounted } from 'vue'
import { useClipViewerStore } from '../stores/clipViewer'

/** A clip named anywhere in a Home Assistant panel route, `…/clip/<id>`. */
const CLIP_ROUTE = /\/clip\/([^/?#]+)/

/** Home Assistant's panel route as it posts it: the part of the URL naming
 *  the panel, and whatever follows it. */
interface PanelRoute {
  prefix?: unknown
  path?: unknown
}

/** The clip id a panel route names, or null.
 *
 *  Reads prefix and path together because Home Assistant splits them at a
 *  different point depending on which URL the panel was opened under —
 *  `/app/<slug>` + `/clip/<id>` for the link an alert sends, but
 *  `/<slug>/clip` + `/<id>` for the same path under the sidebar entry's own
 *  URL — while the text they make up together is the same. */
export function clipIdFromRoute(route: PanelRoute | undefined): string | null {
  if (!route) return null
  const prefix = typeof route.prefix === 'string' ? route.prefix : ''
  const path = typeof route.path === 'string' ? route.path : ''
  const match = CLIP_ROUTE.exec(prefix + path)
  if (!match) return null
  try {
    return decodeURIComponent(match[1])
  } catch {
    return null
  }
}

/** Opens the clip an alert linked to.
 *
 *  Two ways in. `?clip=<id>` on the page's own URL, read once at start.
 *  And, inside Home Assistant (2026.2 and later), an alert links to
 *  `/app/<slug>/clip/<id>`: the panel always loads this page at its own
 *  root, but tells a page that subscribes (`home-assistant/subscribe-
 *  properties`) what its route is, and resends it whenever that changes —
 *  so a second alert tapped while the panel is already open opens that
 *  clip too. Resends of a route already handled (a rotated phone resends
 *  everything) do nothing, or closing the clip would not stick.
 *
 *  Only messages from the page's own parent and origin count: under
 *  ingress, Home Assistant and this page share an origin, and nothing else
 *  should be able to open clips by posting to it. */
export function useClipDeepLink() {
  const viewer = useClipViewerStore()
  const embedded = window.parent !== window
  let handledRoute: string | null = null

  function onMessage(event: MessageEvent) {
    if (event.source !== window.parent || event.origin !== window.location.origin) return
    const data = event.data as { type?: unknown; route?: PanelRoute } | null
    if (data?.type !== 'home-assistant/properties') return
    const clipId = clipIdFromRoute(data.route)
    const route = clipId ?? ''
    if (route === handledRoute) return
    handledRoute = route
    if (clipId) viewer.requestOpen(clipId)
  }

  onMounted(() => {
    const fromQuery = new URLSearchParams(window.location.search).get('clip')
    if (fromQuery) viewer.requestOpen(fromQuery)
    if (!embedded) return
    window.addEventListener('message', onMessage)
    window.parent.postMessage({ type: 'home-assistant/subscribe-properties' }, window.location.origin)
  })

  onUnmounted(() => {
    if (!embedded) return
    window.removeEventListener('message', onMessage)
    window.parent.postMessage({ type: 'home-assistant/unsubscribe-properties' }, window.location.origin)
  })
}
