<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { CheckCircle2, CircleAlert, Database, MessagesSquare, RefreshCw, Server } from '@lucide/vue'
import { api } from '../api'

const health = ref(null)
const conversations = ref([])
const documents = ref([])
const loading = ref(false)
const error = ref('')
const checkedAt = ref(null)
const services = computed(() => [
  { name: 'Backend', key: 'backend', icon: Server },
  { name: 'MySQL', key: 'mysql', icon: Database },
  { name: 'Milvus', key: 'milvus', icon: Database },
])
let timer

async function refresh() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    // 健康检查与两个列表并行拉取；单个失败用 catch 降级为占位数据，避免整页报错。
    const [status, conversationPage, documentPage] = await Promise.all([
      api.health().catch((failure) => ({ status: 'degraded', services: {}, failure })),
      api.listConversations(5).catch(() => ({ items: [] })),
      api.listDocuments(5).catch(() => ({ items: [] })),
    ])
    health.value = status.services ? status : null
    error.value = status.failure ? 'Dependency status unavailable' : ''
    conversations.value = conversationPage.items || []
    documents.value = documentPage.items || []
  } catch {
    health.value = null
    error.value = 'Status unavailable'
  } finally {
    checkedAt.value = new Date()
    loading.value = false
  }
}

onMounted(() => {
  refresh()
  // 每 30s 自动刷新一次依赖状态，卸载时清理定时器。
  timer = setInterval(refresh, 30000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="heading">
    <div><p class="eyebrow">Operations</p><h1>System status</h1></div>
    <button class="icon-button" :disabled="loading" title="Refresh status" aria-label="Refresh status" @click="refresh">
      <RefreshCw :size="20" :class="{ spinning: loading }" />
    </button>
  </div>
  <section class="summary" aria-live="polite">
    <CheckCircle2 v-if="health?.status === 'healthy'" :size="24" />
    <CircleAlert v-else :size="24" />
    <div>
      <h2>
        {{ loading && !checkedAt ? 'Checking services' : error || (health?.status === 'healthy' ? 'All services operational' : 'Service attention required') }}
      </h2>
      <p>{{ checkedAt ? `Last checked ${checkedAt.toLocaleTimeString()}` : 'Pending check' }}</p>
    </div>
  </section>
  <section aria-labelledby="services-heading">
    <h2 id="services-heading" class="section-title">Services</h2>
    <ul class="service-list">
      <li v-for="service in services" :key="service.key">
        <component :is="service.icon" :size="22" aria-hidden="true" />
        <span class="service-name">{{ service.name }}</span>
        <span class="status" :class="{ healthy: health?.services[service.key] === 'healthy' }">
          <CheckCircle2 v-if="health?.services[service.key] === 'healthy'" :size="16" />
          <CircleAlert v-else :size="16" />
          {{ health?.services[service.key] === 'healthy' ? 'Operational' : (loading && !checkedAt ? 'Checking' : 'Unavailable') }}
        </span>
      </li>
    </ul>
  </section>
  <section class="split">
    <div>
      <h2 class="section-title">Recent conversations</h2>
      <p v-if="!conversations.length" class="muted">No conversations yet.</p>
      <ul class="plain-list">
        <li v-for="item in conversations" :key="item.conversation_id">
          <MessagesSquare :size="18" aria-hidden="true" />
          <RouterLink :to="{ name: 'chat', query: { conversation: item.conversation_id } }">
            {{ item.title }}
          </RouterLink>
          <span class="muted">{{ item.updated_at }}</span>
        </li>
      </ul>
    </div>
    <div>
      <h2 class="section-title">Recent documents</h2>
      <p v-if="!documents.length" class="muted">No documents indexed yet.</p>
      <ul class="plain-list">
        <li v-for="item in documents" :key="item.document_id">
          <Database :size="18" aria-hidden="true" />
          <span>{{ item.title }}</span>
          <span class="muted">{{ item.status }}</span>
        </li>
      </ul>
    </div>
  </section>
</template>
