<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import Button from 'primevue/button'
import Drawer from 'primevue/drawer'
import Message from 'primevue/message'
import Skeleton from 'primevue/skeleton'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'
import {
  askAssistant,
  createAssistantChat,
  deleteAssistantChat,
  getAssistantChat,
  getAssistantStatus,
  listAssistantChats,
  renameAssistantChat,
} from '../../api/assistant'
import { describeApiError } from '../../api/client'
import { providerLabel } from '../../api/constants'
import type { AssistantChatSummary, AssistantMessage, AssistantSettings, AssistantStatus } from '../../api/types'
import { useConfirm } from '../../composables/useConfirm'
import { useClipViewerStore } from '../../stores/clipViewer'
import { useToastStore } from '../../stores/toast'
import EmptyState from '../layout/EmptyState.vue'
import LoadingIndicator from '../layout/LoadingIndicator.vue'
import AssistantChatList from './AssistantChatList.vue'
import AssistantMessageBubble from './AssistantMessageBubble.vue'
import AssistantSettingsDialog from './AssistantSettingsDialog.vue'

const toast = useToastStore()
const confirm = useConfirm()
const clipViewer = useClipViewerStore()

const MAX_QUESTION = 1000
const SUGGESTIONS = [
  'What happened overnight?',
  'Was anything flagged as suspicious this week?',
  'Is my system armed, and are all cameras online?',
  'Which cameras have a low battery?',
  'How is face recognition doing?',
]

const loading = ref(true)
const status = ref<AssistantStatus | null>(null)
const chats = ref<AssistantChatSummary[]>([])
const activeId = ref<string | null>(null)
const messages = ref<AssistantMessage[]>([])
const draft = ref('')
const pending = ref(false)
const settingsOpen = ref(false)
const chatsOpen = ref(false)
const scroller = ref<HTMLElement | null>(null)
const composer = ref<{ $el: HTMLTextAreaElement } | null>(null)

// The chat list moves into a drawer on a phone, where a permanent sidebar
// would leave the conversation a sliver.
const narrowQuery = window.matchMedia('(max-width: 820px)')
const narrow = ref(narrowQuery.matches)
function onNarrowChange(e: MediaQueryListEvent) {
  narrow.value = e.matches
  if (!e.matches) chatsOpen.value = false
}

const ready = computed(() => !!status.value?.enabled && status.value.supported !== false)
const activeChat = computed(() => chats.value.find((c) => c.id === activeId.value))
const canSend = computed(() => ready.value && !pending.value && draft.value.trim().length > 0)

/** What the person is told about where their questions go. */
const privacyNote = computed(() => {
  const provider = providerLabel(status.value?.provider)
  const where = status.value?.sends_off_device
    ? `Each question sends ${provider} a text summary of your recent clips, events, battery and system status.`
    : `Questions are answered by ${provider} on your own network.`
  return `${where} Camera images, face photos and the names of people you have enrolled are never included.`
})

function scrollToEnd() {
  void nextTick(() => {
    if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
  })
}

async function loadChats() {
  chats.value = await listAssistantChats()
}

async function selectChat(id: string) {
  chatsOpen.value = false
  if (id === activeId.value) return
  try {
    const chat = await getAssistantChat(id)
    activeId.value = chat.id
    messages.value = chat.messages
    scrollToEnd()
  } catch (e) {
    toast.show(describeApiError(e, 'Could not open that chat'), true)
    await loadChats().catch(() => undefined)
  }
}

async function load() {
  try {
    status.value = await getAssistantStatus()
    await loadChats()
    if (chats.value.length) await selectChat(chats.value[0].id)
  } catch {
    toast.show('Failed to load the Security Assistant', true)
  } finally {
    loading.value = false
  }
}

/** A new chat, unless the one open is already empty: two blank chats side by
 * side are just clutter. */
async function newChat() {
  chatsOpen.value = false
  if (pending.value) return
  if (activeChat.value && messages.value.length === 0) {
    focusComposer()
    return
  }
  try {
    const chat = await createAssistantChat()
    await loadChats()
    activeId.value = chat.id
    messages.value = []
    focusComposer()
  } catch (e) {
    toast.show(describeApiError(e, 'Could not start a new chat'), true)
  }
}

async function renameChat(id: string, title: string) {
  try {
    const r = await renameAssistantChat(id, title)
    const chat = chats.value.find((c) => c.id === id)
    if (chat) chat.title = r.title
  } catch (e) {
    toast.show(describeApiError(e, 'Could not rename the chat'), true)
  }
}

async function removeChat(id: string) {
  if (pending.value) return
  const chat = chats.value.find((c) => c.id === id)
  if (!(await confirm(`Delete "${chat?.title ?? 'this chat'}"? Its messages cannot be recovered.`, 'Delete chat')))
    return
  try {
    await deleteAssistantChat(id)
    await loadChats()
    if (id === activeId.value) {
      activeId.value = null
      messages.value = []
      if (chats.value.length) await selectChat(chats.value[0].id)
    }
    toast.show('Chat deleted')
  } catch (e) {
    toast.show(describeApiError(e, 'Could not delete the chat'), true)
  }
}

function focusComposer() {
  void nextTick(() => composer.value?.$el?.focus())
}

async function send(text = draft.value) {
  const question = text.trim()
  if (!question || pending.value || !ready.value) return
  pending.value = true
  let chatId = activeId.value
  try {
    if (!chatId) {
      const chat = await createAssistantChat()
      chatId = chat.id
      activeId.value = chat.id
      messages.value = []
    }
    // Shown straight away; the server's copy replaces it when the answer lands.
    const shown: AssistantMessage = { role: 'user', content: question, at: new Date().toISOString() }
    messages.value = [...messages.value, shown]
    draft.value = ''
    scrollToEnd()
    const answer = await askAssistant(chatId, question)
    if (activeId.value === chatId) {
      messages.value = [...messages.value.slice(0, -1), ...answer.messages]
      scrollToEnd()
    }
    await loadChats()
  } catch (e) {
    // Nothing was saved for a failed question, so take it back out of the
    // conversation and hand the words back to be edited or retried.
    if (activeId.value === chatId && messages.value.at(-1)?.content === question) {
      messages.value = messages.value.slice(0, -1)
    }
    draft.value = question
    toast.show(describeApiError(e, 'The assistant could not answer'), true)
    await loadChats().catch(() => undefined)
  } finally {
    pending.value = false
    focusComposer()
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    void send()
  }
}

function onSettingsSaved(settings: AssistantSettings) {
  status.value = { ...(status.value as AssistantStatus), settings }
}

onMounted(() => {
  narrowQuery.addEventListener('change', onNarrowChange)
  void load()
})
onUnmounted(() => narrowQuery.removeEventListener('change', onNarrowChange))
</script>

<template>
  <div class="assistant-page">
    <div class="assistant-head">
      <h2>Security Assistant</h2>
      <Tag
        v-if="status?.enabled"
        severity="secondary"
        icon="pi pi-microchip-ai"
        :value="`${providerLabel(status.provider)} · ${status.settings.model || status.default_model}`"
        data-testid="assistant-model-tag"
      />
      <span class="assistant-head-spacer" />
      <Button
        v-if="narrow && ready"
        icon="pi pi-comments"
        label="Chats"
        size="small"
        severity="secondary"
        outlined
        data-testid="assistant-open-chats"
        @click="chatsOpen = true"
      />
      <Button
        v-if="status?.enabled"
        icon="pi pi-cog"
        size="small"
        severity="secondary"
        outlined
        aria-label="Assistant settings"
        title="Assistant settings"
        data-testid="assistant-settings-button"
        @click="settingsOpen = true"
      />
    </div>

    <div v-if="loading" class="assistant-loading"><LoadingIndicator /></div>

    <EmptyState v-else-if="!status?.enabled" title="🤖 AI Analysis Not Configured">
      The Security Assistant uses the AI provider that analyzes your clips. Turn on AI analysis in the add-on
      configuration to use it.
    </EmptyState>

    <template v-else>
      <Message v-if="status.supported === false" severity="warn" :closable="false" data-testid="assistant-unsupported">
        {{ providerLabel(status.provider) }} cannot answer questions. Set <code>ai_provider</code> in the add-on
        configuration to Anthropic, OpenAI, Hugging Face or Ollama to use the assistant.
      </Message>
      <Message v-else severity="secondary" size="small" :closable="false" class="assistant-privacy">
        {{ privacyNote }}
      </Message>

      <div class="assistant-layout">
        <aside v-if="!narrow" class="assistant-side">
          <AssistantChatList
            :chats="chats"
            :active-id="activeId"
            :busy="pending"
            @select="selectChat"
            @create="newChat"
            @rename="renameChat"
            @remove="removeChat"
          />
        </aside>
        <Drawer v-else v-model:visible="chatsOpen" header="Chats" position="left">
          <AssistantChatList
            :chats="chats"
            :active-id="activeId"
            :busy="pending"
            @select="selectChat"
            @create="newChat"
            @rename="renameChat"
            @remove="removeChat"
          />
        </Drawer>

        <section class="assistant-main" aria-label="Conversation">
          <div ref="scroller" class="assistant-scroll" data-testid="assistant-conversation">
            <div v-if="!messages.length" class="assistant-welcome">
              <i class="pi pi-shield assistant-welcome-icon" aria-hidden="true"></i>
              <h3>Ask about your security system</h3>
              <p>
                Events, flagged clips, camera status and battery, arming, face recognition: ask in your own words and
                the assistant answers from what your system has recorded.
              </p>
              <div v-if="ready" class="assistant-suggestions">
                <Button
                  v-for="s in SUGGESTIONS"
                  :key="s"
                  :label="s"
                  size="small"
                  severity="secondary"
                  outlined
                  rounded
                  :disabled="pending"
                  data-testid="assistant-suggestion"
                  @click="send(s)"
                />
              </div>
            </div>
            <template v-else>
              <AssistantMessageBubble
                v-for="(m, i) in messages"
                :key="i"
                :message="m"
                @open-clip="(id) => clipViewer.requestOpen(id)"
              />
            </template>
            <div v-if="pending" class="assistant-thinking" role="status" aria-label="The assistant is thinking">
              <Skeleton width="55%" height="0.9rem" />
              <Skeleton width="80%" height="0.9rem" />
              <Skeleton width="35%" height="0.9rem" />
            </div>
          </div>

          <form class="assistant-composer" @submit.prevent="send()">
            <Textarea
              ref="composer"
              v-model="draft"
              auto-resize
              rows="1"
              :maxlength="MAX_QUESTION"
              :disabled="!ready || pending"
              placeholder="Ask about your cameras, clips or alerts…"
              aria-label="Your question"
              class="assistant-input"
              data-testid="assistant-input"
              @keydown="onKeydown"
            />
            <Button
              type="submit"
              icon="pi pi-send"
              aria-label="Send question"
              :disabled="!canSend"
              :loading="pending"
              data-testid="assistant-send"
            />
          </form>
        </section>
      </div>

      <AssistantSettingsDialog v-model:visible="settingsOpen" :status="status" @saved="onSettingsSaved" />
    </template>
  </div>
</template>

<style scoped>
.assistant-page {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
  min-width: 0;
  min-height: 0;
  padding: 1.5rem 1.75rem;
  width: 100%;
}
.assistant-head {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
}
.assistant-head h2 {
  margin: 0;
}
.assistant-head-spacer {
  flex: 1;
}
.assistant-loading {
  padding: 2rem;
}
.assistant-layout {
  flex: 1;
  min-height: 0;
  display: flex;
  gap: 1.1rem;
}
.assistant-side {
  flex: none;
  width: 17rem;
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 0.85rem;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.assistant-main {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.assistant-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
  padding: 1.1rem;
}
.assistant-welcome {
  margin: auto;
  max-width: 34rem;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.6rem;
  color: var(--text-dim);
}
.assistant-welcome h3 {
  margin: 0;
  color: var(--text);
}
.assistant-welcome p {
  margin: 0;
  line-height: 1.5;
}
.assistant-welcome-icon {
  font-size: 2.2rem;
  color: var(--accent);
}
.assistant-suggestions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 0.5rem;
  margin-top: 0.6rem;
}
.assistant-thinking {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding: 0.8rem 1rem;
  max-width: 24rem;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--card);
}
.assistant-composer {
  display: flex;
  align-items: flex-end;
  gap: 0.6rem;
  padding: 0.8rem 1.1rem 1rem;
  border-top: 1px solid var(--border);
}
.assistant-input {
  flex: 1;
  min-width: 0;
  max-height: 9rem;
  resize: none;
}
@media (max-width: 820px) {
  .assistant-page {
    padding: 1rem;
  }
}
</style>
