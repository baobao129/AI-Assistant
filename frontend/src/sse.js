/**
 * SSE client for one Run.
 *
 * - uses `Last-Event-ID` to replay retained events after a reconnect;
 * - deduplicates by `event_id`, so replay or duplicate delivery cannot repeat a
 *   client-side effect;
 * - stops on the terminal `final` or terminal `error` event;
 * - never creates a new Run on reconnect.
 */

import { authHeaders } from './api'

export class RunStream {
  constructor(runId, { onEvent, onError, onClose, lastEventId = null } = {}) {
    this.runId = runId
    this.onEvent = onEvent || (() => {})
    this.onError = onError || (() => {})
    this.onClose = onClose || (() => {})
    this.lastEventId = lastEventId
    this.seen = new Set()
    this.controller = null
    this.closed = false
    this.reconnectDelay = 500
  }

  async start() {
    this.closed = false
    await this.connect()
  }

  async connect() {
    if (this.closed) return
    this.controller = new AbortController()
    try {
      const headers = authHeaders({ Accept: 'text/event-stream' })
      if (this.lastEventId) headers['Last-Event-ID'] = this.lastEventId
      const response = await fetch(`/api/v1/runs/${this.runId}/events`, {
        headers,
        signal: this.controller.signal,
      })
      if (!response.ok) {
        const body = await response.json().catch(() => null)
        const error = new Error(body?.error?.message || `Stream failed (${response.status})`)
        error.status = response.status
        throw error
      }
      await this.read(response)
    } catch (error) {
      if (this.closed || error.name === 'AbortError') return
      // Replay outside retention or a missing Run must not be retried forever.
      if (error.status === 409 || error.status === 404 || error.status === 401) {
        this.onError(error)
        this.onClose()
        return
      }
      this.onError(error)
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, 8000)
      setTimeout(() => this.connect(), this.reconnectDelay)
    }
  }

  async read(response) {
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let terminal = false
    while (!this.closed) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let separator = buffer.indexOf('\n\n')
      while (separator !== -1) {
        const chunk = buffer.slice(0, separator)
        buffer = buffer.slice(separator + 2)
        const event = this.parseChunk(chunk)
        if (event) {
          terminal = event.event_type === 'final' || this.isTerminalError(event)
          if (!this.seen.has(event.event_id)) {
            this.seen.add(event.event_id)
            this.lastEventId = event.event_id
            this.onEvent(event)
          }
          if (terminal) {
            this.close()
            this.onClose()
            return
          }
        }
        separator = buffer.indexOf('\n\n')
      }
    }
    if (!this.closed && !terminal) {
      // The server closed the stream. Reconnect with Last-Event-ID to continue replay.
      setTimeout(() => this.connect(), this.reconnectDelay)
    }
  }

  parseChunk(chunk) {
    let data = null
    for (const line of chunk.split('\n')) {
      if (line.startsWith('data: ')) {
        try {
          data = JSON.parse(line.slice(6))
        } catch {
          data = null
        }
      }
    }
    return data
  }

  isTerminalError(event) {
    return event.event_type === 'error' && event.payload && event.payload.terminal === true
  }

  close() {
    this.closed = true
    this.controller?.abort()
  }
}
