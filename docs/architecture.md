# Architecture

Business Signals separates orchestration from transport and presentation:

```text
React workbench ── REST + SSE ── FastAPI ── InvestigationService
                                               │
                                               ▼
                                        cyclic LangGraph
                                  ┌────────────┼────────────┐
                             datasource    analytics    external research
                              adapters    functions      specialists
```

The graph is intentionally cyclic. After every query, it evaluates the evidence and chooses among another internal investigation, a targeted external lookup, a human clarification interrupt, or synthesis. The choice depends on active hypotheses, evidence strength, information gain, ambiguity, and remaining budget.

## Trust boundaries

- Connection secrets exist only inside the in-memory datasource registry.
- Model context receives normalized schema metadata, never credentials.
- SQL is parsed before execution and must be a single read-only `SELECT`.
- Database sessions request read-only mode and impose time and row limits.
- External findings are labeled as correlated or supporting evidence, not causation.
- SSE contains concise structured events; private model reasoning is never streamed.

## Prompt catalog

Reusable LLM instructions live in `apps/api/src/business_signals/prompts/`, one versioned text file
per task such as question understanding, query planning, evidence interpretation, synthesis, and
external research selection. `prompt_catalog.py` validates prompt names and loads package resources.
The graph provides changing facts—question, schema metadata, evidence, and approved relationships—as
structured payloads, keeping user data out of static instruction files.

## Investigation package

`apps/api/src/business_signals/investigation/` owns the LangGraph workflow. `workflow.py` contains
graph wiring and the cyclic investigation path; `direct_answers.py` contains factual retrieval,
entity resolution, inherited filters, and result shaping; and `external_research.py` coordinates
bounded outside evidence. This lets workflow concerns grow independently without changing API
callers or saved conversation compatibility.

## Research-agent extension point

`config/research-agents.json` is validated at startup. Simple agents use declarative `http_json`
workflows: fixed HTTPS steps, parameter templates, optional API-key environment variables, and
fixed JSON response mappings. A `pipeline` runner can compose a bounded set of reusable primitives
such as HTTP calls, candidate selection, boundary sampling, batching, and daily-series aggregation.
The runner never dispatches on an agent ID; provider URLs, request formats, JSON paths, and output
templates stay in the catalog. Catalog entries cannot execute user code or accept a URL, header, or
query parameter from the model at runtime, avoiding SSRF and secret-exfiltration paths.

Each agent declares `evidence_topics`, such as `weather.conditions` or `currency.exchange`. A
hypothesis declares the mechanism it needs with the same vocabulary. The external-research
coordinator intersects those declarations before planning and validates the pairing again before
execution. Thus a weather hypothesis cannot select an FX agent, and adding an agent is a catalog
change rather than a workflow-code change. Baseline facts and coverage caveats are stored as
investigation-level evidence; only tagged mechanism evidence may appear beneath a causal hypothesis.

## Cross-source reasoning

Datasources remain independent. The graph may discover affected product identifiers in TimescaleDB, resolve those products to a supplier in PostgreSQL, and return to TimescaleDB to test whether supplier inventory led the observed sales change. No FDW or distributed query layer is used.

## Extending adapters

New adapters implement `Datasource` without changing the graph. The interface exposes structure discovery, entity description, sampling, read execution, statistics, and capability flags, and therefore does not assume that future sources are relational.
