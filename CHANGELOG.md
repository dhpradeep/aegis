# Changelog

## [1.2.1] - 2026-08-25

### Fixed
- Claude Code conversations are keyed by the client's own session id (`metadata.user_id`), so a full interactive chat stays in one Session; transcript-prefix matching remains the fallback for other clients.
- Claude Code's next-prompt suggestion side requests no longer hijack or advance a session; they run as stateless completions.
- If a client compacts or rewrites its history, the conversation resyncs into the same Session instead of spawning a new one.

## [1.2.0] - 2026-08-25

### Added
- Anthropic Messages API shim (`POST /v1/messages`, `POST /v1/messages/count_tokens`) so Claude Code runs against Aegis via `ANTHROPIC_BASE_URL`; `x-api-key` auth accepted.
- Agentic CLI conversations (requests carrying `tools`) are routed into Sessions: follow-up turns are recognized by transcript prefix and resume the same SDK session, sending only new turns. The client system prompt is recorded on the session.
- Delete revoked API keys from the dashboard and `DELETE /admin/api/keys/{id}`; usage history keeps its tenant attribution.
- Completions: searchable tenant filter, redesigned detail view with collapsible system prompt, tool calls, and markdown rendering.
- Compose host port override via `AEGIS_PORT`.

### Changed
- OpenAI shim returns real `tool_calls` for requests with `tools` (previously ignored), reports failures as HTTP 502 / SSE `error` frames instead of assistant text, returns `content: null` for tool-only replies, and serves the full model catalog on `/v1/models`.
- Client system prompts are embedded in the prompt body instead of replacing the CLI system prompt.

### Fixed
- Client disconnects mid-stream no longer poison the SQLite connection pool.
- Large client system prompts no longer fail with a spurious "out of extra usage" error.

## [1.1.0] - 2026-08-14

### Added
- Tag-driven release pipeline publishing multi-arch images to Docker Hub.
- opencode / OpenAI-compatible agent CLI support.
