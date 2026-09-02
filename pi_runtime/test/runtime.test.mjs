import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import { buildTools, normalizeHistory, runAgent, summarizeMessages } from "../src/runtime.mjs";

async function listen(handler) {
  const server = createServer(handler);
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  return {
    server,
    url: `http://127.0.0.1:${address.port}`,
  };
}

function sendCompletion(response, deltas) {
  response.writeHead(200, { "content-type": "text/event-stream" });
  for (const delta of deltas) {
    response.write(`data: ${JSON.stringify({
      id: "chatcmpl-test",
      object: "chat.completion.chunk",
      created: 1,
      model: "test-model",
      choices: [delta],
    })}\n\n`);
  }
  response.end("data: [DONE]\n\n");
}

test("normalizes only user and assistant history", () => {
  const messages = normalizeHistory([
    { role: "user", content: "问题" },
    { role: "assistant", content: "答案" },
    { role: "system", content: "ignore" },
  ], "test-model");
  assert.equal(messages.length, 2);
  assert.equal(messages[1].content[0].text, "答案");
});

test("defers side-effect tools without calling the bridge", async () => {
  const runtime = {
    bridgeUrl: "http://invalid.test",
    sharedSecret: "secret",
    runId: "run",
    pendingToolCalls: [],
    pendingFingerprints: new Set(),
    sourcesByUrl: new Map(),
  };
  const tools = buildTools([{
    function: {
      name: "generate_video",
      description: "Generate video",
      parameters: { type: "object", properties: {}, additionalProperties: false },
    },
  }], runtime);
  const result = await tools[0].execute("call", { prompt: "x" });
  assert.equal(runtime.pendingToolCalls[0].name, "generate_video");
  assert.equal(result.details.deferred, true);

  await tools[0].execute("call-again", { prompt: "x" });
  assert.equal(runtime.pendingToolCalls.length, 1);
});

test("summarizes assistant text and OpenAI-compatible usage", () => {
  const summary = summarizeMessages([{
    role: "assistant",
    content: [{ type: "text", text: "完成" }],
    usage: { input: 12, output: 3, totalTokens: 15 },
  }]);
  assert.equal(summary.content, "完成");
  assert.deepEqual(summary.usage, {
    prompt_tokens: 12,
    completion_tokens: 3,
    total_tokens: 15,
  });
});

test("runs a real Pi turn against an OpenAI-compatible stream", async (context) => {
  const upstream = await listen((_request, response) => {
    sendCompletion(response, [
      { index: 0, delta: { role: "assistant", content: "Pi 已接通" }, finish_reason: null },
      { index: 0, delta: {}, finish_reason: "stop" },
    ]);
  });
  context.after(() => upstream.server.close());

  const result = await runAgent({
    run_id: "run-direct",
    model: "test-model",
    base_url: upstream.url,
    api_key: "test-key",
    system_prompt: "Answer briefly",
    message: "hello",
    history: [],
    tools: [],
    max_turns: 3,
  }, {
    bridgeUrl: "http://127.0.0.1:1/unused",
    sharedSecret: "s".repeat(32),
  });

  assert.equal(result.content, "Pi 已接通");
  assert.equal(result.turns, 1);
});

test("executes web search through the authenticated Python tool bridge", async (context) => {
  let modelCalls = 0;
  const upstream = await listen((_request, response) => {
    modelCalls += 1;
    if (modelCalls === 1) {
      sendCompletion(response, [
        {
          index: 0,
          delta: {
            role: "assistant",
            tool_calls: [{
              index: 0,
              id: "call-search",
              type: "function",
              function: { name: "web_search", arguments: "{\"query\":\"Pi Agent\"}" },
            }],
          },
          finish_reason: null,
        },
        { index: 0, delta: {}, finish_reason: "tool_calls" },
      ]);
      return;
    }
    sendCompletion(response, [
      { index: 0, delta: { role: "assistant", content: "根据来源，Pi 可用。" }, finish_reason: null },
      { index: 0, delta: {}, finish_reason: "stop" },
    ]);
  });
  context.after(() => upstream.server.close());

  let bridgeRequest;
  const bridge = await listen(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    bridgeRequest = {
      secret: request.headers["x-pi-runtime-secret"],
      body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
    };
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({
      is_error: false,
      data: {
        query: "Pi Agent",
        results: [{
          title: "Pi",
          url: "https://example.com/pi",
          snippet: "Agent runtime",
          source: "Example",
          date: "",
        }],
      },
    }));
  });
  context.after(() => bridge.server.close());

  const secret = "s".repeat(32);
  const result = await runAgent({
    run_id: "run-search",
    model: "test-model",
    base_url: upstream.url,
    api_key: "test-key",
    system_prompt: "Search first",
    message: "What is Pi?",
    history: [],
    tools: [{
      type: "function",
      function: {
        name: "web_search",
        description: "Search the web",
        parameters: {
          type: "object",
          properties: { query: { type: "string" } },
          required: ["query"],
          additionalProperties: false,
        },
      },
    }],
    max_turns: 3,
  }, { bridgeUrl: `${bridge.url}/tools`, sharedSecret: secret });

  assert.equal(result.content, "根据来源，Pi 可用。");
  assert.equal(result.turns, 2);
  assert.equal(result.web_sources[0].url, "https://example.com/pi");
  assert.equal(bridgeRequest.secret, secret);
  assert.equal(bridgeRequest.body.name, "web_search");
});
