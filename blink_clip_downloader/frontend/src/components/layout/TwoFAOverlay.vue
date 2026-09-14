<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import { useAuthStore } from '../../stores/auth'

const auth = useAuthStore()
const code = ref('')

// The autofill token for a one-time passcode: it is what makes iOS/Android
// offer the code straight from the SMS or email, and stops a password
// manager filling the Blink account password into a six-digit box.
//
// Bound rather than written as a literal attribute because static HTML
// analysis sees `<InputText>`, not the `<input>` PrimeVue actually renders,
// and flags a valid autocomplete token on a tag it does not recognise as a
// form control. The behaviour is unchanged and pinned by
// TwoFAOverlay.spec.ts's "reaches the real input element" test.
const OTP_AUTOCOMPLETE = 'one-time-code'

const inputEl = ref<InstanceType<typeof InputText>>()

// InputText's declared type doesn't expose `$el` (it's a real property on
// every Vue component instance at runtime, just not part of the narrow
// props/slots/emits type PrimeVue exports) — go through the actual DOM
// <input> it renders as its root element to focus it.
function focusCodeInput() {
  ;(inputEl.value as unknown as { $el?: HTMLInputElement } | undefined)?.$el?.focus()
}

watch(
  () => auth.needsTwoFA,
  (needs, wasNeeding) => {
    if (needs && !wasNeeding) {
      code.value = ''
      void nextTick(focusCodeInput)
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
      void nextTick(focusCodeInput)
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
          <InputText
            id="two-fa-code"
            ref="inputEl"
            v-model="code"
            type="text"
            inputmode="numeric"
            pattern="[0-9]*"
            maxlength="6"
            placeholder="• • • • • •"
            :autocomplete="OTP_AUTOCOMPLETE"
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
          <Button style="font-size: 0.9rem; padding: 0.6rem 1.1rem" :disabled="auth.twoFASubmitting" @click="submit">
            {{
              auth.twoFAPhase === 'verifying'
                ? '⏳ Verifying…'
                : auth.twoFAPhase === 'submitted'
                  ? '✓ Submitted'
                  : 'Verify'
            }}
          </Button>
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
