/**
 * frontend/src/sse.js 的单元测试（Node 内置测试运行器，零新增依赖）。
 *
 * 覆盖 RunStream 的四项契约行为（API-005 / ADR-010）：
 *   1. final 与终止 error 关闭流，且之后不再发起任何请求；
 *   2. 重复投递（服务端回放或网络重送）按 event_id 去重，不重复触发副作用；
 *   3. 断线重连沿用同一个 Run，并携带 Last-Event-ID 续传；
 *   4. 409（回放窗口过期）/ 404（Run 不存在）只报错一次，不无限重连。
 *
 * 运行方式：在 frontend 目录执行 `npm test`（等价于 `node --test src/`）。
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { setTimeout as sleep } from "node:timers/promises";

// api.js 在模块加载时会读取 localStorage，因此先注入最小桩；
// 静态 import 会先于本文件的代码执行，所以必须使用动态 import 导入被测模块。
globalThis.localStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};

const { RunStream } = await import("./sse.js");

/** 事件序号计数器：让每个构造的 SSE 信封拥有递增的 sequence，贴近真实线格式。 */
let sequenceCounter = 0;

/** 构造一个完整的 SSE 事件信封（字段与 API-005 的 Event Envelope 一致）。 */
function makeEvent(eventType, eventId, payload = {}) {
  sequenceCounter += 1;
  return {
    event_id: eventId,
    sequence: sequenceCounter,
    thread_id: "thread-1",
    run_id: "run-1",
    event_type: eventType,
    timestamp: "2026-10-07T00:00:00Z",
    payload,
  };
}

/** 构造一个成功响应：把事件序列编码成 SSE 线格式，并放入可读流供 read() 消费。 */
function sseResponse(events) {
  const encoder = new TextEncoder();
  return {
    ok: true,
    status: 200,
    body: new ReadableStream({
      start(controller) {
        for (const event of events) {
          const wire = `id: ${event.event_id}\nevent: ${event.event_type}\ndata: ${JSON.stringify(event)}\n\n`;
          controller.enqueue(encoder.encode(wire));
        }
        controller.close();
      },
    }),
    json: async () => null,
  };
}

/** 构造一个失败响应；错误信封与 API-009 的统一错误结构一致。 */
function errorResponse(status, code) {
  return {
    ok: false,
    status,
    body: null,
    json: async () => ({
      error: { code, message: "The request could not be completed." },
    }),
  };
}

/** 安装可注入的 fetch 桩：按顺序返回预置响应，并记录每一次请求的 URL、方法与请求头。 */
function installFetchStub(responses) {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({
      url,
      method: options.method ?? "GET",
      headers: options.headers ?? {},
    });
    const response = responses.shift();
    assert.ok(response, `fetch 桩收到了计划外的请求：${url}`);
    return response;
  };
  return calls;
}

/** 轮询等待条件成立，避免测试依赖精确的定时器与微任务时序。 */
async function waitFor(predicate, { timeoutMs = 3000, intervalMs = 25 } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate() && Date.now() < deadline) {
    await sleep(intervalMs);
  }
  return predicate();
}

test("final 事件关闭流，且之后不再发起任何请求", async () => {
  const calls = installFetchStub([
    sseResponse([
      makeEvent("agent_start", "ev-1", {}),
      makeEvent("final", "ev-2", { status: "completed", content: "完成" }),
    ]),
  ]);
  const delivered = [];
  let closes = 0;
  const stream = new RunStream("run-1", {
    onEvent: (event) => delivered.push(event.event_type),
    onClose: () => {
      closes += 1;
    },
  });

  await stream.start();

  assert.deepEqual(delivered, ["agent_start", "final"]);
  assert.equal(closes, 1);
  assert.equal(stream.closed, true);

  // 终态之后即使等待超过重连延迟，也不得再发起请求。
  await sleep(700);
  assert.equal(calls.length, 1);
});

test("终止 error 事件关闭流，且之后不再发起任何请求", async () => {
  const calls = installFetchStub([
    sseResponse([
      makeEvent("error", "ev-1", {
        code: "internal_error",
        message: "失败",
        terminal: true,
      }),
    ]),
  ]);
  const delivered = [];
  let closes = 0;
  const stream = new RunStream("run-1", {
    onEvent: (event) => delivered.push(event.event_type),
    onClose: () => {
      closes += 1;
    },
  });

  await stream.start();

  assert.deepEqual(delivered, ["error"]);
  assert.equal(closes, 1);
  assert.equal(stream.closed, true);

  await sleep(700);
  assert.equal(calls.length, 1);
});

test("非终止 error 只作为普通事件，不会提前关闭流", async () => {
  const softError = () =>
    makeEvent("error", "ev-1", {
      code: "llm_unavailable",
      message: "可修复失败",
      terminal: false,
    });
  const calls = installFetchStub([
    // 第一次连接：只有一条非终止 error，随后服务端断开。
    sseResponse([softError()]),
    // 重连回放：重复投递的 ev-1 应被去重，流由 final 正常收尾。
    sseResponse([
      softError(),
      makeEvent("final", "ev-2", { status: "completed", content: "完成" }),
    ]),
  ]);
  const delivered = [];
  const stream = new RunStream("run-1", {
    onEvent: (event) => delivered.push(event.event_id),
  });

  await stream.start();
  // 非终止 error 不构成终态：此时流并未关闭，客户端等待重连。
  assert.equal(stream.closed, false);

  assert.ok(await waitFor(() => delivered.length === 2), "重连后应收到 final");
  assert.ok(
    await waitFor(() => stream.closed === true),
    "final 到达后流应关闭",
  );
  assert.deepEqual(delivered, ["ev-1", "ev-2"]);
  assert.equal(calls.length, 2);
});

test("重复投递按 event_id 去重，不重复触发副作用", async () => {
  const start = makeEvent("agent_start", "ev-1", {});
  const token = makeEvent("token", "ev-2", { delta: "你" });
  const finalEvent = makeEvent("final", "ev-3", {
    status: "completed",
    content: "你好",
  });
  installFetchStub([sseResponse([start, start, token, token, finalEvent])]);

  const delivered = [];
  const stream = new RunStream("run-1", {
    onEvent: (event) => delivered.push(event.event_id),
  });

  await stream.start();

  // 每个 event_id 只产生一次副作用；重复投递被完整丢弃。
  assert.deepEqual(delivered, ["ev-1", "ev-2", "ev-3"]);
});

test("断线重连沿用同一个 Run，并携带 Last-Event-ID 续传", async () => {
  const first = makeEvent("agent_start", "ev-1", {});
  const second = makeEvent("token", "ev-2", { delta: "你" });
  const finalEvent = makeEvent("final", "ev-3", {
    status: "completed",
    content: "你好",
  });
  const calls = installFetchStub([
    // 第一次连接：读完即断开，没有终态事件。
    sseResponse([first]),
    // 重连：服务端从 Last-Event-ID 之后回放，包含已见过的 ev-1。
    sseResponse([first, second, finalEvent]),
  ]);
  const delivered = [];
  let closes = 0;
  const stream = new RunStream("run-1", {
    onEvent: (event) => delivered.push(event.event_id),
    onClose: () => {
      closes += 1;
    },
  });

  await stream.start();
  assert.equal(calls.length, 1);

  assert.ok(
    await waitFor(() => calls.length === 2),
    "超过重连延迟后应重新连接",
  );
  assert.ok(
    await waitFor(() => delivered.length === 3),
    "重连后应收到后续事件",
  );
  assert.ok(
    await waitFor(() => stream.closed === true && closes === 1),
    "final 到达后应关闭流",
  );

  // 两次连接都指向同一个 Run 的事件流；重连从未创建新的 Run。
  assert.ok(
    calls.every(
      (call) =>
        call.method === "GET" && call.url === "/api/v1/runs/run-1/events",
    ),
    `意外的请求：${JSON.stringify(calls)}`,
  );
  // 重连请求携带 Last-Event-ID 续传；重复回放的 ev-1 不再触发副作用。
  assert.equal(calls[1].headers["Last-Event-ID"], "ev-1");
  assert.deepEqual(delivered, ["ev-1", "ev-2", "ev-3"]);
});

for (const [status, code] of [
  [409, "conflict"],
  [404, "not_found"],
]) {
  test(`回放位置不可用（${status} ${code}）只报错一次，不无限重连`, async () => {
    const calls = installFetchStub([errorResponse(status, code)]);
    const errors = [];
    let closes = 0;
    const stream = new RunStream("run-1", {
      onError: (error) => errors.push(error),
      onClose: () => {
        closes += 1;
      },
    });

    await stream.start();

    assert.equal(errors.length, 1);
    assert.equal(errors[0].status, status);
    assert.equal(closes, 1);

    // 即使等待超过一次重连延迟，也不得再次请求。
    await sleep(700);
    assert.equal(calls.length, 1);
  });
}
