<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Activity, CheckCircle2, CircleAlert, Database, RefreshCw, Server } from '@lucide/vue'

const health = ref(null)
const loading = ref(false)
const checkedAt = ref(null)
const error = ref('')
const services = computed(() => [
  { name: 'Backend', key: 'backend', icon: Server },
  { name: 'MySQL', key: 'mysql', icon: Database },
  { name: 'Milvus', key: 'milvus', icon: Database },
])
let timer
let controller

async function refresh() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), 15000)
  try {
    const response = await fetch('/api/v1/health', { signal: controller.signal })
    const result = await response.json()
    if (!result.services) throw new Error('Unavailable')
    health.value = result
  } catch {
    health.value = null
    error.value = 'Status unavailable'
  } finally {
    clearTimeout(timeout)
    checkedAt.value = new Date()
    loading.value = false
  }
}

onMounted(() => { refresh(); timer = setInterval(refresh, 30000) })
onUnmounted(() => { clearInterval(timer); controller?.abort() })
</script>

<template>
  <header class="topbar">
    <Activity aria-hidden="true" :size="24" />
    <span>Enterprise Multi-Source AI Agent</span>
    <span class="environment">Development</span>
  </header>
  <main>
    <div class="heading">
      <div><p class="eyebrow">Operations</p><h1>System status</h1></div>
      <button @click="refresh" :disabled="loading" title="Refresh status" aria-label="Refresh status">
        <RefreshCw :size="20" :class="{ spinning: loading }" />
      </button>
    </div>
    <section class="summary" aria-live="polite">
      <CheckCircle2 v-if="health?.status === 'healthy'" :size="24" />
      <CircleAlert v-else :size="24" />
      <div>
        <h2>{{ loading && !checkedAt ? 'Checking services' : error || (health?.status === 'healthy' ? 'All services operational' : 'Service attention required') }}</h2>
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
  </main>
  <footer>Enterprise Multi-Source AI Agent <span>Runtime monitor</span></footer>
</template>
