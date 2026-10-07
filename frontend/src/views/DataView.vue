<script setup>
import { computed, onMounted, ref } from 'vue'
import { Database, FileUp, RefreshCw, Table2, Trash2 } from '@lucide/vue'
import { ApiError, api } from '../api'

const documents = ref([])
const dataSources = ref([])
const tables = ref([])
const activeTable = ref(null)
const preview = ref(null)
const error = ref('')
const notice = ref('')
const busy = ref(false)
const uploading = ref(false)
let pollTimer = null

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
    error.value = failure.message || 'Data could not be loaded.'
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
      ? `This document is already being ingested (job ${result.ingestion_job_id}).`
      : `Upload accepted. Ingestion job ${result.ingestion_job_id} is pending.`
    await refresh()
    track(result.ingestion_job_id)
  } catch (failure) {
    error.value = failure.message || 'The upload was rejected.'
  } finally {
    uploading.value = false
    event.target.value = ''
  }
}

// 以 2s 间隔轮询 Ingestion Job，直到 success/failed 再刷新列表并停止轮询。
function track(jobId) {
  clearInterval(pollTimer)
  pollTimer = setInterval(async () => {
    try {
      const job = await api.getIngestionJob(jobId)
      notice.value = `Ingestion ${job.ingestion_job_id}: ${job.status}${job.failure_code ? ` (${job.failure_code})` : ''}`
      if (job.status === 'success' || job.status === 'failed') {
        clearInterval(pollTimer)
        await refresh()
      }
    } catch {
      clearInterval(pollTimer)
    }
  }, 2000)
}

async function removeDocument(documentId) {
  error.value = ''
  try {
    await api.deleteDocument(documentId)
    notice.value = 'Deletion scheduled; vectors and index records are cleaned in the background.'
    await refresh()
  } catch (failure) {
    error.value = failure.message || 'The document could not be deleted.'
  }
}

async function reindex(documentId) {
  error.value = ''
  try {
    const result = await api.reindexDocument(documentId)
    notice.value = `Re-index scheduled as index version ${result.index_version}.`
    await refresh()
    track(result.ingestion_job_id)
  } catch (failure) {
    error.value =
      // 409 表示该文档已有进行中的 Ingestion Job，给出明确提示而非通用错误。
      failure instanceof ApiError && failure.status === 409
        ? 'A job is already active for this document.'
        : failure.message || 'Re-index could not be scheduled.'
  }
}

async function openTable(tableName) {
  error.value = ''
  try {
    activeTable.value = await api.getTable(activeSourceId.value, tableName)
    // 预览只取前 20 行；Data 预览有上限且不接受任意 SQL。
    preview.value = await api.previewTable(activeSourceId.value, tableName, 20)
  } catch (failure) {
    error.value = failure.message || 'The table could not be inspected.'
  }
}

onMounted(refresh)
</script>

<template>
  <div class="heading">
    <div><p class="eyebrow">Data management</p><h1>Documents and structured data</h1></div>
    <button class="icon-button" :disabled="busy" title="Refresh" aria-label="Refresh" @click="refresh">
      <RefreshCw :size="20" :class="{ spinning: busy }" />
    </button>
  </div>
  <p v-if="error" class="alert" role="alert">{{ error }}</p>
  <p v-if="notice" class="notice">{{ notice }}</p>

  <section aria-labelledby="documents-heading">
    <div class="section-head">
      <h2 id="documents-heading" class="section-title">Knowledge documents</h2>
      <label class="upload">
        <FileUp :size="18" aria-hidden="true" />
        {{ uploading ? 'Uploading…' : 'Upload document' }}
        <input type="file" :disabled="uploading" @change="upload" />
      </label>
    </div>
    <p v-if="!documents.length" class="muted">
      No documents yet. Upload a text, Markdown, CSV, or JSON file to index it.
    </p>
    <table v-else class="data-table">
      <thead>
        <tr><th>Title</th><th>Status</th><th>Active index version</th><th>Updated</th><th></th></tr>
      </thead>
      <tbody>
        <tr v-for="document in documents" :key="document.document_id">
          <td>{{ document.title }}</td>
          <td>{{ document.status }}</td>
          <td class="mono">{{ document.active_index_version || '—' }}</td>
          <td class="muted">{{ document.updated_at }}</td>
          <td class="row-actions">
            <button class="icon-button" title="Re-index" aria-label="Re-index" @click="reindex(document.document_id)">
              <RefreshCw :size="16" />
            </button>
            <button class="icon-button" title="Delete" aria-label="Delete" @click="removeDocument(document.document_id)">
              <Trash2 :size="16" />
            </button>
          </td>
        </tr>
      </tbody>
    </table>
  </section>

  <section aria-labelledby="structured-heading">
    <h2 id="structured-heading" class="section-title">Structured data</h2>
    <p class="muted">
      Authorized schemas and bounded previews only. This page cannot run SQL; the SQL Agent
      executes inside the Graph through the SQL Validator and the read-only account.
    </p>
    <div class="split">
      <ul class="plain-list">
        <li v-for="table in tables" :key="table.table_name">
          <Table2 :size="18" aria-hidden="true" />
          <button class="link-button" @click="openTable(table.table_name)">{{ table.table_name }}</button>
          <span class="muted">{{ table.column_count }} columns</span>
        </li>
      </ul>
      <div v-if="activeTable">
        <h3>{{ activeTable.table_name }}</h3>
        <ul class="column-list">
          <li v-for="column in activeTable.columns" :key="column.name">
            <span class="mono">{{ column.name }}</span>
            <span class="muted">{{ column.type }}{{ column.masked ? ' · masked' : '' }}</span>
          </li>
        </ul>
      </div>
    </div>
    <div v-if="preview">
      <h3 class="section-title">Preview ({{ preview.row_count }} rows{{ preview.truncated ? ', truncated' : '' }})</h3>
      <div class="preview-scroll">
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
      schema {{ dataSources[0].schema_version }}
    </p>
  </section>
</template>
