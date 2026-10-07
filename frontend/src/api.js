/**
 * API 客户端：覆盖会话、Run、Approval、Document 与 Data Source 等端点。
 * 浏览器绝不直接调用 Tool，也绝不发送原始 SQL。
 */

// 会话令牌与用户信息持久化在 localStorage 中，刷新页面后仍可恢复登录态。
const TOKEN_KEY = 'dsh.session.token'
const USER_KEY = 'dsh.session.user'

export class ApiError extends Error {
  constructor(status, code, message, requestId) {
    super(message)
    this.status = status
    this.code = code
    this.requestId = requestId
  }
}

export const session = {
  token: localStorage.getItem(TOKEN_KEY) || '',
  user: JSON.parse(localStorage.getItem(USER_KEY) || 'null'),
  save(token, user) {
    this.token = token
    this.user = user
    localStorage.setItem(TOKEN_KEY, token)
    localStorage.setItem(USER_KEY, JSON.stringify(user))
  },
  clear() {
    this.token = ''
    this.user = null
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  },
  get authenticated() {
    return Boolean(this.token)
  },
}

// 已登录时统一附加 Authorization: Bearer 头；未登录则原样透传调用方提供的额外头。
export function authHeaders(extra = {}) {
  return session.token ? { ...extra, Authorization: `Bearer ${session.token}` } : { ...extra }
}

// 宽容解析响应体：204 或空体返回 null，非 JSON 文本按原文返回，避免解析报错。
async function parse(response) {
  if (response.status === 204) return null
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

export async function request(path, { method = 'GET', body, form, signal } = {}) {
  const headers = authHeaders()
  let payload
  if (form) {
    payload = form
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }
  const response = await fetch(path, { method, headers, body: payload, signal })
  const data = await parse(response)
  if (!response.ok) {
    // 统一把后端错误信封映射为 ApiError，并带上 X-Request-ID 便于排查。
    const error = data && data.error ? data.error : {}
    throw new ApiError(
      response.status,
      error.code || 'internal_error',
      error.message || 'The request could not be completed.',
      response.headers.get('X-Request-ID') || error.request_id,
    )
  }
  return data
}

export const api = {
  login: (username, password) =>
    request('/api/v1/auth/login', { method: 'POST', body: { username, password } }),
  logout: () => request('/api/v1/auth/logout', { method: 'POST' }),
  me: () => request('/api/v1/auth/me'),
  health: () => request('/api/v1/health'),

  listConversations: (limit = 20) => request(`/api/v1/conversations?limit=${limit}`),
  createConversation: (title) =>
    request('/api/v1/conversations', { method: 'POST', body: { title } }),
  getConversation: (id) => request(`/api/v1/conversations/${id}`),
  conversationMessages: (id, limit = 50) =>
    request(`/api/v1/conversations/${id}/messages?limit=${limit}`),
  deleteConversation: (id) => request(`/api/v1/conversations/${id}`, { method: 'DELETE' }),

  createRun: (conversationId, userInput, threadId = null) =>
    request(`/api/v1/conversations/${conversationId}/runs`, {
      method: 'POST',
      body: { user_input: userInput, thread_id: threadId, client_metadata: {} },
    }),
  getRun: (runId) => request(`/api/v1/runs/${runId}`),

  approve: (approvalRequestId, correlation) =>
    request(`/api/v1/approvals/${approvalRequestId}/approve`, {
      method: 'POST',
      body: correlation,
    }),
  reject: (approvalRequestId, correlation) =>
    request(`/api/v1/approvals/${approvalRequestId}/reject`, {
      method: 'POST',
      body: correlation,
    }),

  listDocuments: (limit = 20) => request(`/api/v1/documents?limit=${limit}`),
  uploadDocument: (file) => {
    const form = new FormData()
    form.append('file', file)
    return request('/api/v1/documents', { method: 'POST', form })
  },
  deleteDocument: (id) => request(`/api/v1/documents/${id}`, { method: 'DELETE' }),
  reindexDocument: (id) => request(`/api/v1/documents/${id}/reindex`, { method: 'POST' }),
  getIngestionJob: (id) => request(`/api/v1/ingestion-jobs/${id}`),
  retryIngestionJob: (id) =>
    request(`/api/v1/ingestion-jobs/${id}/retry`, { method: 'POST' }),

  listDataSources: () => request('/api/v1/data-sources'),
  listTables: (sourceId) => request(`/api/v1/data-sources/${sourceId}/tables`),
  getTable: (sourceId, table) =>
    request(`/api/v1/data-sources/${sourceId}/tables/${table}`),
  previewTable: (sourceId, table, limit = 50) =>
    request(`/api/v1/data-sources/${sourceId}/tables/${table}/preview?limit=${limit}`),
}
