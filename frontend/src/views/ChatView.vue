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
import { RunStream } from '../sse'

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
let stream = null

// SSE 事件类型到界面标签的映射，未登记的类型回退为原始 event_type。
const eventLabel = {
  agent_start: 'Run started',
  intent: 'Intent resolved',
  retrieval: 'Knowledge retrieval',
  tool_call: 'Tool call',
  tool_result: 'Tool result',
  citation: 'Citation',
  approval_required: 'Approval required',
  final: 'Final response',
  error: 'Error',
}

const canSend = computed(() => input.value.trim().length > 0 && !busy.value)

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
  error.value = ''
  const page = await api.conversationMessages(conversationId, 50)
  // 接口按时间倒序返回，这里反转成从旧到新以便自然展示。
  messages.value = (page.items || [])
    .slice()
    .reverse()
    .map((item) => ({
      id: item.message_id,
      role: item.role,
      content: item.content,
      citations: item.citations || [],
      status: item.status,
    }))
  await scrollToEnd()
  router.replace({ name: 'chat', query: { conversation: conversationId } })
}

async function newConversation() {
  const created = await api.createConversation('New conversation')
  conversations.value.unshift(created)
  await selectConversation(created.conversation_id)
}

async function send() {
  if (!canSend.value) return
  const text = input.value.trim()
  input.value = ''
  error.value = ''
  try {
    if (!activeConversationId.value) await newConversation()
    messages.value.push({ id: `local-${Date.now()}`, role: 'user', content: text, citations: [] })
    await scrollToEnd()
    busy.value = true
    events.value = []
    pendingApproval.value = null
    const accepted = await api.createRun(activeConversationId.value, text, threadId.value)
    activeRunId.value = accepted.run_id
    threadId.value = accepted.thread_id
    listen(accepted.run_id)
  } catch (failure) {
    busy.value = false
    // 409 表示同一 Thread 仍有运行中的请求，提示等待后再重试。
    error.value =
      failure instanceof ApiError && failure.status === 409
        ? 'This thread still has a running request. Wait for it to finish, then retry.'
        : failure.message || 'The request could not be started.'
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
      error.value = failure.message || 'The event stream stopped.'
    },
  })
  stream.start()
}

function handleEvent(event) {
  events.value.push({
    id: event.event_id,
    type: event.event_type,
    sequence: event.sequence,
    label: eventLabel[event.event_type] || event.event_type,
    detail: describe(event),
  })
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
    messages.value.push({
      id: `final-${event.event_id}`,
      role: 'assistant',
      content: event.payload.content || '',
      citations: [],
      status: event.payload.status,
    })
    busy.value = false
    loadConversations().catch(() => {})
    scrollToEnd()
  }
  if (event.event_type === 'error') {
    error.value = event.payload.message || 'The request failed.'
    busy.value = false
  }
}

function describe(event) {
  const payload = event.payload || {}
  switch (event.event_type) {
    case 'intent':
      return `${payload.intent || 'n/a'} · ${payload.routing_outcome || 'n/a'}`
    case 'retrieval':
      return `${payload.result_count ?? 0} results for “${payload.query || ''}”`
    case 'tool_call':
      return payload.tool_id
    case 'tool_result':
      return `${payload.tool_id} · ${payload.status}${payload.row_count != null ? ` · ${payload.row_count} rows` : ''}`
    case 'citation':
      return payload.label || payload.source_id
    case 'approval_required':
      return `Rows: ${payload.summary?.row_count ?? 'n/a'} · columns: ${(payload.summary?.columns || []).join(', ')}`
    case 'final':
      return payload.status
    case 'error':
      return `${payload.code} · ${payload.message}`
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
      content: 'The export request was rejected. No export was created.',
      citations: [],
      status: 'rejected',
    })
    busy.value = false
  } catch (failure) {
    busy.value = false
    error.value = failure.message || 'The approval decision could not be applied.'
  }
}

async function scrollToEnd() {
  await nextTick()
  transcriptEnd.value?.scrollIntoView({ block: 'end' })
}

onMounted(async () => {
  if (!session.authenticated) return
  try {
    await loadConversations()
    const requested = typeof route.query.conversation === 'string' ? route.query.conversation : ''
    const initial = requested || activeConversationId.value
    if (initial) await selectConversation(initial)
  } catch (failure) {
    error.value = failure.message || 'Conversations could not be loaded.'
  }
})

onUnmounted(() => stream?.close())
</script>

<template>
  <div class="chat-layout">
    <aside class="panel conversation-panel">
      <div class="panel-heading">
        <h2>Conversations</h2>
        <button class="icon-button" title="New conversation" aria-label="New conversation" @click="newConversation">
          <Plus :size="18" />
        </button>
      </div>
      <ul class="conversation-list">
        <li v-for="item in conversations" :key="item.conversation_id">
          <button
            class="conversation-item"
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
          Ask about enterprise documents, business data, or general questions. Mixed
          Knowledge and Structured Data requests are reported as unsupported.
        </p>
        <article v-for="message in messages" :key="message.id" class="message" :class="message.role">
          <header>
            <FileText v-if="message.role === 'user'" :size="16" aria-hidden="true" />
            <Wrench v-else :size="16" aria-hidden="true" />
            <span>{{ message.role === 'user' ? 'You' : 'Agent' }}</span>
            <span v-if="message.status" class="muted">{{ message.status }}</span>
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

      <div v-if="pendingApproval" class="approval" role="alertdialog" aria-label="Export approval">
        <ShieldQuestion :size="22" aria-hidden="true" />
        <div>
          <h3>Export approval required</h3>
          <p class="muted">
            {{ pendingApproval.summary.row_count ?? 'unknown' }} rows ·
            {{ (pendingApproval.summary.columns || []).join(', ') || 'no columns' }} · csv
          </p>
          <p class="muted">No export is created until you approve this request.</p>
        </div>
        <div class="approval-actions">
          <button class="primary" :disabled="busy" @click="decide('approve')">
            <CheckCircle2 :size="16" /> Approve
          </button>
          <button :disabled="busy" @click="decide('reject')">
            <XCircle :size="16" /> Reject
          </button>
        </div>
      </div>

      <p v-if="error" class="alert" role="alert">{{ error }}</p>

      <form class="composer" @submit.prevent="send">
        <textarea
          v-model="input"
          rows="2"
          placeholder="Ask a question…"
          :disabled="busy"
          @keydown.enter.exact.prevent="send"
        ></textarea>
        <button class="primary" type="submit" :disabled="!canSend">
          <Send :size="18" aria-hidden="true" />
          Send
        </button>
      </form>
    </section>

    <aside class="panel event-panel">
      <div class="panel-heading"><h2>Run activity</h2></div>
      <p v-if="!events.length" class="muted">No events for this run yet.</p>
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
