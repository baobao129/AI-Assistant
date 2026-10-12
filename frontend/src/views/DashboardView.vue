<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { CheckCircle2, CircleAlert, Database, MessagesSquare, RefreshCw, Server } from '@lucide/vue'
import { api } from '../api'
import { statusLabel } from '../labels'

const health = ref(null)
const conversations = ref([])
const documents = ref([])
const loading = ref(false)
const error = ref('')
const checkedAt = ref(null)
const services = computed(() => [
  { name: '后端服务', key: 'backend', icon: Server },
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
    error.value = status.failure ? '无法读取服务状态' : ''
    conversations.value = conversationPage.items || []
    documents.value = documentPage.items || []
  } catch {
    health.value = null
    error.value = '状态暂不可用'
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
    <div><p class="eyebrow">运行状态</p><h1>系统状态</h1></div>
    <button class="icon-button" :disabled="loading" title="刷新状态" aria-label="刷新状态" @click="refresh">
      <RefreshCw :size="20" :class="{ spinning: loading }" />
    </button>
  </div>
  <section class="summary" aria-live="polite">
    <CheckCircle2 v-if="health?.status === 'healthy'" :size="24" />
    <CircleAlert v-else :size="24" />
    <div>
      <h2>
        {{ loading && !checkedAt ? '正在检查服务' : error || (health?.status === 'healthy' ? '所有服务运行正常' : '部分服务需要检查') }}
      </h2>
      <p>{{ checkedAt ? `检查时间 ${checkedAt.toLocaleTimeString('zh-CN')}` : '等待检查' }}</p>
    </div>
  </section>
  <section aria-labelledby="services-heading">
    <h2 id="services-heading" class="section-title">服务</h2>
    <ul class="service-list">
      <li v-for="service in services" :key="service.key">
        <component :is="service.icon" :size="22" aria-hidden="true" />
        <span class="service-name">{{ service.name }}</span>
        <span class="status" :class="{ healthy: health?.services[service.key] === 'healthy' }">
          <CheckCircle2 v-if="health?.services[service.key] === 'healthy'" :size="16" />
          <CircleAlert v-else :size="16" />
          {{ health?.services[service.key] === 'healthy' ? '正常' : (loading && !checkedAt ? '检查中' : '不可用') }}
        </span>
      </li>
    </ul>
  </section>
  <section class="split">
    <div>
      <h2 class="section-title">最近对话</h2>
      <p v-if="!conversations.length" class="muted">暂无对话。</p>
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
      <h2 class="section-title">最近文档</h2>
      <p v-if="!documents.length" class="muted">暂无文档。</p>
      <ul class="plain-list">
        <li v-for="item in documents" :key="item.document_id">
          <Database :size="18" aria-hidden="true" />
          <span>{{ item.title }}</span>
          <span class="muted">{{ statusLabel(item.status) }}</span>
        </li>
      </ul>
    </div>
  </section>
</template>
