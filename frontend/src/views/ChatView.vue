<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  CheckCircle2,
  ChevronRight,
  FileText,
  Plus,
  Send,
  ShieldQuestion,
  Wrench,
  XCircle,
} from '@lucide/vue'
import { ApiError, api, session } from '../api'
import {
  MAX_SCOPE_DOCUMENTS,
  availabilityReason,
  canSelectMore,
  documentAvailability,
  scopeSummary,
  startFailureMessage,
} from '../scope'
import { RunStream } from '../sse'
import { answerContent, errorMessage, statusLabel, toolLabel } from '../labels'

const route = useRoute()
const router = useRouter()

const conversations = ref([])
const activeConversationId = ref('')
const messages = ref([])
const pendingApproval = ref(null)
const input = ref('')
const events = ref([])
const error = ref('')
const busy = ref(false)
const activeRunId = ref('')
const threadId = ref(null)
const transcriptEnd = ref(null)
const draftMessage = ref(null)
// 文档作用域（ADR-039）：用户显式选择「只在哪些文档里找答案」。
// 未选择 = 不发送 document_ids = 与服务端既有行为完全一致。
const documents = ref([])
const selectedDocumentIds = ref([])
const scopeOpen = ref(false)
let stream = null

// SSE 事件类型到界面标签的映射，未登记的类型回退为原始 event_type。
const eventLabel = {
  agent_start: '开始处理',
  intent: '确定问题类型',
  retrieval: '检索知识',
  tool_call: '调用工具',
  tool_result: '工具结果',
  citation: '来源引用',
  approval_required: '等待审批',
  final: '最终答复',
  error: '处理失败',
  token: '正在生成',
}

const canSend = computed(() => input.value.trim().length > 0 && !busy.value && !pendingApproval.value)
const scopeLabel = computed(() => scopeSummary(selectedDocumentIds.value.length))
const readyDocumentCount = computed(
  () => documents.value.filter((doc) => documentAvailability(doc) === 'ready').length,
)

function toMessage(item) {
  return {
    id: item.message_id,
    role: item.role,
    content: item.content,
    citations: item.citations || [],
    status: item.status,
  }
}

async function loadDocuments() {
  const page = await api.listDocuments(100)
  documents.value = page.items || []
  // 选择里的文档可能已经不可用（删除/重建）：主动剔除，避免下一次请求必然被拒。
  const selectable = new Set(
    documents.value
      .filter((doc) => documentAvailability(doc) === 'ready')
      .map((doc) => doc.document_id),
  )
  selectedDocumentIds.value = selectedDocumentIds.value.filter((id) => selectable.has(id))
}

function clearScope() {
  selectedDocumentIds.value = []
}

async function loadConversations() {
  const page = await api.listConversations(50)
  conversations.value = page.items || []
  if (!activeConversationId.value && conversations.value.length) {
    activeConversationId.value = conversations.value[0].conversation_id
  }
}

async function selectConversation(conversationId) {
  activeConversationId.value = conversationId
  activeRunId.value = ''
  threadId.value = null
  pendingApproval.value = null
  events.value = []
  draftMessage.value = null
  error.value = ''
  const page = await api.conversationMessages(conversationId, 50)
  // 接口按时间倒序返回，这里反转成从旧到新以便自然展示。
  messages.value = (page.items || []).slice().reverse().map(toMessage)
  await scrollToEnd()
  router.replace({ name: 'chat', query: { conversation: conversationId } })
}

// 终态后以持久化消息为准，保持引用和历史一致。
async function refreshPersistedMessages() {
  if (!activeConversationId.value) return
  try {
    const page = await api.conversationMessages(activeConversationId.value, 50)
    const items = (page.items || []).slice().reverse().map(toMessage)
    if (items.some((item) => item.role === 'assistant')) {
      messages.value = items
      await scrollToEnd()
    }
  } catch {
    // 拉取失败时保留实时消息，不打断用户。
  }
}

async function newConversation() {
  const created = await api.createConversation('新对话')
  conversations.value.unshift(created)
  await selectConversation(created.conversation_id)
}

async function send() {
  if (!canSend.value) return
  busy.value = true
  const text = input.value.trim()
  input.value = ''
  error.value = ''
  try {
    if (!activeConversationId.value) await newConversation()
    messages.value.push({ id: `local-${Date.now()}`, role: 'user', content: text, citations: [] })
    await scrollToEnd()
    events.value = []
    draftMessage.value = null
    pendingApproval.value = null
    const accepted = await api.createRun(
      activeConversationId.value,
      text,
      threadId.value,
      selectedDocumentIds.value,
    )
    activeRunId.value = accepted.run_id
    threadId.value = accepted.thread_id
    listen(accepted.run_id)
  } catch (failure) {
    busy.value = false
    // 作用域冲突（409 document_scope_unavailable）与「同 Thread 有运行中请求」是两种
    // 完全不同的 409，必须分别解释（见 scope.js 的单测）。
    error.value = startFailureMessage(failure)
    if (failure instanceof ApiError && failure.code === 'document_scope_unavailable') {
      // 选择里已有文档不可用：刷新列表让失效项显式可见。
      await loadDocuments().catch(() => {})
    }
  }
}

// 订阅指定 Run 的 SSE 事件流；切换 Run 时先关闭上一条流，避免重复副作用。
function listen(runId) {
  stream?.close()
  stream = new RunStream(runId, {
    onEvent: handleEvent,
    onClose: () => {
      busy.value = false
    },
    onError: (failure) => {
      busy.value = false
      error.value = errorMessage(failure, '连接已中断，请刷新对话查看结果。')
    },
  })
  stream.start()
}

function handleEvent(event) {
  if (event.event_type === 'token') {
    if (event.payload?.reset && draftMessage.value) draftMessage.value.content = ''
    const delta = event.payload?.delta
    if (typeof delta !== 'string' || !delta) return
    if (!draftMessage.value) {
      draftMessage.value = { id: `draft-${activeRunId.value}`, role: 'assistant', content: '', citations: [], status: 'draft' }
      messages.value.push(draftMessage.value)
    }
    draftMessage.value.content += delta
    scrollToEnd()
    return
  }
  events.value.push({
    id: event.event_id,
    type: event.event_type,
    sequence: event.sequence,
    label: eventLabel[event.event_type] || '处理进度',
    detail: describe(event),
  })
  if (events.value.length > 100) events.value.shift()
  if (event.event_type === 'approval_required') {
    // 记录 correlation 三要素（conversation/thread/run），Approve/Reject 时原样回传。
    pendingApproval.value = {
      approvalRequestId: event.payload.approval_request_id,
      summary: event.payload.summary || {},
      conversationId: event.payload.conversation_id,
      threadId: event.payload.thread_id,
      runId: event.payload.run_id,
    }
    busy.value = false
  }
  if (event.event_type === 'final') {
    const finalMessage = {
      id: `final-${event.event_id}`,
      role: 'assistant',
      content: answerContent(event.payload),
      citations: event.payload.citations || [],
      status: event.payload.status,
    }
    if (draftMessage.value) {
      const index = messages.value.findIndex((message) => message.id === draftMessage.value.id)
      if (index >= 0) messages.value.splice(index, 1, finalMessage)
      else messages.value.push(finalMessage)
      draftMessage.value = null
    } else messages.value.push(finalMessage)
    busy.value = false
    loadConversations().catch(() => {})
    scrollToEnd()
    // 终态后以持久化消息为准，保证实时与历史显示同一组引用。
    refreshPersistedMessages()
  }
  if (event.event_type === 'error') {
    if (event.payload?.terminal && draftMessage.value) {
      messages.value = messages.value.filter((message) => message.id !== draftMessage.value.id)
      draftMessage.value = null
    }
    error.value = errorMessage(event.payload)
    busy.value = false
  }
}

function describe(event) {
  const payload = event.payload || {}
  switch (event.event_type) {
    case 'intent':
      return `${statusLabel(payload.intent)} · ${statusLabel(payload.routing_outcome)}`
    case 'retrieval':
      return `检索到 ${payload.result_count ?? 0} 条证据`
    case 'tool_call':
      return toolLabel(payload.tool_id)
    case 'tool_result':
      return `${toolLabel(payload.tool_id)} · ${statusLabel(payload.status)}${payload.row_count != null ? ` · ${payload.row_count} 行` : ''}`
    case 'citation':
      return payload.label || payload.source_id
    case 'approval_required':
      return `行数：${payload.summary?.row_count ?? '未知'} · 列：${(payload.summary?.columns || []).join(', ')}`
    case 'final':
      return statusLabel(payload.status)
    case 'error':
      return errorMessage(payload)
    default:
      return ''
  }
}

async function decide(decision) {
  if (!pendingApproval.value) return
  const approval = pendingApproval.value
  busy.value = true
  error.value = ''
  try {
    const request =
      decision === 'approve'
        ? api.approve(approval.approvalRequestId, {
            conversation_id: approval.conversationId,
            thread_id: approval.threadId,
            run_id: approval.runId,
          })
        : api.reject(approval.approvalRequestId, {
            conversation_id: approval.conversationId,
            thread_id: approval.threadId,
            run_id: approval.runId,
          })
    const result = await request
    pendingApproval.value = null
    if (decision === 'approve' && result.successor_run_id) {
      // Approve 会移交到 successor Run，其拥有独立的 SSE sequence。
      threadId.value = approval.threadId
      activeRunId.value = result.successor_run_id
      listen(result.successor_run_id)
      return
    }
    messages.value.push({
      id: `decision-${Date.now()}`,
      role: 'assistant',
      content: '导出请求已拒绝，未创建导出文件。',
      citations: [],
      status: 'rejected',
    })
    busy.value = false
  } catch (failure) {
    busy.value = false
    error.value = errorMessage(failure, '审批操作失败。')
  }
}

async function scrollToEnd() {
  await nextTick()
  transcriptEnd.value?.scrollIntoView({ block: 'end' })
}

onMounted(async () => {
  if (!session.authenticated) return
  try {
    await Promise.all([loadConversations(), loadDocuments()])
    const requested = typeof route.query.conversation === 'string' ? route.query.conversation : ''
    const initial = requested || activeConversationId.value
    if (initial) await selectConversation(initial)
  } catch (failure) {
    error.value = errorMessage(failure, '对话加载失败。')
  }
})

onUnmounted(() => stream?.close())
</script>

<template>
  <div class="chat-layout">
    <aside class="panel conversation-panel">
      <div class="panel-heading">
        <h2>对话</h2>
        <button class="icon-button" title="新对话" aria-label="新对话" :disabled="busy || !!pendingApproval" @click="newConversation">
          <Plus :size="18" />
        </button>
      </div>
      <ul class="conversation-list">
        <li v-for="item in conversations" :key="item.conversation_id">
          <button
            class="conversation-item"
            :disabled="busy || !!pendingApproval"
            :class="{ active: item.conversation_id === activeConversationId }"
            @click="selectConversation(item.conversation_id)"
          >
            <span class="conversation-title">{{ item.title }}</span>
            <ChevronRight :size="16" aria-hidden="true" />
          </button>
        </li>
      </ul>
    </aside>

    <section class="panel chat-panel">
      <div class="transcript">
        <p v-if="!messages.length" class="muted">
          暂无消息。
        </p>
        <article v-for="message in messages" :key="message.id" class="message" :class="message.role">
          <header>
            <FileText v-if="message.role === 'user'" :size="16" aria-hidden="true" />
            <Wrench v-else :size="16" aria-hidden="true" />
            <span>{{ message.role === 'user' ? '你' : '智能助手' }}</span>
            <span v-if="message.status" class="muted">{{ statusLabel(message.status) }}</span>
          </header>
          <p class="message-body">{{ message.content }}</p>
          <ul v-if="message.citations?.length" class="citations">
            <li v-for="(citation, index) in message.citations" :key="index">
              {{ citation.label || citation.source_id }}
              <span v-if="citation.location" class="muted">· {{ citation.location }}</span>
            </li>
          </ul>
        </article>
        <div ref="transcriptEnd"></div>
      </div>

      <div v-if="pendingApproval" class="approval" role="alertdialog" aria-label="导出审批">
        <ShieldQuestion :size="22" aria-hidden="true" />
        <div>
          <h3>导出需要审批</h3>
          <p class="muted">
            {{ pendingApproval.summary.row_count ?? '未知' }} 行 ·
            {{ (pendingApproval.summary.columns || []).join(', ') || '无列' }} · CSV
          </p>
          <p class="muted">审批前不会创建导出文件。</p>
        </div>
        <div class="approval-actions">
          <button class="primary" :disabled="busy" @click="decide('approve')">
            <CheckCircle2 :size="16" /> 同意
          </button>
          <button :disabled="busy" @click="decide('reject')">
            <XCircle :size="16" /> 拒绝
          </button>
        </div>
      </div>

      <p v-if="error" class="alert" role="alert">{{ error }}</p>

      <div class="scope">
        <div class="scope-bar">
          <button
            type="button"
            class="scope-toggle"
            :aria-expanded="scopeOpen"
            @click="scopeOpen = !scopeOpen"
          >
            <FileText :size="16" aria-hidden="true" />
            <span>文档范围：{{ scopeLabel }}</span>
            <ChevronRight :size="14" class="scope-chevron" :class="{ open: scopeOpen }" aria-hidden="true" />
          </button>
          <button v-if="selectedDocumentIds.length" type="button" @click="clearScope">
            清空选择
          </button>
        </div>

        <div v-if="scopeOpen" class="scope-list" role="group" aria-label="文档范围">
          <p v-if="!documents.length" class="muted">
            暂无文档。
          </p>
          <template v-else>
            <ul>
              <li v-for="doc in documents" :key="doc.document_id">
                <label :class="{ disabled: documentAvailability(doc) !== 'ready' }">
                  <input
                    v-model="selectedDocumentIds"
                    type="checkbox"
                    :value="doc.document_id"
                    :disabled="
                      documentAvailability(doc) !== 'ready' ||
                      !canSelectMore(selectedDocumentIds, doc.document_id)
                    "
                  />
                  <span class="scope-name" :title="doc.source_name">{{ doc.source_name }}</span>
                  <span
                    v-if="documentAvailability(doc) !== 'ready'"
                    class="muted scope-reason"
                  >
                    {{ availabilityReason(documentAvailability(doc)) }}
                  </span>
                </label>
              </li>
            </ul>
            <p v-if="readyDocumentCount === 0" class="muted">
              暂无可检索文档。
            </p>
            <p v-else-if="selectedDocumentIds.length >= MAX_SCOPE_DOCUMENTS" class="muted">
              最多选择 {{ MAX_SCOPE_DOCUMENTS }} 份文档。
            </p>
          </template>
        </div>
      </div>

      <form class="composer" @submit.prevent="send">
        <textarea
          v-model="input"
          rows="2"
          placeholder="输入问题"
          aria-label="问题"
          :disabled="busy || !!pendingApproval"
          @keydown.enter.exact="event => { if (event.isComposing) return; event.preventDefault(); send() }"
        ></textarea>
        <button class="primary" type="submit" :disabled="!canSend">
          <Send :size="18" aria-hidden="true" />
          发送
        </button>
      </form>
    </section>

    <aside class="panel event-panel">
      <div class="panel-heading"><h2>处理进度</h2></div>
      <p v-if="!events.length" class="muted">暂无进度。</p>
      <ol class="event-list">
        <li v-for="event in events" :key="event.id" :class="`event-${event.type}`">
          <span class="event-sequence">{{ event.sequence }}</span>
          <div>
            <strong>{{ event.label }}</strong>
            <p v-if="event.detail" class="muted">{{ event.detail }}</p>
          </div>
        </li>
      </ol>
    </aside>
  </div>
</template>
