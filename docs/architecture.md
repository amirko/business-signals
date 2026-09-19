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

## Cross-source reasoning

Datasources remain independent. The graph may discover affected product identifiers in TimescaleDB, resolve those products to a supplier in PostgreSQL, and return to TimescaleDB to test whether supplier inventory led the observed sales change. No FDW or distributed query layer is used.

## Extending adapters

New adapters implement `Datasource` without changing the graph. The interface exposes structure discovery, entity description, sampling, read execution, statistics, and capability flags, and therefore does not assume that future sources are relational.
