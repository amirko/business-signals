# Business Signals

An evidence-first, AI-powered investigation workbench for business anomalies. Business Signals connects independent relational and time-series databases, discovers their schemas, proposes cross-database relationships for an administrator to approve, validates the event that needs explaining, keeps competing explanations alive, chooses the next informative check, and stops only when evidence is sufficient or its configured budget is exhausted.

It is a LangGraph decision loop—not a chatbot, BI dashboard, text-to-SQL wrapper, or scripted multi-agent sequence. It also handles factual retrieval, schema discovery, safe cross-datasource lookups, resumable human clarification, and optional external research.

## What it demonstrates

- A genuinely cyclic LangGraph that selects, executes, evaluates, and reroutes investigations
- Premise validation before causal reasoning: it verifies that the reported change occurred before explaining it
- Separate direct-answer and root-cause paths, both using schema-aware display-name resolution
- Schema discovery, fingerprints, controlled-vocabulary caching, and persisted connection metadata
- Cross-database relationship candidates inferred from compatible fields and sampled values, then approved by a human
- Human-approved, bounded value lookups across sources without FDWs or distributed SQL
- Explicit hypotheses, supporting and contradicting evidence, recoverable failed checks, and calibrated confidence
- Catalog-configured historical weather and news/event research using real public APIs
- LangGraph checkpoints, human clarification interrupts, resume/follow-up conversations, and local archives
- Read-only SQL AST validation, table/column allowlists, timeouts, row caps, and database-level read-only sessions
- Typed Server-Sent Events and optional privacy-conscious LangSmith traces with token usage
- Reproducible synthetic benchmarks, including external-event and negative-control scenarios

## Experience

Ask either a factual business question or a causal investigation question:

> What was total revenue for Milan in Q3 2024?

> Why did unit sales of outdoor products in Milan fall in July 2024 compared with June?

For a factual request, the engine plans and safely executes the needed read-only query, then returns readable values rather than internal database identifiers. For a causal request, it first validates that the decline occurred. It can then test demand, availability, price, or promotion explanations in TimescaleDB; carry approved relationship values into a separate PostgreSQL catalog; and decide whether historical weather or news research is relevant evidence.

```text
CONNECT AND UNDERSTAND INDEPENDENT SOURCES

  Relational database                         Time-series database
  (for example PostgreSQL)                    (for example TimescaleDB)
           │                                           │
           └─────────────── schema discovery ─────────┘
                                  │
                   tables, fields, keys, types,
                    and representative values
                                  │
                                  ▼
              compatible-field and value-overlap checks
                                  │
                                  ▼
                  suggested cross-database relationship
                                  │
                                  ▼
                 administrator reviews, edits, approves
                                  │
                                  ▼
                    confirmed relationship is saved


ANSWER A QUESTION

  Business question
          │
          ▼
  Understand the requested measure, scope, and time period
          │
          ├── ambiguity that matters ──► ask the person ──► continue
          │
          └── request understood
                    │
                    ├── factual question
                    │       │
                    │       ▼
                    │   plan a safe read-only query
                    │       │
                    │       ├── one source ──► execute and shape the result
                    │       │
                    │       └── two sources ──► approved bounded lookup
                    │                              then safe filtered query
                    │                                      │
                    │                                      ▼
                    │                            readable factual answer
                    │
                    └── causal question (for example, “why did sales fall?”)
                            │
                            ▼
                    validate that the reported change occurred
                            │
                            ├── not confirmed ──► explain that no cause can be established
                            │
                            └── confirmed
                                    │
                                    ▼
                         form competing explanations
                                    │
                                    ▼
                  run the most useful internal or external check
                                    │
                                    ▼
                    attach supporting or contradicting evidence
                                    │
                                    └── repeat until evidence is sufficient
                                        or the configured budget is reached
                                                    │
                                                    ▼
                                   evidence-backed investigation report
```

Business Signals does not join independent databases directly. It compares compatible schema fields and representative values to propose a relationship; an administrator approves or edits it. When a query needs both sources, the application first performs a bounded read-only lookup in one source, then applies the approved values as a safe filter in the other.

## Repository layout

```text
apps/
  api/                 FastAPI + reusable LangGraph investigation engine
  web/                 React/TypeScript investigation workbench
docs/                  Architecture and evaluation notes
examples/
  data/                Versioned CSV fixtures loaded into the demo databases
  generators/          Deterministic PostgreSQL and TimescaleDB fixtures
  scenarios/           Ground-truth benchmark definitions and negative controls
  evaluate.py          Scenario result scorer
docker-compose.yml     Optional synthetic catalog and analytics demo databases
pyproject.toml         Python 3.12 package and test configuration
```

## Quick start

Requirements: Python 3.12+, Node 22.13+, and an API key for a supported AI provider. Docker is needed only to run the provided synthetic demo dataset.

```bash
cp .env.example .env
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
npm --prefix apps/web install
```

### Provided demo dataset (optional)

Run the following only if you want the supplied synthetic data and preconfigured PostgreSQL/TimescaleDB demo sources:

```bash
docker compose up -d --wait
```

This starts two demo databases and initializes them from the checked-in CSV files under `examples/data/`. The data is synthetic and safe to use for the walkthrough. Otherwise, start the API and connect your own databases through the workbench.

Add these two connections in the workbench after starting Docker:

| Name | Type | Host | Port | Database | User / password |
| --- | --- | --- | ---: | --- | --- |
| Product Catalog | PostgreSQL | `localhost` | 5432 | `catalog` | `investigator` / `investigator` |
| Sales Analytics | TimescaleDB | `localhost` | 5434 | `analytics` | `investigator` / `investigator` |

The Docker credentials are for the disposable demo only. Use a dedicated read-only database role for any real datasource.

### AI provider

Set `AI_PROVIDER` in `.env` to select the model backend. The engine always uses the same
structured-output interface, so changing providers does not change graph behavior or safety rules.

```bash
# OpenAI (default)
AI_PROVIDER=openai
AI_API_KEY=...
AI_MODEL=gpt-5-mini

# Anthropic's native Messages API
AI_PROVIDER=anthropic
AI_API_KEY=...
AI_MODEL=claude-sonnet-4-5

# Any OpenAI-compatible endpoint (for example OpenRouter, Groq, or a compatible Gemini endpoint)
AI_PROVIDER=openai_compatible
AI_API_KEY=...
AI_MODEL=provider-model-name
AI_BASE_URL=https://provider.example/v1
```

`AI_BASE_URL` is needed only for an OpenAI-compatible provider; OpenAI and Anthropic have their
own built-in endpoints. Restart the API after changing any provider setting. Provider credentials
stay local in `.env` and are never added to graph state,
API responses, events, or logs.

### Investigation limits

Investigation budgets are deployment settings, configured in `.env` rather than by client requests:

```bash
INVESTIGATION_MAX_ITERATIONS=8
INVESTIGATION_MAX_SQL_QUERIES=12
INVESTIGATION_MAX_EXTERNAL_CALLS=2
INVESTIGATION_MAX_DURATION_SECONDS=180
IGNORE_INVESTIGATION_LIMITS=false
```

Set `IGNORE_INVESTIGATION_LIMITS=true` to disable these stopping budgets for a deliberately
unbounded investigation. Read-only SQL validation and `MAX_QUERY_ROWS` still apply.

### Geographic weather coverage

Weather for a city or other known point first resolves its coordinates through Open-Meteo
geocoding. It rejects a bare ambiguous city rather than accepting the first search result. For a
country or region, it resolves a boundary polygon, generates polygon-clipped sample points at the
configured geographic density, then splits those coordinates into provider-sized batches for the
Open-Meteo historical-forecast API:

```bash
# One representative point per 2,500 km² (roughly a 50 km × 50 km cell).
TARGET_CELL_AREA_KM2=2500
# Maximum latitude/longitude pairs in one weather-provider request.
MAX_POINTS_WEATHER_API=150
```

Each research finding records its actual sampling method and point count. A store-based business
investigation should use the relevant store coordinates rather than a regional grid.

The database initialization scripts create schema and load the checked-in CSV fixtures under `examples/data/`. Those files hold internal business records only; weather and historic-event data remains an external-research concern.

Start the API and web app in separate terminals:

```bash
make api
make web
```

Open `http://localhost:3000`. Datasource definitions and discovered schemas are saved locally at `.business-signals/datasources.json` (or `DATASOURCE_STORE_PATH`) so they survive API restarts. The file is gitignored and created with owner-only permissions; it contains the credentials needed to restore local connections. Credentials are never serialized into graph state, API responses, events, logs, or model payloads. Delete a datasource in the UI to remove its saved definition and schema cache.

## API

```text
POST /api/datasources/test
POST /api/datasources
GET  /api/datasources
GET  /api/datasources/{id}/metadata   # cached schema; no database call
POST /api/datasources/{id}/refresh
DELETE /api/datasources/{id}

GET  /api/relationships
GET  /api/relationships/suggestions
POST /api/relationships/validate
POST /api/relationships
PUT  /api/relationships/{id}
DELETE /api/relationships/{id}

GET  /api/research-agents
POST /api/investigations
GET  /api/investigations                 # local saved-run archive
DELETE /api/investigations              # delete all saved runs (when none are running)
GET  /api/investigations/{id}
DELETE /api/investigations/{id}
POST /api/investigations/{id}/stop
GET  /api/investigations/{id}/events     # SSE; accepts an event offset for replay
POST /api/investigations/{id}/responses  # resume a clarification interrupt
POST /api/investigations/{id}/skip-clarification
POST /api/investigations/{id}/follow-up
```

FastAPI exposes interactive OpenAPI documentation at `http://localhost:8000/docs`.

## Core design

The model plans and interprets; code owns data access, query safety, state transitions, relationship binding, row shaping, and budget enforcement. The engine sends compact structural metadata rather than unrestricted database access, validates generated SQL as a single read-only statement, executes through the selected adapter, and stores result rows as observations. A causal investigation first validates the factual premise before it evaluates candidate causes.

PostgreSQL and TimescaleDB are separate adapter instances. Intermediate findings move through typed graph state. The `Datasource` contract is deliberately broader than SQL so ClickHouse, BigQuery, Snowflake, MongoDB, or Elasticsearch adapters can be added later without changing the investigation loop.

External specialists are bounded, configured checks rather than free-form fan-out workers. The decision node selects only agents whose declared evidence topics match a hypothesis's declared mechanism. Their outputs carry source, date range, confidence, and a `correlated` or `supporting` relationship; they cannot directly establish causation.

LLM instructions are versioned as package-local text files in `apps/api/src/business_signals/prompts/`. The loader validates and caches named prompts; runtime schema, evidence, and user context remain typed payloads in code rather than being interpolated into prompt files.

See [the architecture notes](docs/architecture.md) for trust boundaries and extension points.

## Safety

- Only one `SELECT` or `WITH … SELECT` statement is accepted.
- Data-changing and DDL AST nodes, comments, multiple statements, and `SELECT INTO` are rejected.
- Queries receive a hard maximum row count and statement timeout.
- PostgreSQL connections request `default_transaction_read_only=on`.
- Datasource credentials are saved only in the gitignored, owner-only local connection store and are excluded from LLM payloads, API responses, events, and logs.
- Every run has iteration, SQL-query, external-call, and wall-clock budgets.
- Low-evidence runs return “insufficient evidence” instead of inventing a cause.

The validator is defense in depth, not a substitute for a database role with minimal read permissions.

## Benchmarks

The checked-in scenarios cover payment regression, duplicate ingestion, supplier inventory, historical weather, a local geopolitical disruption, FX-driven margin pressure, a major technology outage, and a storm negative control. Ground truth specifies expected evidence, allowed external routes, and forbidden conclusions. Scenarios are evaluation fixtures; an external route is exercised only when a matching configured agent is available.

```bash
python examples/evaluate.py \
  examples/scenarios/03-supplier-shortage.yaml \
  result.json
```

The score rewards correct cause and evidence, datasource routing, restraint with external tools, and query efficiency. It penalizes unsupported causal language and coincidental-event attribution. See [evaluation.md](docs/evaluation.md).

## Tests

```bash
pytest
npm --prefix apps/web run lint
npm --prefix apps/web run build
```

## Current scope

Datasource registrations and their schema cache survive local API restarts. Investigation records are archived locally under `.business-signals/investigations/` (or `INVESTIGATION_STORE_DIR`), with their question, human clarifications, hypotheses, evidence, and final analysis. LangGraph checkpoints are stored separately in the catalog PostgreSQL database, under the `business_signals` schema by default (`CHECKPOINT_DATABASE_URL` and `CHECKPOINT_SCHEMA`). This preserves the exact graph position and pending interrupt, so a paused clarification can resume after an API restart without repeating earlier steps. The Saved runs view can inspect or delete one archived run or all non-running runs. It currently implements PostgreSQL and TimescaleDB, supports OpenAI, OpenAI-compatible, and Anthropic structured-output providers, and relies on live public services for optional external research. Durable encrypted credentials, organization authentication, and additional adapters are follow-on work.

## Optional integrations

### Research agents

The bundled agents cover historical weather and historic Guardian news. You can add your own research agents by configuring `config/research-agents.json` (or the path set by `RESEARCH_AGENT_CATALOG_PATH`). Each agent needs a fixed HTTPS provider configuration, clear business purpose, supported subjects, evidence topics, a bounded request/response mapping, reliability, and the name of any environment variable that contains its API key. The catalog is validated at startup; it is configuration, not executable code, so agents cannot run arbitrary code or accept model-supplied URLs.

Keep provider secrets in `.env`, never in the catalog. To enable the built-in Guardian historic-news agent, add `GUARDIAN_API_KEY=your-key` to `.env` and restart the API.

### LangSmith monitoring

Tracing is off by default. To monitor LangGraph investigations, structured LLM calls, SQL execution, and research-agent calls, add the following to the untracked `.env` file and restart the API:

```bash
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=your-key
LANGSMITH_PROJECT=business-signals-dev
LANGSMITH_ENDPOINT=https://api.smith.langchain.com  # use the regional endpoint for your project, if applicable
LANGSMITH_CAPTURE_CONTENT=false
```

With content capture disabled, traces contain only operational metadata, fingerprints, durations, outcomes, and LLM token usage—not question text, SQL, query rows, external-response bodies, or credentials. The provider layer maps native usage to LangSmith's `input_tokens`, `output_tokens`, and `total_tokens` fields when available. Monitoring failures are non-fatal and never stop an investigation.

## License

This project is source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE). It permits personal and other noncommercial use, modification, and distribution, but not commercial use. The software is provided "as is," without warranty or liability to the extent permitted by law. Commercial licensing requires separate permission from the copyright holder.
