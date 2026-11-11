<script setup lang="ts">
import { nextTick, ref } from 'vue'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Menu from 'primevue/menu'
import type { MenuItem } from 'primevue/menuitem'
import { fmtRelative } from '../../api/constants'
import type { AssistantChatSummary } from '../../api/types'

const props = defineProps<{
  chats: AssistantChatSummary[]
  activeId: string | null
  /** True while a question is being answered: new/delete would race it. */
  busy?: boolean
}>()

const emit = defineEmits<{
  select: [id: string]
  create: []
  rename: [id: string, title: string]
  remove: [id: string]
}>()

const MAX_TITLE = 60

const menu = ref<InstanceType<typeof Menu> | null>(null)
const menuItems = ref<MenuItem[]>([])
const editingId = ref<string | null>(null)
const draft = ref('')
// Escape cancels, but the input still blurs as it goes away; without this the
// blur handler would then save the text that was just thrown out.
let cancelled = false

/** The menu is rebuilt for the chat it opens on, so its commands never have
 * to look the chat up again. */
function openMenu(event: Event, chat: AssistantChatSummary) {
  menuItems.value = [
    {
      label: 'Rename',
      icon: 'pi pi-pencil',
      command: () => {
        cancelled = false
        draft.value = chat.title
        editingId.value = chat.id
      },
    },
    { label: 'Delete', icon: 'pi pi-trash', class: 'chat-menu-danger', command: () => emit('remove', chat.id) },
  ]
  menu.value?.toggle(event)
}

/** Called with the input when it appears, and with null when it goes. */
function focusInput(el: unknown) {
  const input = (el as { $el?: HTMLInputElement } | null)?.$el
  if (input) {
    void nextTick(() => {
      input.focus()
      input.select()
    })
  }
}

function commit() {
  if (cancelled || !editingId.value) return
  const id = editingId.value
  const title = draft.value.trim()
  editingId.value = null
  const original = props.chats.find((c) => c.id === id)
  if (title && original && title !== original.title) emit('rename', id, title)
}

function cancel() {
  cancelled = true
  editingId.value = null
}
</script>

<template>
  <nav class="chat-list" aria-label="Assistant chats">
    <Button
      label="New chat"
      icon="pi pi-plus"
      class="chat-new"
      :disabled="busy"
      data-testid="assistant-new-chat"
      @click="emit('create')"
    />
    <ul v-if="chats.length" class="chat-items">
      <li v-for="chat in chats" :key="chat.id" class="chat-item" :class="{ active: chat.id === activeId }">
        <InputText
          v-if="editingId === chat.id"
          :ref="focusInput"
          v-model="draft"
          size="small"
          class="chat-rename"
          :maxlength="MAX_TITLE"
          aria-label="Chat name"
          data-testid="assistant-rename-input"
          @keydown.enter.prevent="commit"
          @keydown.esc.prevent="cancel"
          @blur="commit"
        />
        <template v-else>
          <button
            type="button"
            class="chat-main"
            :aria-current="chat.id === activeId ? 'true' : undefined"
            data-testid="assistant-chat-item"
            @click="emit('select', chat.id)"
          >
            <span class="chat-title">{{ chat.title }}</span>
            <span class="chat-meta">{{ fmtRelative(chat.updated_at) }}</span>
          </button>
          <Button
            icon="pi pi-ellipsis-v"
            text
            rounded
            size="small"
            severity="secondary"
            class="chat-options"
            :aria-label="`Options for ${chat.title}`"
            :disabled="busy"
            data-testid="assistant-chat-options"
            @click="openMenu($event, chat)"
          />
        </template>
      </li>
    </ul>
    <p v-else class="chat-empty">No chats yet. Start one with a question.</p>
    <Menu ref="menu" :model="menuItems" popup />
  </nav>
</template>

<style scoped>
.chat-list {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  min-height: 0;
  min-width: 0;
}
.chat-new {
  width: 100%;
}
.chat-items {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  overflow-y: auto;
  min-height: 0;
}
.chat-item {
  display: flex;
  align-items: center;
  gap: 0.15rem;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  padding-right: 0.15rem;
  transition:
    background 0.15s var(--ease),
    border-color 0.15s var(--ease);
}
.chat-item:hover {
  background: var(--card-hover);
}
.chat-item.active {
  background: var(--tag-bg);
  border-color: var(--accent);
}
.chat-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
  text-align: left;
  background: none;
  border: none;
  color: var(--text);
  font: inherit;
  padding: 0.55rem 0.7rem;
  cursor: pointer;
  border-radius: var(--radius-sm);
}
.chat-main:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.chat-title {
  font-weight: 600;
  font-size: 0.9rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chat-meta {
  font-size: 0.75rem;
  color: var(--muted);
}
.chat-rename {
  flex: 1;
  min-width: 0;
  margin: 0.2rem;
}
.chat-empty {
  color: var(--muted);
  font-size: 0.85rem;
  padding: 0.25rem 0.25rem;
}
:global(.chat-menu-danger .p-menu-item-link),
:global(.chat-menu-danger .p-menu-item-icon),
:global(.chat-menu-danger .p-menu-item-label) {
  color: var(--danger);
}
</style>
