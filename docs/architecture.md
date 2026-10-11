# Architecture

Business Signals is a local-first workbench for schema-aware factual answers and evidence-backed root-cause investigations.

```text
React / Vinext workbench
    │ REST + SSE
    ▼
FastAPI ── InvestigationService ── JSON archives + PostgreSQL checkpoints
    │                 │
    │                 ▼
    │          cyclic LangGraph workflow
    │            ├─ LLM providers
    │            ├─ datasource registry
    │            └─ external-research executor
    ▼
optional LangSmith monitoring
```

## Presentation and API

The React/Vinext workbench manages connections, schema discovery, relationship review, saved conversations, and the investigation timeline. Timeline records include their owning hypothesis or evidence identity, so the UI never infers hierarchy from displayed text.

FastAPI owns CORS, validation, health checks, datasource and relationship administration, research-agent listing, investigation lifecycle endpoints, and SSE. Investigation decisions are kept out of HTTP handlers.

The browser uses REST for state operations and `EventSource` while work is active. SSE events have monotonic offsets, so clients can reconnect without replaying previous events. REST investigation state is the source of truth after refreshes and reconnects.

## Conversation lifecycle and durability

`InvestigationService` starts graph work in a background task, publishes typed progress events, persists material state changes, supports cancellation, and converts unexpected failures into concise UI errors while retaining detailed logs.

| Store | Purpose |
| --- | --- |
| `.business-signals/investigations/inv_*.json` | Readable local archive restored at startup and shown in Saved Items. |
| PostgreSQL `business_signals` schema | LangGraph checkpoints used to resume an interrupted clarification or graph turn without replaying completed work. |

A follow-up uses a new checkpoint thread with compact prior-answer context; it does not restart an old graph. A negative follow-up closes the conversation. Deletion removes the archive and all related checkpoints. Active work must be stopped before it can be deleted.

## Datasources, schema, and relationships

`DatasourceRegistry` is the source of truth for configured connections and discovered schemas. It currently builds PostgreSQL and TimescaleDB adapters through the shared `Datasource` interface, which provides connection checks, structure discovery, fingerprints, sampling, read execution, statistics, and capability discovery.

The registry persists connection definitions, cached metadata, and confirmed relationships in `.business-signals/datasources.json`. This is a local-development store, not a secrets manager: it is owner-readable only and must stay out of source control. Production should replace it with managed secrets and a durable registry.

Schema discovery records tables, columns, primary and foreign keys, indexes, approximate counts, time-series metadata, representative values, and a fingerprint. Metadata is reused while a lightweight datasource fingerprint is unchanged. Controlled categorical values are separately cached per datasource/schema version.

Datasources remain independent: there is no distributed SQL or foreign-data wrapper. The registry may suggest relationships from compatible field types and sampled value overlap, but an administrator must validate and confirm a relationship before it is usable. The workflow performs a bounded lookup in one source and binds those values as a filter in the other. Lookup values are private per-investigation caches, never model-created pseudo-tables.

## Investigation workflow

`InvestigationEngine` builds a cyclic LangGraph state machine. State includes the question, selected datasources, scope, premise status, hypotheses, evidence, external findings, clarifications, resolved display entities, private query/lookup caches, budgets, and conclusion.

```text
understand ─┬─ direct answer ────────────────────────────────────► synthesize
            ├─ clarification ─► wait for person ─────────────────► resume node
            └─ causal investigation
                   │
                   ├─ generate hypotheses
                   ├─ validate factual premise
                   └─ select check ─► execute ─► interpret ─► decide ─┐
                                      ▲                 │             │
                                      └─────────────────┴─────────────┘
                                                        │
                                              external research ───────┘
                                                        │
                                                   synthesize ─► end
```

Question understanding identifies the business measure, scope, ambiguity, request type, and any factual premise behind a causal question. A genuine measure or comparison ambiguity is clarified before validation. Controlled schema vocabulary may resolve a scope term but cannot silently choose a business measure.

For causal questions, the graph validates that the stated event occurred before trying to explain it. A rejected or inconclusive premise is reported as such. Confirmed premises lead to ordinary-language hypotheses with explicit claims and evidence requirements. Evidence, actions, and failures attach to the relevant hypothesis; baseline facts and coverage limitations remain investigation-level records.

Direct answers use a bounded path: plan, validate, execute, resolve display names through discovered relationships, shape readable rows, and synthesize. Its cross-source operations are deterministic combinations of independently retrieved rows, not cross-database SQL.

Iteration, SQL-query, external-call, and elapsed-time budgets are configuration values. They can be disabled for deeper exploration. A failed query or unavailable provider becomes a recoverable failed check rather than automatically failing the conversation.

## Model-provider layer and prompts

`LLM` is the provider-neutral interface for schema-validated structured responses. The application supports OpenAI, OpenAI-compatible APIs, and Anthropic. OpenAI-compatible APIs use structured chat completions; Anthropic uses a forced tool call with a Pydantic JSON schema. Workflow code receives validated Pydantic models rather than arbitrary provider JSON.

Prompts are versioned text resources in `apps/api/src/business_signals/prompts/`. They contain stable instructions. Dynamic question, schema, evidence, approved relationships, and retained entity context are separate structured payloads. Credentials are never prompt content.

## External research and geography

Research agents are declarative entries in `config/research-agents.json`. An entry defines purpose, evidence topics, reliability, secret environment-variable names, fixed HTTP steps, templates, response paths, and output shape. The generic executor supports bounded `http_json` workflows and reusable pipeline primitives such as geocoding, boundary sampling, batching, candidate ranking, and daily-series aggregation.

The model selects a configured agent ID; it cannot supply arbitrary URLs, headers, or request templates. The coordinator matches a hypothesis's causal mechanism to agent evidence topics, then attaches resulting findings to that hypothesis. Empty, irrelevant, and failed provider responses remain non-supporting or failed checks rather than being swallowed.

`geography.py` deterministically selects a geocoding candidate, calculates boundary area, and samples bounded points for city, region, and country weather requests. Sampling density and maximum points are configuration values, not model decisions.

## Safety and trust boundaries

- Credentials remain server-side and are excluded from model payloads, API responses, browser archives, and traces.
- The model sees normalized schema metadata and approved relationships, not unrestricted database access.
- SQL must parse as exactly one read-only PostgreSQL `SELECT` or `WITH … SELECT`. Mutations, comments, locking reads, `SELECT INTO`, DB-API interpolation, unsafe limits, unknown tables, and known invalid qualified columns are rejected before execution.
- Database sessions are read-only and have row and statement-time limits.
- Cross-source lookups return one approved relationship key; application code binds it as a filter instead of executing a model-invented temporary table.
- External credentials are injected from environment variables and redacted from returned links and saved findings.
- External findings are evidence, not automatic proof of causation. Synthesis weighs directness, confidence, contradictions, missing coverage, and untested alternatives.
- SSE carries typed progress, not private chain-of-thought or raw provider reasoning.

## Observability

LangSmith is optional and non-fatal. When enabled, the service creates a root `Business Signals investigation` span with nested spans for structured LLM calls, SQL reads, and external research. If tracing fails, the investigation continues.

With `LANGSMITH_CAPTURE_CONTENT=false`, inputs and outputs are hidden. Traces retain only safe fingerprints, operational outcomes, durations, provider/model metadata, and counts. LLM spans use LangSmith's provider-neutral usage schema: `input_tokens`, `output_tokens`, and `total_tokens`. Native OpenAI-compatible, Anthropic, and Gemini-style usage fields are normalized when supplied; absent provider usage is not estimated.

## Extension points

| Need | Extension point |
| --- | --- |
| Another data system | Implement `Datasource`; isolate credentials and query semantics in the adapter. |
| Another model vendor | Implement `LLM._structured`, returning a validated response and normalized usage where supplied. |
| Another research source | Add a bounded catalog entry and environment-variable credential; do not add agent-ID conditionals to the workflow. |
| Another geographic source | Add deterministic geography primitives and compose them in catalog configuration. |
| Another UI view | Consume REST state and typed SSE events; do not recover ownership from display text. |

## Testing and evaluation

The backend suite covers query safety, datasource and relationship persistence, graph routing, clarification and resume behavior, archives, direct answers, external research, geography/weather, and observability redaction and token usage. Tests disable external telemetry even when a local `.env` enables LangSmith.

`docs/evaluation.md` defines the evaluation rubric: root-cause quality, evidence recovery, routing, causal restraint, research-agent appropriateness, and efficiency. SQL count, graph iterations, external calls, elapsed time, and model token usage can be correlated with these results in LangSmith.
