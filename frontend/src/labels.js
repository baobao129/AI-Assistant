export const errorMessages = {
  authentication_error: '登录已失效或账号密码不正确，请重新登录。',
  authorization_error: '当前账号无法访问该资源。',
  not_found: '资源不可用，请刷新后重试。',
  validation_error: '提交的内容不符合要求，请检查后重试。',
  conflict: '资源状态已变化，请刷新后重试。',
  document_scope_unavailable: '所选文档已不可用，请刷新文档列表后重新选择。',
  knowledge_index_incompatible: '文档索引与当前向量模型不兼容，请使用当前配置重建索引后重试。',
  retrieval_unavailable: '知识检索暂时不可用，本次请求已停止，请稍后重试。',
  llm_unavailable: '模型服务暂时不可用，请稍后重试。',
  rate_limit: '请求过于频繁，请稍后重试。',
  timeout: '请求超时，请稍后重试。',
  unsupported: '当前操作暂不支持。',
  internal_error: '请求未能完成，请稍后重试。',
  parse_timeout: '文档解析超时，可重试并继续已有解析任务。',
  insufficient_evidence: '授权文档中的证据不足，无法回答该问题。',
  invalid_request: '问题内容无效，请补充问题后重试。',
  routing_error: '暂时无法确定问题来源，请明确需要查询的内容。',
  unsupported_mixed_query: '暂不支持同时查询知识文档与业务数据，请拆分问题。',
  validation_failed: '答复未通过验证，无法提供可靠答案。',
  sql_rejected: '该数据查询不符合安全要求，已停止执行。',
  tool_budget_exceeded: '本次查询已达到处理上限，请缩小问题范围。',
  repair_budget_exceeded: '本次查询未能完成，请调整问题后重试。',
  approval_expired: '审批已过期，请重新发起请求。',
  parse_failed: '文档解析失败，请检查文件后重试。',
  embedding_failed: '文档向量生成失败，请稍后重试。',
  vector_write_failed: '索引写入失败，请稍后重试。',
  unavailable: '当前资源暂不可用。',
}

export function errorMessage(error, fallback = '请求未能完成，请稍后重试。') {
  return errorMessages[error?.code || error?.error_code] || fallback
}

export function answerContent(payload) {
  if (!payload.error_code) return payload.content || ''
  // 已本地化的服务端安全答复与持久化历史使用相同正文。
  if (typeof payload.content === 'string' && /[\u4e00-\u9fff]/u.test(payload.content)) return payload.content
  return errorMessage(payload)
}

const tools = {
  knowledge_search: '检索知识文档', database_query: '查询业务数据',
  schema_inspect: '查看数据结构', database_schema: '查看数据结构',
  schema_lookup: '查看数据结构', data_export: '导出数据',
}

export function toolLabel(value) {
  return tools[value] || '处理工具'
}

const statuses = {
  available: '可用', indexing: '索引中', failed: '失败', deleted: '已删除',
  pending: '等待处理', running: '处理中', success: '成功', accepted: '已接受',
  awaiting_approval: '等待审批', completed: '已完成', rejected: '已拒绝',
  expired: '已过期', handoff_complete: '已移交', cancelled: '已取消',
  committed: '已保存',
  knowledge: '知识问答', structured_data: '业务数据', general: '通用对话',
  routed: '已路由', unsupported_mixed_query: '不支持混合来源',
  invalid_request: '请求无效', routing_error: '路由失败',
  insufficient_evidence: '证据不足', validated: '验证通过', passed: '验证通过',
  'insufficient-evidence': '证据不足', invalid: '验证未通过',
  invalid_result: '验证未通过', retryable_validation_failure: '验证未通过',
  validation_error: '验证失败',
  safe_failure: '安全停止', unsupported: '暂不支持', draft: '生成中',
  unavailable: '不可用',
}

export function statusLabel(value) {
  return statuses[value] || '未知状态'
}
