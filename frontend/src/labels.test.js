import test from 'node:test'
import assert from 'node:assert/strict'
import { answerContent, errorMessage, statusLabel, toolLabel } from './labels.js'

test('safe final uses error code rather than provider English content', () => {
  assert.equal(answerContent({ error_code: 'insufficient_evidence', content: 'No evidence' }), '授权文档中的证据不足，无法回答该问题。')
  assert.equal(answerContent({ error_code: 'unregistered_failure', content: 'provider trace' }), '请求未能完成，请稍后重试。')
  assert.equal(answerContent({ error_code: 'insufficient_evidence', content: '当前授权文档中没有足够证据回答这个问题。' }), '当前授权文档中没有足够证据回答这个问题。')
})

test('successful final preserves source text and unknown presentation values are safe', () => {
  assert.equal(answerContent({ content: 'API 示例 SELECT' }), 'API 示例 SELECT')
  assert.equal(toolLabel('knowledge_search'), '检索知识文档')
  assert.equal(toolLabel('hidden_tool_id'), '处理工具')
  assert.equal(statusLabel('hidden_status'), '未知状态')
  assert.equal(errorMessage({ code: 'secret' }), '请求未能完成，请稍后重试。')
})
