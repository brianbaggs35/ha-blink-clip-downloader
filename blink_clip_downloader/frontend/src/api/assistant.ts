import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from './client'
import type {
  AssistantAnswer,
  AssistantChat,
  AssistantChatSummary,
  AssistantModelsResponse,
  AssistantSettings,
  AssistantStatus,
} from './types'

export function getAssistantStatus(): Promise<AssistantStatus> {
  return apiGet('/api/assistant/status')
}

export function getAssistantModels(): Promise<AssistantModelsResponse> {
  return apiGet('/api/assistant/models')
}

export function saveAssistantSettings(
  settings: AssistantSettings,
): Promise<{ saved: boolean; settings: AssistantSettings }> {
  return apiPut('/api/assistant/settings', settings)
}

export function listAssistantChats(): Promise<AssistantChatSummary[]> {
  return apiGet('/api/assistant/chats')
}

export function createAssistantChat(title?: string): Promise<AssistantChat> {
  return apiPost('/api/assistant/chats', title ? { title } : {})
}

export function getAssistantChat(id: string): Promise<AssistantChat> {
  return apiGet(`/api/assistant/chats/${encodeURIComponent(id)}`)
}

export function renameAssistantChat(id: string, title: string): Promise<{ title: string }> {
  return apiPatch(`/api/assistant/chats/${encodeURIComponent(id)}`, { title })
}

export function deleteAssistantChat(id: string): Promise<{ deleted: boolean }> {
  return apiDelete(`/api/assistant/chats/${encodeURIComponent(id)}`)
}

export function askAssistant(id: string, question: string): Promise<AssistantAnswer> {
  return apiPost(`/api/assistant/chats/${encodeURIComponent(id)}/ask`, { question })
}
