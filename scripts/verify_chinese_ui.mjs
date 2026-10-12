/** Reproducible UI fixture checks; live retrieval is verified separately. */
import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
const require = createRequire(new URL('../frontend/package.json', import.meta.url))
const { chromium } = require('@playwright/test')
const base = process.env.UI_BASE_URL || 'http://127.0.0.1:5174'
const output = fileURLToPath(new URL('../runtime/chinese-ui/', import.meta.url))
mkdirSync(output, { recursive: true })
const longName = '企业知识库年度预算和采购审批实施细则补充说明'.repeat(4) + '.pdf'
const citations = Array.from({ length: 8 }, (_, i) => ({ source_id: `source-${i}`, label: longName, location: `第 ${i + 1} 页 · 采购审批与预算限制` }))
const documents = ['available', 'indexing', 'failed', 'deleted'].map((status, i) => ({ document_id: `doc-${i}`, title: longName, source_name: longName, status, active_index_version: i ? null : 'index-version-20261010', updated_at: '2026-10-10T09:00:00+08:00' }))
const report = { tool: `@playwright/test ${require('@playwright/test/package.json').version}`, base, mode: 'controlled API fixtures with real HTTP incremental SSE', checks: [], screenshots: [] }
let mode = 'normal'
let complete = false
let submitted = null
let retried = false
let releaseFinal
let releaseRevision
const frame = (run, seq, type, payload) => `id: ${run}-${seq}\nevent: ${type}\ndata: ${JSON.stringify({ event_id: `${run}-${seq}`, run_id: run, sequence: seq, event_type: type, payload })}\n\n`
const server = createServer((request, response) => {
  response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*' })
  const run = request.url.slice(1)
  if (mode === 'approval') {
    response.end(frame(run, 1, 'approval_required', { approval_request_id: 'approval-1', conversation_id: 'conversation-1', thread_id: 'thread-1', run_id: run, summary: { row_count: 50, columns: ['部门', '预算金额'] } }))
    return
  }
  response.write(frame(run, 1, 'intent', { intent: 'knowledge', routing_outcome: 'routed' }))
  response.write(frame(run, 2, 'tool_call', { tool_id: 'knowledge_search' }))
  response.write(frame(run, 3, 'token', { delta: '正在生成的临时文本' }))
  releaseRevision = () => {
    response.write(frame(run, 4, 'token', { delta: '', reset: true, provisional: true }))
    response.write(frame(run, 5, 'token', { delta: '修订后的临时文本', provisional: true }))
  }
  releaseFinal = () => {
    complete = true
    if (mode === 'terminal-error') {
      response.end(frame(run, 6, 'error', { code: 'llm_unavailable', terminal: true }))
      return
    }
    response.end(frame(run, 6, 'final', mode === 'error'
      ? { status: 'failed', content: 'Sensitive provider failure', error_code: 'knowledge_index_incompatible', citations: [] }
      : { status: 'completed', content: '最终答复：采购金额与审批数量应按授权文档中的规定执行。', citations, validation_status: 'passed' }))
  }
})
await new Promise((done) => server.listen(0, '127.0.0.1', done))
const streamPort = server.address().port
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined })
async function check(page, name, condition) {
  assert.ok(condition, name)
  report.checks.push({ viewport: page.viewportSize(), name, passed: true })
}
async function snapshot(page, name) {
  if (name.endsWith('answer-citations')) await page.locator('.transcript').evaluate((node) => { node.scrollTop = 0 })
  await page.screenshot({ path: resolve(output, `${name}.png`), fullPage: true })
  report.screenshots.push(`${name}.png`)
  const dimensions = await page.evaluate(() => ({ width: innerWidth, content: document.documentElement.scrollWidth }))
  await check(page, `${name}: no page horizontal overflow`, dimensions.content <= dimensions.width + 1)
}
try {
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const page = await browser.newPage({ viewport })
    const errors = []
    page.on('pageerror', (error) => errors.push(error.message))
    const size = viewport.width === 390 ? 'mobile' : 'desktop'
    mode = 'normal'; complete = false; retried = false
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request()
      const path = new URL(request.url()).pathname
      let body = { items: [] }
      if (path.endsWith('/events')) {
        await route.continue({ url: `http://127.0.0.1:${streamPort}/${path.split('/')[4]}` })
        return
      }
      if (path.endsWith('/auth/login')) body = { session: { token: 'ui-fixture-session' }, user: { user_id: 'fixture-user', display_name: '验收用户' } }
      else if (path.endsWith('/health')) body = { status: 'healthy', services: { backend: 'healthy', mysql: 'healthy', milvus: 'healthy' } }
      else if (path.endsWith('/messages')) body = { items: complete ? [{ message_id: 'answer-1', role: 'assistant', content: mode === 'error' ? '文档索引与当前向量模型不兼容，请使用当前配置重建索引后重试。' : '最终答复：采购金额与审批数量应按授权文档中的规定执行。', status: mode === 'error' ? 'failed' : 'completed', citations: mode === 'error' ? [] : citations }] : [] }
      else if (path.endsWith('/conversations')) body = request.method() === 'POST' ? { conversation_id: 'conversation-1', title: '新对话' } : { items: [{ conversation_id: 'conversation-1', title: '这是用于验证超长中文会话标题不会挤压布局的对话'.repeat(3), updated_at: '2026-10-10' }] }
      else if (path.endsWith('/runs')) { submitted = request.postDataJSON(); body = { run_id: `run-${mode}`, thread_id: 'thread-1' } }
      else if (path.endsWith('/documents')) body = request.method() === 'POST' ? { ingestion_job_id: 'job-failed', document_id: 'doc-2', status: 'pending', duplicate: false } : { items: documents }
      else if (path.endsWith('/job-failed')) body = { ingestion_job_id: 'job-failed', status: 'failed', attempt: 1, failure_code: 'parse_timeout' }
      else if (path.endsWith('/retry')) { retried = true; body = { ingestion_job_id: 'job-success', document_id: 'doc-2', status: 'pending' } }
      else if (path.endsWith('/job-success')) body = { ingestion_job_id: 'job-success', status: 'success', attempt: 2 }
      else if (path.endsWith('/data-sources')) body = { items: [{ data_source_id: 'demo_mysql', schema_version: '1' }] }
      else if (path.endsWith('/tables')) body = { items: [{ table_name: '采购业务明细', column_count: 3 }] }
      else if (path.endsWith('/preview')) body = { row_count: 2, truncated: true, columns: ['部门', '金额', '备注'], rows: [['行政部', 3000, '长中文备注'.repeat(20)], ['技术部', 5000, '已脱敏']] }
      else if (path.includes('/tables/')) body = { table_name: '采购业务明细', columns: [{ name: 'department', type: 'varchar', masked: false }, { name: 'contact', type: 'varchar', masked: true }] }
      else if (path.endsWith('/reject')) body = { status: 'rejected' }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    })
    await page.goto(`${base}/login`)
    await check(page, 'Chinese document language', await page.locator('html').getAttribute('lang') === 'zh-CN')
    await page.locator('[name=username]').focus()
    await page.keyboard.press('Tab')
    await check(page, 'keyboard password focus', await page.locator('[name=password]').evaluate((node) => node === document.activeElement))
    await snapshot(page, `${size}-login`)
    await page.locator('[name=password]').fill('ui-fixture-only')
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await page.getByText('所有服务运行正常').waitFor()
    await snapshot(page, `${size}-dashboard`)
    await page.getByRole('link', { name: '对话', exact: true }).click()
    await page.locator('.scope-toggle').waitFor()
    await page.locator('.scope-toggle').focus()
    await page.keyboard.press('Enter')
    await check(page, 'keyboard opens document selector', await page.locator('.scope-toggle').getAttribute('aria-expanded') === 'true')
    await check(page, 'unavailable document options disabled', await page.locator('.scope-list input:disabled').count() === 3)
    await check(page, 'disabled reasons fit selector width', await page.locator('.scope-list').evaluate((node) => [...node.querySelectorAll('.scope-reason')].every((reason) => reason.getBoundingClientRect().right <= node.getBoundingClientRect().right)))
    await page.locator('.scope-list input:not(:disabled)').check()
    await snapshot(page, `${size}-document-scope`)
    await page.locator('.composer textarea').fill('请核对中文采购预算与审批要求。')
    await page.locator('.composer textarea').press('Enter')
    await page.getByText('正在生成的临时文本', { exact: true }).waitFor()
    await check(page, 'selected document passed to API', submitted.document_ids?.[0] === 'doc-0')
    await check(page, 'tokens do not flood progress list', await page.locator('.event-token').count() === 0)
    releaseRevision()
    await page.getByText('修订后的临时文本', { exact: true }).waitFor()
    await check(page, 'revision reset discards previous draft', await page.getByText('正在生成的临时文本', { exact: true }).count() === 0)
    releaseFinal()
    await page.getByText('最终答复：采购金额与审批数量应按授权文档中的规定执行。', { exact: true }).waitFor()
    await check(page, 'final replaces provisional message', await page.getByText('修订后的临时文本', { exact: true }).count() === 0)
    await check(page, 'citations rendered', await page.locator('.message.assistant .citations li').count() === 8)
    await check(page, 'tool label translated', await page.locator('.event-list').innerText().then((value) => value.includes('检索知识文档') && !value.includes('knowledge_search')))
    await snapshot(page, `${size}-answer-citations`)
    await page.reload()
    await page.locator('.message.assistant .citations li').first().waitFor()
    await check(page, 'history retains same citations', await page.locator('.message.assistant .citations li').count() === 8)
    mode = 'error'; complete = false
    await page.locator('.composer textarea').fill('再次检索')
    await page.locator('.composer textarea').press('Enter')
    await page.getByText('正在生成的临时文本', { exact: true }).waitFor()
    releaseFinal()
    await page.getByText('文档索引与当前向量模型不兼容，请使用当前配置重建索引后重试。', { exact: true }).waitFor()
    await check(page, 'safe error hides provider text', !(await page.locator('body').innerText()).includes('Sensitive provider failure'))
    await snapshot(page, `${size}-safe-error`)
    await page.reload()
    await page.getByText('文档索引与当前向量模型不兼容，请使用当前配置重建索引后重试。', { exact: true }).waitFor()
    await check(page, 'safe final matches history after reload', await page.locator('.message.assistant .citations li').count() === 0)
    mode = 'terminal-error'; complete = false
    await page.locator('.composer textarea').fill('模型服务失败的场景')
    await page.locator('.composer textarea').press('Enter')
    await page.getByText('正在生成的临时文本', { exact: true }).waitFor()
    releaseFinal()
    await page.getByRole('alert').filter({ hasText: '模型服务暂时不可用' }).waitFor()
    await check(page, 'terminal error clears provisional text', await page.getByText('正在生成的临时文本', { exact: true }).count() === 0)
    mode = 'approval'; complete = false
    await page.locator('.composer textarea').fill('导出采购数据')
    await page.locator('.composer textarea').press('Enter')
    await page.getByRole('alertdialog', { name: '导出审批' }).waitFor()
    await snapshot(page, `${size}-approval`)
    await page.getByRole('button', { name: '拒绝', exact: true }).click()
    await page.getByText('导出请求已拒绝，未创建导出文件。').waitFor()
    await page.getByRole('link', { name: '文档与数据' }).click()
    await page.locator('.document-table').waitFor()
    await page.locator('input[type=file]').setInputFiles({ name: longName, mimeType: 'application/pdf', buffer: Buffer.from('UI fixture') })
    await page.getByRole('button', { name: '重试', exact: true }).waitFor()
    await snapshot(page, `${size}-failed-job`)
    await page.getByRole('button', { name: '重试', exact: true }).click()
    await page.getByText('第 2 次处理', { exact: false }).waitFor()
    await check(page, 'failed job creates retry successor', retried)
    await check(page, 'original job retained', await page.locator('.job-list li').count() === 2)
    await page.getByRole('button', { name: '采购业务明细' }).click()
    await page.getByRole('region', { name: '数据预览', exact: true }).waitFor()
    await snapshot(page, `${size}-data-preview`)
    await check(page, 'no browser errors', errors.length === 0)
    await page.close()
  }
} finally {
  await browser.close()
  server.closeAllConnections()
  server.close()
  writeFileSync(resolve(output, 'report.json'), JSON.stringify(report, null, 2))
}
console.log(`PASS: ${report.checks.length} checks; ${report.screenshots.length} screenshots; ${output}`)
