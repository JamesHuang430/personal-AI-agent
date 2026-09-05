import { createServer } from "node:http";
import { timingSafeEqual } from "node:crypto";
import { runAgent } from "./runtime.mjs";

const port = Number(process.env.PORT || 8787);
const sharedSecret = process.env.PI_RUNTIME_SHARED_SECRET || "";
const bridgeUrl = process.env.PI_RUNTIME_TOOL_BRIDGE_URL || "";
const maxBodyBytes = 2 * 1024 * 1024;

function json(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
  });
  response.end(body);
}

function authorized(request) {
  const supplied = String(request.headers["x-pi-runtime-secret"] || "");
  if (!sharedSecret || supplied.length !== sharedSecret.length) return false;
  return timingSafeEqual(Buffer.from(supplied), Buffer.from(sharedSecret));
}

async function readJson(request) {
  let size = 0;
  const chunks = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maxBodyBytes) throw new Error("Request body is too large");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

const server = createServer(async (request, response) => {
  if (request.method === "GET" && request.url === "/health") {
    const ready = Boolean(sharedSecret && bridgeUrl);
    return json(response, ready ? 200 : 503, {
      status: ready ? "ok" : "misconfigured",
      runtime: "pi-agent-core",
    });
  }
  if (request.method !== "POST" || request.url !== "/v1/runs") {
    return json(response, 404, { error: "Not found" });
  }
  if (!authorized(request)) return json(response, 401, { error: "Unauthorized" });
  if (!bridgeUrl) return json(response, 503, { error: "Tool bridge is not configured" });

  try {
    const input = await readJson(request);
    for (const name of ["run_id", "model", "base_url", "api_key", "system_prompt", "message"]) {
      if (typeof input[name] !== "string" || !input[name]) {
        return json(response, 422, { error: `Missing field: ${name}` });
      }
    }
    input.require_model_permit = true;
    const result = await runAgent(input, { bridgeUrl, sharedSecret });
    return json(response, 200, result);
  } catch (error) {
    return json(response, 502, { error: error instanceof Error ? error.message : "Pi runtime failed" });
  }
});

server.listen(port, "0.0.0.0");
