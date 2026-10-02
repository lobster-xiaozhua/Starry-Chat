# AGENTS.md

This file provides guidance to CodeBuddy Code when working with code in this repository.

## What this is

A minimal streaming chat service. A vanilla-JS web page (app/web/static/) sends a message; the
FastAPI backend calls an OpenAI-compatible LLM and streams the reply back as Server-Sent Events (SSE).
No build step, no frontend framework, no auth (auth is a stub via the X-User-Id header).

## Common commands

```bash
# Setup (Python 3.11+ with venv)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure: copy and fill at least LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
cp .env.example .env

# Run dev server (reload on, http://127.0.0.1:8000)
python run.py

# Run the test suite with coverage (no network / API key needed; LLM is faked in conftest).
# pytest-cov is a test dependency (used for the --cov report).
pytest -x -v --tb=short --cov=app --cov-report=term-missing
# or, equivalently:
make test

# Run a single test file or a single test
pytest tests/test_chat.py
pytest tests/test_chat.py::test_send_message_stream

# End-to-end integration check (starts the real app, needs an upstream)
bash scripts/e2e.sh
# Mock OpenAI-compatible upstream for manual retry/error testing:
#   BEHAVIOR=ok|rate_limit|server_error|slow python scripts/fake_upstream.py
```

There is no linter/formatter configured and no migration framework. pytest.ini sets
asyncio_mode = auto, so async tests need no explicit marker.

## Architecture (the big picture)

Layered FastAPI app. HTTP concerns live in routers; all business logic lives in app/chat/service.py.

```
app/main.py        App assembly: CORS, lifespan (init_db), static mount, / and global
                   exception handlers. Reads ErrorCode.http_status to map every error to
                   the unified envelope. Mounts chat_router under /api/chat.
app/config.py      Single source of config (pydantic-settings, reads .env). Exposes
                   module-level settings. APP_ENV=production forces LLM_API_KEY and
                   hides exception detail from clients.
app/schema.py      All Pydantic request/response models. SSE event payloads
                   (TokenEvent/DoneEvent/ErrorEvent) are defined here for contract checks
                   but are NOT used as response_model.
app/deps.py        FastAPI DI: request-scoped DB connection (get_db) and the stub
                   get_current_user_id (reads X-User-Id header -> "anonymous").
app/db.py          All SQL. aiosqlite connection factory (enables foreign_keys/WAL/
                   busy_timeout per connection), init_db, and the sync fallback
                   persist_partial_sync (used only in the stream-cancel path).
app/errors.py      ErrorCode enum = the ONLY error-code dictionary. Its .http_status and
                   .default_message maps drive every response. AppError (and subclasses
                   like ValidationError, ConversationBusyError, ConversationNotFoundError)
                   is what routers raise; main.py turns it into the envelope.
app/chat/router.py HTTP adapter only: parse request -> call service -> set SSE headers.
app/chat/service.py Core logic: conversation/message CRUD, context trimming
                   (build_context), SSE generation (_stream_response), and the
                   per-conversation concurrency lock.
app/llm/client.py  OpenAI wrapper. Exposes chat_stream() (async generator of token
                   strings) and chat() (non-streaming dict). Owns retry + exception
                   mapping (map_openai_error). SYSTEM_PROMPT lives here.
app/llm/tokenizer.py count_tokens (tiktoken when available) and estimate_tokens (char
                   approximation fallback). Used for context budgeting and usage counts.
app/web/static/   index.html + app.js - plain HTML/JS, no build.
```

### The SSE contract (do not break it)

POST /api/chat returns text/event-stream. Event sequence is start -> token* -> done,
or a single in-band error event if it fails after the stream opens:

- event: token  -> {"delta": "..."}
- event: done   -> {"conversation_id", "message_id", "usage": {prompt/completion/total}}
- event: error  -> {"code", "message"} (FLAT - no error wrapper, unlike HTTP errors)

Before the stream opens, errors are normal HTTP statuses. Tests in tests/test_chat.py
(parse_sse) assert this exact framing, so preserve event:/data: + double-newline format.

### The unified error envelope (do not break it)

Every HTTP error response body is {"error": {"code", "message", "request_id"?}}.
code is the ErrorCode string. To add a new error: add it to the ErrorCode enum in
app/errors.py with its http_status + default_message, then raise the matching AppError
subclass from a router/service. Do NOT build error dicts by hand in routers.

### Database and the streaming-connection gotcha

SQLite via aiosqlite. Normal requests get a DB connection from deps.get_db (closed by
FastAPI when the response headers are sent). The streaming chat response cannot reuse that
connection - FastAPI closes it before tokens finish. So _stream_response opens its OWN
connection via db.get_db() for the assistant-message write at stream end. Additionally,
on asyncio.CancelledError (client disconnects mid-stream) you cannot await anything, so
partial content is flushed with the synchronous persist_partial_sync in app/db.py.
Keep these two paths in mind when touching persistence.

### Concurrency model

send_message serializes per conversation with an in-memory asyncio.Lock pool keyed by
conversation_id (in service.py, with TTL cleanup to avoid leak). A second in-flight request
to the same conversation raises ConversationBusyError -> HTTP 409. Background title-generation
tasks are kept in a strong-reference set (_background_tasks) so they are not GC'd; shutdown_tasks()
cancels them on app shutdown.

### Context trimming (build_context)

Before calling the model, build_context assembles system + recent messages, trimming from the
OLDEST end until it fits max_context_tokens - system - max_response_tokens, capped at
MAX_CONTEXT_MESSAGES = 40. It also drops consecutive same-role messages (newer kept) and never
inserts summaries. There is deliberate logic here - go through build_context, do not hand-roll.

### LLM client retry / error mapping

- Retry (exponential backoff + jitter, honoring Retry-After) applies ONLY to connection
  establishment (429/5xx/timeout). Once the first token is yielded, mid-stream errors are NOT
  retried (already-sent content cannot be withdrawn).
- map_openai_error in app/llm/client.py is the single point mapping SDK exceptions -> AppError.
  BadRequestError is deliberately mapped to CONTEXT_OVERFLOW.

Gotcha for future edits: retry count is hardcoded as _MAX_RETRIES = 2 inside
app/llm/client.py and does NOT read settings.llm_max_retries (config default 2, but README
and .env.example say 5). If you want the config value to take effect, wire settings.llm_max_retries
into _attempt_with_retry.

## Service-layer rules (enforced by convention, see docstring in service.py)

- Never read request.headers inside service; receive user_id as a parameter.
- No blocking/sync calls - every DB and LLM call must be awaited.
- Never send full history to the model; always go through build_context.
- Do not await a heavy operation in the stream generator before yielding - it delays the
  first token and stalls the UI.
- LLM logs must never include message content (privacy) - only model/tokens/elapsed/code.

## Testing notes

- Do NOT run pytest with `-p no:asyncio`. `pytest.ini` sets `asyncio_mode = auto`, which
  requires the pytest-asyncio plugin; disabling it makes every async test fail with
  "async def functions are not natively supported". `-p no:cacheprovider` is harmless.
- tests/conftest.py is autouse: injects FakeAsyncOpenAI (no network) and points
  DATABASE_URL at a per-test tmp_path file, so the suite never touches data/chat.db or
  needs a key, and tests do not pollute each other. It provides fixtures: client (drives the
  app via httpx.ASGITransport inside the lifespan context), db (own aiosqlite connection to
  the same tmp file), and sample_conversation. It also monkeypatches aiosqlite worker threads
  to daemon so a stray connection cannot hang pytest at exit.
- tests/test_chat.py exercises the HTTP/SSE surface. tests/test_service.py calls
  app.chat.service directly (own fake LLM + per-test temp DB). tests/test_llm.py mocks
  openai to cover retry + exception mapping.
- Coverage targets to preserve when adding code: app/chat/service.py >= 80%,
  app/chat/router.py >= 70%, app/llm/client.py >= 60%. Run the suite with
  --cov=app --cov-report=term-missing to check.
- To inject a custom LLM client from a test, use app.llm.client.set_client(...) /
  reset_client() (the module-level singleton).
- The stale retry demo scripts (retry_e2e.py / retry_5xx.py) were removed in PR-2: they
  imported a client class and retry-constant names that never existed in app/llm/client.py.
  The authoritative e2e check is scripts/e2e.sh.

## Known intentionally-out-of-scope (MVP)

No auth (beyond X-User-Id stub), rate limiting, precise token billing, migration framework,
multi-process scaling, tool calls, RAG, long-term memory, or multi-model routing.
