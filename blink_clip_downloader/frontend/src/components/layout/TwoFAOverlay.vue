<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { useAuthStore } from '../../stores/auth'

const auth = useAuthStore()
const code = ref('')
const inputEl = ref<HTMLInputElement>()

watch(
  () => auth.needsTwoFA,
  (needs, wasNeeding) => {
    if (needs && !wasNeeding) {
      code.value = ''
      void nextTick(() => inputEl.value?.focus())
    }
  },
)

// Once the store clears the message after a failed attempt, refocus so the
// user can immediately retry without reaching for the mouse.
watch(
  () => auth.twoFASubmitting,
  (submitting, wasSubmitting) => {
    if (!submitting && wasSubmitting && auth.twoFAMessageIsError) {
      code.value = ''
      void nextTick(() => inputEl.value?.focus())
    }
  },
)

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter') submit()
  if (e.key.length === 1 && !/\d/.test(e.key)) e.preventDefault()
}

function submit() {
  auth.submitTwoFA(code.value)
}
</script>

<template>
  <div class="modal-bg" :class="{ open: auth.needsTwoFA }" style="z-index: 200">
    <div class="modal" style="max-width: 480px">
      <div class="modal-body" style="padding: 1.8rem 1.6rem">
        <div class="modal-title" style="font-size: 1.08rem; margin-bottom: 0.5rem">🔐 Two-Factor Authentication</div>
        <p style="color: var(--muted); font-size: 0.86rem; line-height: 1.55; margin-bottom: 1.2rem">
          Blink has sent a verification code to your registered email address or phone. Enter it below to complete
          sign-in.
        </p>
        <div style="display: flex; gap: 0.5rem; align-items: stretch">
          <label for="two-fa-code" class="sr-only">Verification code</label>
          <input
            id="two-fa-code"
            ref="inputEl"
            v-model="code"
            type="text"
            inputmode="numeric"
            pattern="[0-9]*"
            maxlength="6"
            placeholder="• • • • • •"
            autocomplete="one-time-code"
            class="tag-input"
            style="
              flex: 1;
              padding: 0.6rem 0.9rem;
              font-size: 1.4rem;
              letter-spacing: 0.35em;
              text-align: center;
              font-family: monospace;
              width: auto;
            "
            @keydown="onKeydown"
          />
          <button
            class="btn"
            style="font-size: 0.9rem; padding: 0.6rem 1.1rem"
            :disabled="auth.twoFASubmitting"
            @click="submit"
          >
            {{
              auth.twoFAPhase === 'verifying'
                ? '⏳ Verifying…'
                : auth.twoFAPhase === 'submitted'
                  ? '✓ Submitted'
                  : 'Verify'
            }}
          </button>
        </div>
        <div
          style="margin-top: 0.75rem; font-size: 0.83rem; min-height: 1.3rem; line-height: 1.4"
          :style="{ color: auth.twoFAMessageIsError ? 'var(--danger)' : 'var(--muted)' }"
        >
          {{ auth.twoFAMessage }}
        </div>
        <p style="color: var(--muted); font-size: 0.76rem; margin-top: 1rem; line-height: 1.45">
          Code not arriving? Check your spam folder or restart the add-on to request a new code. Codes expire quickly.
        </p>
      </div>
    </div>
  </div>
</template>
