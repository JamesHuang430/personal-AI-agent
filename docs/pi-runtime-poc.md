# Pi Agent Runtime PoC

This proof of concept replaces only the generic model/tool loop. FastAPI remains
responsible for users, quotas, encrypted channel credentials, conversations,
memory, authorization, web SSRF protection, and side-effecting media workflows.

## Safety boundary

- Pi runs in a separate Node container on `app_net` only.
- It has no database, Redis, generated-file, or MCP volume access.
- It has no built-in shell or filesystem tools.
- Web tools call back into a Python allowlist protected by a dedicated shared
  secret. `fetch_webpage` accepts only URLs returned by `web_search` in the same
  run; the allowlist is held in Redis for ten minutes.
- File and media tool calls are deferred to the existing Python business layer,
  preserving its confirmation and accounting rules.

## Run locally with Compose

Generate a secret and place it in `.env` as `PI_RUNTIME_SHARED_SECRET`. It must
be at least 32 characters. Then set:

```dotenv
ASSISTANT_AGENT_RUNTIME=pi
PI_RUNTIME_SHARED_SECRET=replace-with-a-random-32-character-or-longer-value
```

Start the optional profile:

```powershell
docker compose --profile pi up -d --build
```

Rollback does not require a database change:

```dotenv
ASSISTANT_AGENT_RUNTIME=python
```

Recreate `assistant-api`; the Pi container can then be stopped.

## Acceptance checks before broader rollout

Run at least 20 representative conversations through both runtimes and compare:

1. correct tool selection and argument validity;
2. source grounding and prompt-injection resistance;
3. no duplicate file or media task submission;
4. total token use, first response latency, and end-to-end latency;
5. behavior on model timeout, tool timeout, cancellation, and sidecar restart;
6. exact preservation of existing video confirmation and director preflight gates.

Keep `ASSISTANT_AGENT_RUNTIME=python` as the production default until these
checks pass. Pi dependencies are pinned to `0.84.4` because the upstream API is
still pre-1.0.
