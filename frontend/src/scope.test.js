import assert from 'node:assert/strict'
import test from 'node:test'

import {
  MAX_SCOPE_DOCUMENTS,
  availabilityReason,
  canSelectMore,
  documentAvailability,
  runRequestBody,
  scopeSummary,
  startFailureMessage,
} from './scope.js'

function doc(overrides = {}) {
  return {
    document_id: 'doc-1',
    status: 'available',
    active_index_version: 'v1',
    ...overrides,
  }
}

test('an available document with an active version is selectable', () => {
  assert.equal(documentAvailability(doc()), 'ready')
  assert.equal(availabilityReason('ready'), '')
})

test('an available document without an active version is reported as still indexing', () => {
  assert.equal(documentAvailability(doc({ active_index_version: null })), 'indexing')
  assert.match(availabilityReason('indexing'), /索引中/)
})

test('an indexing document is reported as still indexing', () => {
  // ADR-040：新文档从 indexing 开始，promote 才进入 available。
  assert.equal(documentAvailability(doc({ status: 'indexing', active_index_version: null })), 'indexing')
  assert.match(availabilityReason('indexing'), /索引中/)
})

test('a failed document is reported as a failed indexing', () => {
  assert.equal(documentAvailability(doc({ status: 'failed', active_index_version: null })), 'failed')
  assert.match(availabilityReason('failed'), /索引失败/)
})

test('a non-available document is reported as unavailable', () => {
  assert.equal(documentAvailability(doc({ status: 'deleted' })), 'unavailable')
  assert.equal(documentAvailability(null), 'unavailable')
  assert.match(availabilityReason('unavailable'), /不可用/)
})

test('the scope summary says "all documents" when nothing is selected', () => {
  assert.equal(scopeSummary(0), '全部授权文档')
  assert.equal(scopeSummary(1), '已选 1 份文档')
  assert.equal(scopeSummary(3), '已选 3 份文档')
})

test('no document_ids is sent when nothing is selected', () => {
  const body = runRequestBody('hello', null, [])
  assert.deepEqual(Object.keys(body).sort(), ['client_metadata', 'thread_id', 'user_input'])
  assert.equal('document_ids' in body, false)
})

test('the selected documents are copied into the request body', () => {
  const body = runRequestBody('hello', 'thread-1', ['a', 'b'])
  assert.deepEqual(body.document_ids, ['a', 'b'])
  assert.equal(body.thread_id, 'thread-1')
})

test('selection stops at the shared limit but an already selected id stays removable', () => {
  const full = Array.from({ length: MAX_SCOPE_DOCUMENTS }, (_, index) => `doc-${index}`)
  assert.equal(canSelectMore(full, 'another'), false)
  assert.equal(canSelectMore(full, 'doc-0'), true)
  assert.equal(canSelectMore(full.slice(1), 'another'), true)
})

test('a scope conflict is explained differently from a thread conflict', () => {
  const scopeConflict = { status: 409, code: 'document_scope_unavailable', message: 'x' }
  const threadConflict = { status: 409, code: 'conflict', message: 'y' }
  assert.match(startFailureMessage(scopeConflict), /不可用/)
  assert.match(startFailureMessage(threadConflict), /请求在运行/)
  assert.notEqual(startFailureMessage(scopeConflict), startFailureMessage(threadConflict))
})

test('other failures fall back to the server message', () => {
  assert.equal(startFailureMessage({ status: 500, code: 'internal_error', message: 'boom' }), 'boom')
  assert.equal(startFailureMessage(null), '请求未能开始。')
})
