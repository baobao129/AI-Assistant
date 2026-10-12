/**
 * 文档作用域的纯前端逻辑（ADR-039 / CC-API-002）。
 *
 * 这里只做「展示与选择」相关判定：真正的授权校验在服务端（建 Run 时与检索边界各一次），
 * 前端永远不决定谁有权限。把判定抽成纯函数是为了能直接用 node --test 覆盖。
 */

// 与后端 `MAX_DOCUMENT_SCOPE_IDS` 对齐：超出会被 422 拒绝，所以前端先拦住。
export const MAX_SCOPE_DOCUMENTS = 20

// 后端在建 Run 与检索边界使用的统一冲突码（409）。
export const SCOPE_UNAVAILABLE_CODE = 'document_scope_unavailable'

/**
 * 文档在作用域里是否可用。
 * - `ready`：available 且有 active 版本，可以选；
 * - `indexing`：已接受但还没有 active 版本（首次索引中），选了也检索不到；
 * - `failed`：摄取失败且没有可用版本（ADR-040 / CC-API-003），需要重试或删除；
 * - `unavailable`：其它非可用状态（删除中/已删除）。
 */
export function documentAvailability(document) {
  if (!document) return 'unavailable'
  if (document.status === 'indexing') return 'indexing'
  if (document.status === 'failed') return 'failed'
  if (document.status !== 'available') return 'unavailable'
  // 历史行：available 但没有 active 版本（早于 ADR-040 的数据），等同「尚未索引完成」。
  if (!document.active_index_version) return 'indexing'
  return 'ready'
}

/** 不可选时给用户的原因说明。 */
export function availabilityReason(availability) {
  if (availability === 'indexing') return '索引中'
  if (availability === 'failed') return '索引失败'
  if (availability === 'unavailable') return '不可用'
  return ''
}

/** 作用域摘要：未选择时明确表示「全部文档」，避免用户以为被限制了。 */
export function scopeSummary(selectedCount) {
  if (!selectedCount) return '全部授权文档'
  return `已选 ${selectedCount} 份文档`
}

/** 是否还能勾选更多文档（上限由后端与前端共用同一个数）。 */
export function canSelectMore(selected, documentId) {
  if (selected.includes(documentId)) return true
  return selected.length < MAX_SCOPE_DOCUMENTS
}

/**
 * 把建 Run 的请求体拼出来。
 * 未选择任何文档时**不发送** `document_ids`，与「缺省字段」等价，保持既有行为。
 */
export function runRequestBody(userInput, threadId, documentIds = []) {
  const body = { user_input: userInput, thread_id: threadId ?? null, client_metadata: {} }
  if (documentIds.length) body.document_ids = [...documentIds]
  return body
}

/**
 * 把建 Run 失败映射为界面提示。
 * 关键点：`document_scope_unavailable` 必须与「同 Thread 有运行中请求」的 409 区分开，
 * 否则用户会读到完全错误的解释。
 */
export function startFailureMessage(error) {
  if (!error) return '请求未能开始。'
  if (error.code === SCOPE_UNAVAILABLE_CODE) {
    return (
      '所选文档已不可用，请刷新列表后重新选择，本次问题尚未发送。'
    )
  }
  if (error.status === 409) {
    return '当前对话仍有请求在运行，请等待完成后重试。'
  }
  if (error.status === 422 && Array.isArray(error.details?.document_ids)) {
    return '文档选择无效，最多可选 20 份文档。'
  }
  return error.message || '请求未能开始。'
}
