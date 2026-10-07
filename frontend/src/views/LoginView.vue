<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { LogIn } from '@lucide/vue'
import { ApiError, api, session } from '../api'

const username = ref('demo')
const password = ref('')
const error = ref('')
const busy = ref(false)
const route = useRoute()
const router = useRouter()

async function submit() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    const result = await api.login(username.value.trim(), password.value)
    // 登录成功后持久化会话令牌与用户信息，再回跳来源页面（默认根路径）。
    session.save(result.session.token, result.user)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    router.replace(redirect)
  } catch (failure) {
    error.value =
      // 401 统一提示凭据无效，其余错误沿用后端消息。
      failure instanceof ApiError && failure.status === 401
        ? 'Invalid credentials.'
        : failure.message || 'Sign in failed.'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="auth-card">
    <p class="eyebrow">Authentication</p>
    <h1>Sign in</h1>
    <p class="muted">
      Minimal MVP authentication. Protected Conversation, Run, Approval, Document, Ingestion,
      and Structured Data APIs require this session.
    </p>
    <form @submit.prevent="submit">
      <label>
        Username
        <input v-model="username" name="username" autocomplete="username" required />
      </label>
      <label>
        Password
        <input
          v-model="password"
          name="password"
          type="password"
          autocomplete="current-password"
          required
        />
      </label>
      <p v-if="error" class="alert" role="alert">{{ error }}</p>
      <button class="primary" type="submit" :disabled="busy">
        <LogIn :size="18" aria-hidden="true" />
        {{ busy ? 'Signing in' : 'Sign in' }}
      </button>
    </form>
  </div>
</template>
