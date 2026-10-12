<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Database, FileUp, RefreshCw, Table2, Trash2 } from '@lucide/vue'
import { ApiError, api, session } from '../api'
import { errorMessage, statusLabel } from '../labels'

const documents = ref([])
const dataSources = ref([])
const tables = ref([])
const activeTable = ref(null)
const preview = ref(null)
const error = ref('')
const notice = ref('')
const busy = ref(false)
const uploading = ref(false)
const jobs = ref([])
const retrying = ref('')
const jobStorageKey = `dsh.ingestion-jobs.${session.user?.user_id || 'current'}`
let pollTimer = null
let polling = false
let disposed = false

// MVP 仅使用首个数据源，取其 data_source_id 作为后续表操作的限定范围。
const activeSourceId = computed(() => dataSources.value[0]?.data_source_id || '')

async function refresh() {
  busy.value = true
  error.value = ''
  try {
    // 文档与数据源可并行拉取；数据源就绪后再串行获取其表清单。
    const [documentPage, sourcePage] = await Promise.all([api.listDocuments(50), api.listDataSources()])
    documents.value = documentPage.items || []
    dataSources.value = sourcePage.items || []
    if (activeSourceId.value) {
      const tablePage = await api.listTables(activeSourceId.value)
      tables.value = tablePage.items || []
    }
  } catch (failure) {
    error.value = errorMessage(failure, '数据加载失败。')
  } finally {
    busy.value = false
  }
}

async function upload(event) {
  const file = event.target.files?.[0]
  if (!file) return
  uploading.value = true
  error.value = ''
  notice.value = ''
  try {
    const result = await api.uploadDocument(file)
    notice.value = result.duplicate
      ? `文档已存在，任务状态：${statusLabel(result.status)}。`
      : '上传已接受，等待建立索引。'
    await refresh()
    track(result.ingestion_job_id)
  } catch (failure) {
    error.value = errorMessage(failure, '上传失败，请检查文件后重试。')
  } finally {
    uploading.value = false
    event.target.value = ''
  }
}

// 以 2s 间隔轮询 Ingestion Job，直到 success/failed 再刷新列表并停止轮询。
function track(jobId) {
  if (!jobId) return
  if (!jobs.value.some((job) => job.ingestion_job_id === jobId)) {
    jobs.value.unshift({ ingestion_job_id: jobId, status: 'pending' })
    jobs.value = jobs.value.slice(0, 20)
  }
  localStorage.setItem(jobStorageKey, JSON.stringify(jobs.value.map((job) => job.ingestion_job_id)))
  pollJobs()
}

async function pollJobs() {
  if (polling || disposed) return
  polling = true
  clearTimeout(pollTimer)
  let finished = false
  for (const item of jobs.value.filter((job) => ['pending', 'running'].includes(job.status))) {
    try {
      const job = await api.getIngestionJob(item.ingestion_job_id)
      Object.assign(item, job)
      finished ||= job.status === 'success' || job.status === 'failed'
    } catch {
      item.status = 'unavailable'
      error.value = '任务状态暂不可用，请刷新后重试。'
    }
  }
  if (finished) await refresh()
  polling = false
  if (!disposed && jobs.value.some((job) => ['pending', 'running'].includes(job.status))) {
    pollTimer = setTimeout(pollJobs, 2000)
  }
}

async function retryJob(jobId) {
  if (retrying.value) return
  retrying.value = jobId
  error.value = ''
  try {
    const result = await api.retryIngestionJob(jobId)
    notice.value = '重试任务已接受。'
    track(result.ingestion_job_id)
    await refresh()
  } catch (failure) {
    error.value = errorMessage(failure, '任务重试失败，请刷新后重试。')
  } finally {
    retrying.value = ''
  }
}

async function removeDocument(documentId) {
  error.value = ''
  try {
    await api.deleteDocument(documentId)
    notice.value = '文档已移出检索范围，后台正在清理索引。'
    await refresh()
  } catch (failure) {
    error.value = errorMessage(failure, '删除失败。')
  }
}

async function reindex(documentId) {
  error.value = ''
  try {
    const result = await api.reindexDocument(documentId)
    notice.value = '重建索引任务已接受。'
    await refresh()
    track(result.ingestion_job_id)
  } catch (failure) {
    error.value =
      // 409 表示该文档已有进行中的 Ingestion Job，给出明确提示而非通用错误。
      failure instanceof ApiError && failure.status === 409
        ? '该文档已有任务正在处理。'
        : errorMessage(failure, '重建索引失败。')
  }
}

async function openTable(tableName) {
  error.value = ''
  try {
    activeTable.value = await api.getTable(activeSourceId.value, tableName)
    // 预览只取前 20 行；Data 预览有上限且不接受任意 SQL。
    preview.value = await api.previewTable(activeSourceId.value, tableName, 20)
  } catch (failure) {
    error.value = errorMessage(failure, '数据表读取失败。')
  }
}

onMounted(async () => {
  await refresh()
  try {
    const ids = JSON.parse(localStorage.getItem(jobStorageKey) || '[]')
    if (Array.isArray(ids)) jobs.value = ids.filter((id) => typeof id === 'string').slice(0, 20).map((id) => ({ ingestion_job_id: id, status: 'pending' }))
  } catch { /* 无效本地缓存不会阻止数据页加载。 */ }
  if (jobs.value.length) pollJobs()
})
onUnmounted(() => { disposed = true; clearTimeout(pollTimer) })
</script>

<template>
  <div class="heading">
    <div><p class="eyebrow">数据管理</p><h1>文档与业务数据</h1></div>
    <button class="icon-button" :disabled="busy" title="刷新" aria-label="刷新" @click="refresh">
      <RefreshCw :size="20" :class="{ spinning: busy }" />
    </button>
  </div>
  <p v-if="error" class="alert" role="alert">{{ error }}</p>
  <p v-if="notice" class="notice" role="status">{{ notice }}</p>
  <section v-if="jobs.length" aria-labelledby="jobs-heading">
    <h2 id="jobs-heading" class="section-title">索引任务</h2>
    <ul class="job-list">
      <li v-for="job in jobs" :key="job.ingestion_job_id">
        <div>
          <strong>{{ statusLabel(job.status) }}</strong>
          <span v-if="job.attempt" class="muted"> · 第 {{ job.attempt }} 次处理</span>
          <p v-if="job.failure_code" class="muted">{{ errorMessage({ code: job.failure_code }, '任务处理失败，可重试。') }}</p>
          <p class="muted mono">{{ job.ingestion_job_id }}</p>
        </div>
        <button v-if="job.status === 'failed'" :disabled="!!retrying" @click="retryJob(job.ingestion_job_id)">
          <RefreshCw :size="16" aria-hidden="true" /> {{ retrying === job.ingestion_job_id ? '重试中' : '重试' }}
        </button>
      </li>
    </ul>
  </section>

  <section aria-labelledby="documents-heading">
    <div class="section-head">
      <h2 id="documents-heading" class="section-title">知识文档</h2>
      <label class="upload" tabindex="0" role="button" aria-label="上传文档" @keydown.enter.prevent="$refs.uploadInput.click()" @keydown.space.prevent="$refs.uploadInput.click()">
        <FileUp :size="18" aria-hidden="true" />
        {{ uploading ? '上传中' : '上传文档' }}
        <input ref="uploadInput" type="file" :disabled="uploading" @change="upload" />
      </label>
    </div>
    <p v-if="!documents.length" class="muted">
      暂无文档。
    </p>
    <div v-else class="table-scroll" tabindex="0" role="region" aria-label="知识文档列表">
    <table class="data-table document-table">
      <thead>
        <tr><th>标题</th><th>状态</th><th>当前索引版本</th><th>更新时间</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="document in documents" :key="document.document_id">
          <td class="document-name">{{ document.title || document.source_name }}</td>
          <td>{{ statusLabel(document.status) }}</td>
          <td class="mono">{{ document.active_index_version || '—' }}</td>
          <td class="muted">{{ document.updated_at }}</td>
          <td class="row-actions">
            <button class="icon-button" title="重建索引" aria-label="重建索引" :disabled="document.status !== 'available'" @click="reindex(document.document_id)">
              <RefreshCw :size="16" />
            </button>
            <button class="icon-button" title="删除" aria-label="删除" :disabled="document.status === 'deleted'" @click="removeDocument(document.document_id)">
              <Trash2 :size="16" />
            </button>
          </td>
        </tr>
      </tbody>
    </table>
    </div>
  </section>

  <section aria-labelledby="structured-heading">
    <h2 id="structured-heading" class="section-title">业务数据</h2>
    <div class="split">
      <ul class="plain-list">
        <li v-for="table in tables" :key="table.table_name">
          <Table2 :size="18" aria-hidden="true" />
          <button class="link-button" @click="openTable(table.table_name)">{{ table.table_name }}</button>
          <span class="muted">{{ table.column_count }} 列</span>
        </li>
      </ul>
      <div v-if="activeTable">
        <h3>{{ activeTable.table_name }}</h3>
        <ul class="column-list">
          <li v-for="column in activeTable.columns" :key="column.name">
            <span class="mono">{{ column.name }}</span>
            <span class="muted">{{ column.type }}{{ column.masked ? ' · 已脱敏' : '' }}</span>
          </li>
        </ul>
      </div>
    </div>
    <div v-if="preview">
      <h3 class="section-title">数据预览（{{ preview.row_count }} 行{{ preview.truncated ? '，结果已截断' : '' }}）</h3>
      <div class="preview-scroll" tabindex="0" role="region" aria-label="数据预览">
        <table class="data-table">
          <thead><tr><th v-for="column in preview.columns" :key="column">{{ column }}</th></tr></thead>
          <tbody>
            <tr v-for="(row, index) in preview.rows" :key="index">
              <td v-for="(value, cell) in row" :key="cell">{{ value }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <p v-if="dataSources.length" class="muted">
      <Database :size="14" aria-hidden="true" /> {{ dataSources[0].data_source_id }} ·
      数据结构版本 {{ dataSources[0].schema_version }}
    </p>
  </section>
</template>
