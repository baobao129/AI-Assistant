<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Activity, Database, LogOut, MessagesSquare, ShieldCheck } from '@lucide/vue'
import { api, session } from './api'

const route = useRoute()
const router = useRouter()
const busy = ref(false)

const user = computed(() => session.user)
const navigation = computed(() => [
  { name: 'dashboard', label: 'Dashboard', icon: ShieldCheck },
  { name: 'chat', label: 'Chat', icon: MessagesSquare },
  { name: 'data', label: 'Data', icon: Database },
])

async function signOut() {
  busy.value = true
  try {
    await api.logout()
  } catch {
    // 登出是幂等的：即使请求失败，也仍然清理本地会话并跳转到登录页。
  } finally {
    session.clear()
    busy.value = false
    router.push({ name: 'login' })
  }
}
</script>

<template>
  <header class="topbar">
    <Activity aria-hidden="true" :size="24" />
    <span>Enterprise Multi-Source AI Agent</span>
    <nav v-if="session.authenticated" class="main-nav" aria-label="Primary">
      <RouterLink
        v-for="item in navigation"
        :key="item.name"
        :to="{ name: item.name }"
        class="nav-link"
        :class="{ active: route.name === item.name }"
      >
        <component :is="item.icon" :size="18" aria-hidden="true" />
        {{ item.label }}
      </RouterLink>
    </nav>
    <div class="topbar-right">
      <span v-if="user" class="environment">{{ user.display_name }}</span>
      <button
        v-if="session.authenticated"
        class="icon-button"
        title="Sign out"
        aria-label="Sign out"
        :disabled="busy"
        @click="signOut"
      >
        <LogOut :size="18" />
      </button>
    </div>
  </header>
  <main :class="{ wide: route.name === 'chat' || route.name === 'data' }">
    <RouterView />
  </main>
  <footer>Enterprise Multi-Source AI Agent <span>Internal MVP</span></footer>
</template>
