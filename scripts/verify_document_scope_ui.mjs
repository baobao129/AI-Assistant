/**
 * 文档作用域的界面验收（ADR-039）：用真实浏览器走一遍「选文档 → 提问 → 看引用」。
 *
 * 使用 frontend/package-lock.json 锁定的 Playwright。先在 frontend 运行 npm ci 和
 * npx playwright install chromium，再从环境提供 SCOPE_UI_PASSWORD 并运行此脚本。
 *
 * 截图与报告写入 runtime/（已被 Git 忽略）。
 */

import { mkdirSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const require = createRequire(new URL('../frontend/package.json', import.meta.url))
const { chromium } = require('@playwright/test')

const BASE_URL = process.env.SCOPE_UI_BASE_URL || 'http://127.0.0.1:5173'
const USERNAME = process.env.SCOPE_UI_USERNAME || 'demo'
const PASSWORD = process.env.SCOPE_UI_PASSWORD || ''
const CHROME_PATH = process.env.CHROME_PATH || ''
const OUT_DIR = fileURLToPath(new URL('../runtime/scope-ui/', import.meta.url))
const QUESTION = process.env.SCOPE_UI_QUESTION || '请根据这份文档说明 AI Agent 的核心组成和工作流程。'
// 按**文档名**选择目标，而不是「列表第一项」：演示库里会有测试夹具文档，
// 它们按 created_at DESC 排在前面，选错就会得到 0 引用（假失败）。
const TARGET_DOCUMENT = process.env.SCOPE_UI_DOCUMENT || 'AI-Agents-in-Depth-zh-CN.pdf'

const results = {}
const failures = []
if (!PASSWORD) throw new Error('请通过 SCOPE_UI_PASSWORD 环境变量提供验收账号密码。')

function check(name, condition, detail = null) {
  results[name] = { ok: Boolean(condition), detail }
  console.log(`  [${condition ? 'PASS' : 'FAIL'}] ${name}: ${detail}`)
  if (!condition) failures.push(name)
}

mkdirSync(OUT_DIR, { recursive: true })

const browser = await chromium.launch({
  executablePath: CHROME_PATH || undefined,
  args: ['--no-proxy-server', '--proxy-bypass-list=*'],
})
try {
  const page = await browser.newPage({ viewport: { width: Number(process.env.SCOPE_UI_WIDTH || 1440), height: Number(process.env.SCOPE_UI_HEIGHT || 900) } })
  const consoleErrors = []
  let finalPayload = null
  let acceptedRunId = null
  const streamResponses = []
  page.on('pageerror', (error) => consoleErrors.push(String(error)))
  page.on('response', (response) => {
    if (/\/conversations\/[^/]+\/runs$/.test(new URL(response.url()).pathname) && response.request().method() === 'POST') {
      streamResponses.push(response.json().then((accepted) => { acceptedRunId = accepted.run_id }).catch(() => {}))
    }
    if (!/\/runs\/[^/]+\/events$/.test(new URL(response.url()).pathname)) return
    streamResponses.push(response.text().then((wire) => {
      for (const line of wire.split('\n')) {
        if (!line.startsWith('data: ')) continue
        const envelope = JSON.parse(line.slice(6))
        if (envelope.event_type === 'final') finalPayload = envelope.payload
      }
    }).catch(() => {}))
  })

  await page.goto(`${BASE_URL}/login`, { waitUntil: 'load' })
  await page.fill('input[name="username"]', USERNAME)
  await page.fill('input[name="password"]', PASSWORD)
  await page.click('button[type="submit"]')
  await page.waitForURL((url) => !url.pathname.includes('/login'), { timeout: 15000 })

  await page.goto(`${BASE_URL}/chat`, { waitUntil: 'load' })
  await page.waitForSelector('.scope-toggle', { timeout: 15000 })
  const priorUrl = page.url()
  await page.locator('.panel-heading').getByRole('button', { name: '新对话', exact: true }).click()
  await page.waitForURL((url) => url.toString() !== priorUrl)
  check('scope_control_rendered', true)

  const initialLabel = (await page.textContent('.scope-toggle')).trim()
  check('scope_defaults_to_all_documents', initialLabel.includes('全部授权文档'), initialLabel)

  await page.click('.scope-toggle')
  await page.waitForSelector('.scope-list', { timeout: 5000 })
  const options = await page.$$('.scope-list li')
  check('document_list_rendered', options.length > 0, `${options.length} documents`)

  const ready = await page.$$('.scope-list input[type="checkbox"]:not([disabled])')
  const disabled = await page.$$('.scope-list input[type="checkbox"][disabled]')
  check('selectable_documents_present', ready.length > 0, `${ready.length} selectable`)
  console.log(`  [INFO] unavailable/disabled entries: ${disabled.length}`)

  await page.screenshot({ path: `${OUT_DIR}/scope-picker-open.png`, fullPage: false })

  // 选中**目标文档**（按名称匹配，只在可选行里找），找不到再退回第一项可选文档。
  const targetIndex = await page.$$eval(
    '.scope-list li',
    (nodes, target) => {
      const enabled = nodes.filter((node) => {
        const input = node.querySelector('input[type="checkbox"]')
        return input && !input.disabled
      })
      return enabled.findIndex((node) => node.textContent.includes(target))
    },
    TARGET_DOCUMENT,
  )
  if (targetIndex < 0) throw new Error('指定的验收文档不可用，请设置 SCOPE_UI_DOCUMENT。')
  const chosenIndex = targetIndex
  console.log(
    `  [INFO] selecting document #${chosenIndex} (target=${TARGET_DOCUMENT}, matched=${targetIndex >= 0})`,
  )
  await ready[chosenIndex].check()
  const selectedLabel = (await page.textContent('.scope-toggle')).trim()
  check('scope_label_reflects_selection', selectedLabel.includes('已选 1 份文档'), selectedLabel)

  await page.fill('.composer textarea', QUESTION)
  await page.screenshot({ path: `${OUT_DIR}/scope-selected.png`, fullPage: false })
  // 发送前记录已有 assistant 消息数：会话可能已经包含历史答案，
  // 只等「数量增加」会立刻返回并读到上一条答案（假失败）。
  const beforeCount = await page.$$eval('.message.assistant', (nodes) => nodes.length)
  await page.click('.composer button[type="submit"]')

  await page.waitForFunction(
    (count) => document.querySelectorAll('.message.assistant').length > count && !document.querySelector('.composer textarea').disabled,
    beforeCount,
    { timeout: 180000 },
  )
  // G14：终态后前端会重取持久化消息并以其为准，因此轮询最后一条 assistant 的引用。
  await page
    .waitForFunction(
      () => {
        const nodes = document.querySelectorAll('.message.assistant')
        const last = nodes[nodes.length - 1]
        return Boolean(last) && last.querySelectorAll('.citations li').length > 0
      },
      undefined,
      { timeout: 30000 },
    )
    .catch(() => {})
  await page.waitForTimeout(1500)
  await page.screenshot({ path: `${OUT_DIR}/scoped-answer.png`, fullPage: false })

  const activity = await page.textContent('.event-list')
  check('run_stayed_on_knowledge_path', activity.includes('知识问答'), '知识问答路径')
  check(
    'no_structured_data_tool_call',
    !activity.includes('查询业务数据'),
    'no database_query tool call',
  )
  // 只取**最后一条** assistant 消息的引用：历史答案不属于本次运行。
  const lastAssistant = await page.evaluate(() => {
    const nodes = document.querySelectorAll('.message.assistant')
    const last = nodes[nodes.length - 1]
    return {
      citations: last
        ? Array.from(last.querySelectorAll('.citations li')).map((node) => node.textContent.trim())
        : [],
      text: last ? last.textContent.trim() : '',
    }
  })
  const citations = lastAssistant.citations
  check('assistant_citations_rendered', citations.length > 0, `${citations.length} citations`)
  check(
    'answer_is_not_a_safe_failure',
    !/暂时不可用|证据不足|没有足够证据|无法回答|无法可靠回答|无法提供可靠答案|未通过(?:证据校验|验证)|insufficient evidence|temporarily unavailable|no longer available|could not be validated/i.test(lastAssistant.text),
    lastAssistant.text.slice(0, 120),
  )
  check('no_console_errors', consoleErrors.length === 0, consoleErrors.slice(0, 2))
  await Promise.all(streamResponses)
  // 前端在 final 后主动 abort，浏览器 response.text 可能被取消；按既有 Run 回放读取终态。
  if (!finalPayload && acceptedRunId) {
    finalPayload = await page.evaluate(async (runId) => {
      const token = localStorage.getItem('dsh.session.token')
      const response = await fetch(`/api/v1/runs/${runId}/events`, { headers: { Authorization: `Bearer ${token}`, Accept: 'text/event-stream' } })
      if (!response.ok) return null
      const wire = await response.text()
      let final = null
      for (const line of wire.split('\n')) {
        if (!line.startsWith('data: ')) continue
        const event = JSON.parse(line.slice(6))
        if (event.event_type === 'final') final = event.payload
      }
      return final
    }, acceptedRunId)
  }
  check('grounded_final_validated', finalPayload?.validation_status === 'validated' && !finalPayload?.error_code, { validation_status: finalPayload?.validation_status, error_code: finalPayload?.error_code })
  await page.reload()
  await page.waitForFunction((expected) => {
    const messages = document.querySelectorAll('.message.assistant')
    return messages.length && messages[messages.length - 1].textContent.trim() === expected
  }, lastAssistant.text)
  const historyCitations = await page.locator('.message.assistant').last().locator('.citations li').allTextContents()
  check('final_matches_persisted_history', JSON.stringify(historyCitations.map((text) => text.trim())) === JSON.stringify(citations))
  await page.screenshot({ path: `${OUT_DIR}/history-answer.png`, fullPage: false })

  writeFileSync(
    `${OUT_DIR}/report.json`,
    JSON.stringify({ results, citations, consoleErrors, final: { validation_status: finalPayload?.validation_status, error_code: finalPayload?.error_code, status: finalPayload?.status } }, null, 2),
    'utf8',
  )
} finally {
  await browser.close()
}

console.log(`screenshots: ${OUT_DIR}`)
if (failures.length) {
  console.log(`FAILED: ${failures.join(', ')}`)
  process.exit(1)
}
console.log('ALL CHECKS PASSED')
