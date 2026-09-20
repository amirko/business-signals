Build a public GitHub portfolio project: an AI-powered business root-cause investigation system that answers questions such as:
	•	Why did revenue fall last week?
	•	Why did sales decline in a specific region?
	•	Why did conversion drop after a certain date?
	•	Why did margin collapse for a product category?
	•	Why did inventory shortages suddenly appear?
The system must autonomously investigate multiple business data sources, form and test hypotheses, dynamically decide what to inspect next, correlate evidence across databases, selectively investigate real external events, and produce an evidence-backed explanation.
This project is intended to demonstrate serious agentic engineering using LangGraph.
It is NOT:
	•	a chatbot
	•	a text-to-SQL wrapper
	•	a BI dashboard
	•	a fixed ETL/reporting pipeline
	•	a pre-scripted multi-agent demo
The graph must make meaningful decisions during execution.

Core Idea
A user connects business data sources and asks:
Why did sales of outdoor products fall sharply in northern Italy during July?
The system should:
	1	understand the connected data sources;
	2	discover and cache their schemas/metadata;
	3	understand the user's business question;
	4	identify the relevant metric and period;
	5	generate multiple plausible hypotheses;
	6	decide which datasource can best test each hypothesis;
	7	query the appropriate datasource;
	8	interpret evidence;
	9	strengthen, weaken, reject, or revise hypotheses;
	10	correlate findings across multiple datasources;
	11	decide whether internal data is sufficient;
	12	selectively investigate relevant external events;
	13	ask the human for clarification when required;
	14	stop when evidence is sufficient or the investigation budget is exhausted;
	15	generate an evidence-backed final analysis.
The investigation path must be dynamic.
Do NOT implement:
schema -> sales query -> product query -> weather -> report
Instead implement:
question
   ↓
form hypotheses
   ↓
choose highest-value investigation
   ↓
choose appropriate datasource/tool
   ↓
execute
   ↓
evaluate evidence
   ↓
update hypotheses
   ↓
decide what to investigate next
   ↓
repeat / external research / HITL / finish

Repository Structure
Use a monorepo.
Suggested layout:
business-root-cause/
  apps/
    api/
      src/
      tests/

    web/
      src/
      public/

  packages/
    # optional shared/generated API contracts

  examples/
    scenarios/
    generators/

  docs/

  docker-compose.yml
  README.md
The backend and frontend should be in the same repository.
The frontend is intentionally lightweight.
The backend investigation engine is the core of the project.

Backend Tech Stack
Use:
	•	Python 3.12+
	•	FastAPI
	•	LangGraph
	•	LangChain only where useful
	•	Pydantic
	•	SQLAlchemy where appropriate
	•	PostgreSQL
	•	TimescaleDB
	•	pytest
	•	pyproject.toml
	•	an OpenAI-compatible LLM abstraction
Optional:
	•	LangSmith tracing
	•	pandas/polars for deterministic analytics
Do not make LangSmith mandatory.

Frontend Tech Stack
Create at least a working skeleton using:
	•	React
	•	TypeScript
	•	preferably Vite unless there is a strong reason for Next.js
	•	generated API types from FastAPI/OpenAPI if practical
Do not build a large frontend.
The purpose of the frontend is to visualize and control an investigation.

Architecture
The major layers should be:
React Web App
      │
      │ REST + SSE
      ▼
FastAPI
      │
      ▼
Investigation Service
      │
      ▼
LangGraph
      │
      ├── datasource tools
      ├── deterministic analytics
      ├── hypothesis management
      ├── external research
      ├── HITL
      └── reporting
The LangGraph engine must not depend on React/FastAPI concepts.
Keep the core investigation engine reusable independently.

API
Create a small clean API.
Suggested endpoints:
POST /api/datasources/test
POST /api/datasources
GET  /api/datasources
POST /api/datasources/{id}/refresh

POST /api/investigations
GET  /api/investigations/{id}
GET  /api/investigations/{id}/events
POST /api/investigations/{id}/responses
Use Server-Sent Events for investigation event streaming.
Do not use WebSockets unless clearly necessary.
HITL responses can use normal POST requests.

Datasource Model
The application must support multiple independent datasources.
For v0.1 implement:
	1	PostgreSQL
	2	TimescaleDB
Treat them as separate connections even though TimescaleDB is PostgreSQL-based.
The important challenge is cross-datasource reasoning.
Example:
PostgreSQL catalog database

products
categories
suppliers
stores
promotions
customers


TimescaleDB analytics database

sales_events
inventory_history
price_history
traffic_metrics
conversion_metrics
The investigation engine should decide which datasource to query.
Do not merge both databases using FDW or build one distributed SQL layer.
The graph itself should carry intermediate findings between sources.
Example:
TimescaleDB:
Affected product IDs = P12, P44, P82

        ↓

PostgreSQL:
All belong to supplier S17

        ↓

TimescaleDB:
Inventory for S17 products dropped before sales

        ↓

Potential supplier/logistics hypothesis
This cross-source reasoning is a central feature of the project.

Dynamic Datasource Configuration
The frontend should allow connection details to be submitted dynamically.
For example:
Datasource name: Product Catalog
Type: PostgreSQL
Host:
Port:
Database:
Username:
Password:
And separately:
Datasource name: Sales Analytics
Type: TimescaleDB
...
Credentials must never be sent to the LLM.
The backend datasource adapters alone use credentials.
For the portfolio/demo version, credentials may remain in backend memory rather than being persisted.
Document clearly that credentials are not persisted.
Prefer read-only DB credentials.

Datasource Interface
Create an abstraction conceptually similar to:
class Datasource:
    def discover_structure(...)
    def describe_entity(...)
    def sample_data(...)
    def execute_read_query(...)
    def get_statistics(...)
    def capabilities(...)
Do not assume that all future datasources will be relational.
The architecture should make future adapters possible for:
	•	ClickHouse
	•	BigQuery
	•	Snowflake
	•	MongoDB
	•	Elasticsearch
but do not implement them now.
Avoid building unnecessary generic infrastructure.

Schema Discovery
Schemas must not be hard-coded.
Implement deterministic schema discovery.
For PostgreSQL:
	•	schemas
	•	tables
	•	columns
	•	types
	•	PKs
	•	FKs
	•	indexes
	•	approximate row counts
	•	optional representative values
For TimescaleDB also identify:
	•	hypertables
	•	time columns
	•	dimensions
	•	available time ranges
	•	relevant indexes
Build structured metadata.
Do not repeatedly send complete schemas to the LLM.

Schema Cache
Cache datasource metadata.
Each datasource should conceptually have:
DatasourceMetadata:
    datasource_id
    type
    structural_metadata
    semantic_metadata
    fingerprint
    discovered_at
Separate:
STRUCTURAL METADATA
authoritative / deterministic

SEMANTIC METADATA
AI-inferred / uncertain
Examples of semantic metadata:
	•	likely order table
	•	likely revenue field
	•	likely customer dimension
	•	likely date field
	•	likely product identifier
Compute a schema fingerprint from normalized structural metadata.
When an investigation starts:
lightweight metadata check
        ↓
fingerprint
        ↓
same?
 ├── yes -> use cached schema
 └── no  -> refresh schema
Also support manual schema refresh from the UI.

Cross-Datasource Relationships
The system should be able to reason about relationships across datasources.
For example:
Timescale:
sales_events.product_id

PostgreSQL:
products.id
Represent relationships explicitly:
CrossDatasourceRelation(
    source_datasource="sales",
    source_field="product_id",
    target_datasource="catalog",
    target_field="products.id",
    confidence=...
)
Relationships may come from:
	1	explicit configuration
	2	matching names/types
	3	LLM inference
	4	human confirmation
Do not silently assume uncertain relationships.
If necessary, ask:
sales.product_id appears to correspond to catalog.products.id.
Should I treat these as the same business identifier?

Business Semantics
The LLM may infer business concepts from:
	•	table names
	•	column names
	•	sample values
	•	relationships
	•	optional business metadata
But do not fabricate meaning.
Example ambiguity:
customers.type:
A
B
C
Trigger HITL rather than guessing.
Optional metadata may define:
metrics:
  conversion:
    numerator: completed_orders
    denominator: checkout_sessions

dimensions:
  region:
    source: stores.region
The app should still work without metadata when meaning is reasonably inferable.

LangGraph State
Create strongly typed state.
Conceptually:
class InvestigationState(BaseModel):
    question: str

    datasources: list[DatasourceSummary]

    metric_definition: MetricDefinition | None

    observations: list[Observation]

    hypotheses: list[Hypothesis]

    evidence: list[Evidence]

    investigation_history: list[InvestigationStep]

    external_findings: list[ExternalFinding]

    current_focus: str | None

    human_feedback: list[HumanFeedback]

    iteration: int
    query_count: int
    external_call_count: int

    confidence: float | None

    status: InvestigationStatus

Hypothesis Model
Conceptually:
class Hypothesis(BaseModel):
    id: str
    description: str

    category: str

    confidence: float

    supporting_evidence: list[str]
    contradicting_evidence: list[str]

    status: Literal[
        "active",
        "weakened",
        "rejected",
        "supported",
        "confirmed",
        "needs_more_evidence"
    ]
The application should keep several competing hypotheses alive when appropriate.

Evidence Model
Explicitly distinguish evidence strength.
class Evidence(BaseModel):
    description: str

    source: str

    relationship: Literal[
        "direct",
        "supporting",
        "correlated",
        "contradicting"
    ]

    confidence: float
This is particularly important for external real-world events.
Do not turn temporal correlation into a causal claim.

Investigation Loop
The graph should behave roughly like:
UNDERSTAND QUESTION
        ↓
GENERATE HYPOTHESES
        ↓
SELECT HIGHEST-VALUE INVESTIGATION
        ↓
SELECT DATASOURCE / TOOL
        ↓
EXECUTE
        ↓
INTERPRET EVIDENCE
        ↓
UPDATE HYPOTHESES
        ↓
ENOUGH EVIDENCE?
   ├── no -> loop
   ├── ambiguous -> HITL
   ├── possible external factor -> external research
   └── yes -> final synthesis
There must be real cycles.
Do not implement a linear DAG.

Investigation Selection
The agent should not simply test hypotheses in order.
It should select the investigation that is likely to provide the most useful information.
For example:
H1 Price change                  confidence 0.30
H2 Stock shortage                confidence 0.27
H3 Geographic disruption        confidence 0.24
H4 Marketing effect             confidence 0.19
If one query can strongly distinguish H1 vs H2, it may be more valuable than checking H3 next.
Keep this pragmatic.
Do not implement a mathematically complex Bayesian framework unless clearly useful.

SQL and Query Safety
LLMs may propose analytical queries.
All database execution must be read-only.
Permit:
SELECT
WITH ... SELECT
Reject:
INSERT
UPDATE
DELETE
DROP
ALTER
TRUNCATE
CREATE
GRANT
Add:
	•	query timeout
	•	row limits
	•	statement validation
	•	read-only DB sessions where possible
Do not execute shell commands.

Deterministic Analytics
Do not use the LLM to calculate numbers.
Implement deterministic functions for:
	•	percentage change
	•	period-over-period comparison
	•	cohort comparison
	•	segmentation
	•	missing-data analysis
	•	duplicate detection
	•	distribution comparison
	•	simple correlation
	•	time-series anomaly detection
	•	trend changes
	•	before/after analysis
The LLM chooses what to investigate.
Code calculates the result.

External Research
External context is a major feature of this project.
However, external specialists must be invoked selectively.
Implement at least these conceptual capabilities:
Weather Research
Economic / FX Research
News / Event Research
Potential later addition:
Geopolitical Research
Do not automatically call all agents.
The graph must first decide whether internal evidence is insufficient and whether an external explanation is plausible.

External Research Routing
Conceptually:
Internal explanation found?
        │
      yes
        ↓
continue validation / finish

      no
        ↓

Evidence suggests external factor?
        │
      yes
        ↓

What category is plausible?

 ┌───────────────┬──────────────┬──────────────┐
 ↓               ↓              ↓
Weather       Economy/FX     News/Event
Examples:
Strong geographic + short-time anomaly
-> weather/event research may be useful

Imported product margin decline
-> FX/economic research may be useful

Supplier/inventory disruption
-> news/logistics/geopolitical research may be useful

Real Historical External Data
Do not fake external events in the advanced demo scenarios.
Use real historical external data.
Synthetic internal company data should be constructed around real historical events.
Examples of suitable event categories:
	•	severe weather
	•	heat waves
	•	storms
	•	wildfires
	•	blackouts
	•	wars
	•	terror attacks
	•	shipping disruptions
	•	port closures
	•	major technology outages
	•	FX shocks
	•	inflation shocks
	•	major economic changes
Potential scenario ideas:
European extreme heat
Middle East conflict
Iberian blackout
Los Angeles wildfire
Dubai flooding
Baltimore bridge collapse
CrowdStrike outage
Red Sea shipping disruption
major FX movement
Do not hard-code conclusions.
The agent must discover the temporal/geographic relationship through external research.

Weather Agent
The weather specialist should accept:
location
start_date
end_date
investigation_context
It should retrieve historical conditions such as:
	•	temperature
	•	precipitation
	•	extreme events
	•	wind
	•	snow
	•	unusual conditions
Return structured findings.
Example:
ExternalFinding(
    type="weather",
    location="...",
    period="...",
    observation="...",
    relationship="correlated",
    confidence=...
)

Economy / FX Agent
The economic specialist may retrieve:
	•	exchange rates
	•	inflation
	•	interest rates
	•	commodity prices
	•	economic indicators
Example scenario:
Imported-product margins fall
        ↓
selling prices stable
        ↓
supplier EUR price stable
        ↓
local acquisition cost rises
        ↓
agent investigates exchange rate
        ↓
large currency move found
The agent should distinguish:
direct financial mechanism
from:
general macroeconomic correlation

News / Event Agent
The event researcher should investigate questions such as:
Was there a major event affecting this region and period?
Potential events:
	•	blackout
	•	port closure
	•	transport disruption
	•	major outage
	•	war
	•	terror incident
	•	strike
	•	regulation
	•	supply-chain disruption
Require sources and dates.
Do not let news results directly become causal conclusions.
They become evidence for the main graph to evaluate.

Human-in-the-Loop
Use LangGraph interrupt/checkpointing.
HITL should occur when:
	•	metric meaning is ambiguous
	•	schema relationships are uncertain
	•	business semantics are unclear
	•	there are multiple plausible interpretations
	•	the user needs to define a business term
	•	the graph requires important contextual information
Example:
I found two plausible definitions of conversion:

1. completed_orders / sessions
2. completed_orders / checkout_sessions

Which should be used?
Frontend should render the question and allow a response.
Graph should resume afterward.

Investigation Events
The backend should emit structured events.
Examples:
InvestigationStarted
SchemaDiscovered
HypothesisCreated
HypothesisUpdated
HypothesisRejected
DatasourceSelected
QueryStarted
QueryCompleted
EvidenceFound
ExternalResearchStarted
ExternalFindingReceived
HumanInputRequired
InvestigationCompleted
InvestigationFailed
These events are streamed via SSE.
Do NOT stream raw model chain-of-thought.
Only stream concise structured user-facing reasoning.

Frontend Skeleton
Create a functional but minimal frontend.
At minimum include:
Datasource page
Allow user to add:
	•	PostgreSQL
	•	TimescaleDB
Fields:
	•	name
	•	host
	•	port
	•	database
	•	username
	•	password
Actions:
Test Connection
Add Datasource
Refresh Schema
Show:
Connected
Tables discovered: X
Schema cached

Investigation page
Allow user to:
	1	select connected datasources;
	2	enter a business question;
	3	start an investigation.
Example:
Question:

Why did outdoor-product revenue fall sharply
in northern Italy during July?

[Investigate]

Investigation Timeline
Stream structured events.
Example UI:
Hypothesis
Inventory shortage may explain the decline.

Datasource
Sales Analytics / TimescaleDB

Evidence
Inventory levels for affected products fell 47%
before the sales decline.

Confidence
0.31 -> 0.68
Later:
New finding

Affected products share supplier S17.

Datasource:
Product Catalog / PostgreSQL
Then:
External research started

Reason:
Internal inventory data suggests a supplier-side disruption.

Research:
News/Event

Hypothesis Panel
Display:
Stock shortage                 HIGH
Pricing change                 REJECTED
Regional demand decline        LOW
Supplier disruption            HIGH
No elaborate visual design is required.

HITL Panel
When backend emits:
HumanInputRequired
render the question and possible responses.

Final Result
Display:
	•	likely root cause
	•	confidence
	•	evidence
	•	rejected hypotheses
	•	external findings
	•	uncertainty/caveats
	•	investigation summary

Demo Dataset Generation
Generate synthetic business datasets with known ground truth.
Use deterministic random seeds.
Datasets should be large enough to feel realistic but manageable locally.
Example catalog DB:
products
categories
suppliers
stores
customers
promotions
Example Timescale DB:
sales_events
inventory_history
price_history
store_traffic
conversion_metrics

Benchmark Scenarios
Create several scenarios.
Not every scenario should use external research.
Scenario 1 — Internal Software / Payment Effect
Example:
conversion decline
-> platform-specific
-> payment failure
-> release metadata
No external research required.

Scenario 2 — Data Quality
Example:
revenue spike
-> duplicate event ingestion
No external research required.

Scenario 3 — Inventory / Supplier
Example:
sales decline
-> affected products
-> common supplier
-> inventory shortage
Initially internal.
May optionally lead to external research.

Scenario 4 — Real Historical Weather Event
Construct synthetic company sales around an actual severe weather event.
Business evidence might show:
physical-store traffic ↓
physical sales ↓
online sales stable / ↑
effect geographically concentrated
The weather agent should independently retrieve the real event.
The main agent should treat weather as strong supporting evidence, not automatic proof of causation.

Scenario 5 — Real Historical Economic / FX Event
Construct synthetic imported-product economics around a real currency movement.
Potential evidence:
units stable
selling price stable
supplier base price stable
local acquisition cost ↑
margin ↓
The economy/FX agent should discover the external currency move.

Scenario 6 — Real Historical Major Event
Use a real historical event such as:
	•	blackout
	•	war
	•	port closure
	•	shipping disruption
	•	major outage
Internal data should exhibit a plausible business impact.
The event agent should discover the real external event.

Negative Controls
Include scenarios where major external events occurred but are NOT the root cause.
Example:
major storm occurs
but business is fully online
and the actual cause is a software regression
The evaluation should penalize the agent for incorrectly attributing the problem to the external event.
This is important.

Evaluation Framework
Each scenario should define ground truth.
Example:
root_cause:
  category: payment_regression

expected_findings:
  - ios
  - apple_pay
  - release_4_17

expected_external_agents: []

forbidden_conclusions:
  - weather
External scenario:
root_cause:
  category: weather_disruption

expected_findings:
  - regional_store_decline
  - traffic_decline

expected_external_agents:
  - weather

expected_external_event:
  - historical weather event
Evaluate:
	•	correct root cause
	•	relevant evidence found
	•	unnecessary hypotheses rejected
	•	correct datasource routing
	•	appropriate external-agent usage
	•	unnecessary external calls
	•	unsupported causal claims
	•	total DB queries
	•	total graph iterations
	•	token usage
	•	external API calls

Stopping Conditions
Prevent infinite investigation.
Support configurable limits:
max iterations
max SQL queries
max external API calls
max total investigation duration
max token budget if practical
If evidence is insufficient, say so.
Do not invent a root cause.
Example:
A definitive root cause could not be established.

Most plausible remaining explanations:
...

Additional data required:
...

Persistence
Do not build complex persistent application storage initially.
For v0.1:
	•	datasource schema cache may use local SQLite or filesystem storage;
	•	investigation state may use LangGraph-compatible local checkpointing;
	•	credentials should not be persisted.
Keep this simple.

Docker Compose
Provide Docker Compose for local development.
At minimum:
PostgreSQL product/catalog database
TimescaleDB sales/analytics database
backend API
The frontend may run separately in development or in Docker if convenient.
Include database initialization and demo dataset loading.
A new developer should be able to run the complete demo locally with minimal setup.

Observability
Expose:
	•	graph transitions
	•	hypotheses
	•	query count
	•	selected datasource
	•	query duration
	•	external research calls
	•	token usage
	•	model cost if available
	•	investigation duration
Optional LangSmith tracing is welcome.
Do not make it required.

Security
This is a demo, but use good practices.
	•	database access should be read-only
	•	validate SQL
	•	do not expose credentials to LLMs
	•	do not return passwords through API responses
	•	redact sensitive connection data from logs
	•	apply query limits
	•	do not allow arbitrary shell execution
	•	do not allow database mutations

README
The README is a major portfolio artifact.
Include:
What this project does
Explain:
The system investigates why a business metric changed by dynamically querying multiple internal datasources and, when appropriate, independent external historical sources.
What makes it agentic
Explain why this cannot be represented as a fixed pipeline.
Why LangGraph
Show the investigation loop.
Use Mermaid:
flowchart TD
    Q[Business Question] --> H[Generate Hypotheses]
    H --> I[Select Investigation]
    I --> S[Choose Datasource / Tool]
    S --> E[Execute]
    E --> V[Evaluate Evidence]
    V --> U[Update Hypotheses]

    U --> D{Enough Evidence?}

    D -->|No| I
    D -->|Ambiguous| HITL[Human Clarification]
    HITL --> I

    D -->|External cause plausible| X[Select External Research]
    X --> W[Weather]
    X --> EC[Economy / FX]
    X --> N[News / Events]

    W --> V
    EC --> V
    N --> V

    D -->|Yes| R[Evidence-backed Final Report]
Also explain the cross-database behavior:
TimescaleDB
      ↓
discover affected products
      ↓
PostgreSQL
      ↓
discover common supplier
      ↓
TimescaleDB
      ↓
inspect inventory history
      ↓
External research

Engineering Principles
Follow these rules:
	1	Do not use LLMs for deterministic calculations.
	2	Do not expose raw model chain-of-thought. Generate and stream concise, structured, user-facing reasoning summaries explaining each hypothesis, tool selection, datasource choice, evidence interpretation, and graph-routing decision.
	3	Make graph routing genuinely dynamic.
	4	Keep graph state typed.
	5	Keep datasource adapters separate from reasoning.
	6	Treat evidence separately from conclusions.
	7	Distinguish correlation from causation.
	8	Cache schemas.
	9	Do not repeatedly send schemas/results to the model.
	10	Use external research only when justified.
	11	Allow uncertainty.
	12	Build tests before adding more agent roles.
	13	Prefer a few meaningful agents over many artificial personas.
	14	Keep the frontend thin.
	15	Optimize for technical clarity and portfolio quality.

Implementation Milestones
Milestone 1 — Monorepo + Data Layer
Build:
	•	FastAPI backend skeleton
	•	React/TypeScript frontend skeleton
	•	PostgreSQL datasource adapter
	•	TimescaleDB datasource adapter
	•	datasource connection testing
	•	schema discovery
	•	schema caching/fingerprinting
	•	Docker Compose
	•	tests
Frontend should be able to:
	•	display a datasource connection form
	•	test a connection
	•	list connected datasources
No LangGraph reasoning yet.

Milestone 2 — Investigation Core
Add:
	•	typed LangGraph state
	•	hypothesis generation
	•	investigation selection
	•	datasource selection
	•	safe SQL generation/execution
	•	deterministic analytics
	•	evidence evaluation
	•	hypothesis update/rejection
	•	looping/stopping logic
Get one internal scenario working end-to-end.

Milestone 3 — Streaming UI
Add:
	•	structured investigation events
	•	SSE endpoint
	•	investigation timeline UI
	•	hypothesis panel
	•	final result panel
Do not spend significant time on styling.

Milestone 4 — HITL
Add:
	•	ambiguity detection
	•	LangGraph interrupts/checkpointing
	•	frontend clarification panel
	•	resume workflow

Milestone 5 — Cross-Database Scenarios
Add:
	•	realistic catalog + sales datasets
	•	cross-source relationship discovery/configuration
	•	supplier/inventory scenario
	•	benchmark evaluation

Milestone 6 — Real External Research
Add:
	•	weather provider
	•	economic/FX provider
	•	news/event researcher
	•	supervisor routing
Create at least one real historical weather scenario.
Create at least one real historical economy/FX scenario.
Do not mock these for the actual showcase demo.
Mocks may still be used in unit tests.

Milestone 7 — Evaluation and Polish
Add:
	•	multiple benchmark scenarios
	•	negative controls
	•	evaluation harness
	•	token/cost reporting
	•	optional LangSmith
	•	README
	•	architecture documentation
	•	clean screenshots/demo instructions

First Task
Start with Milestone 1 only.
Specifically:
	1	inspect the current repository;
	2	propose the final monorepo directory structure;
	3	document architectural assumptions;
	4	create FastAPI backend skeleton;
	5	create React/TypeScript frontend skeleton;
	6	add PostgreSQL datasource connection support;
	7	add TimescaleDB datasource connection support;
	8	implement deterministic schema discovery;
	9	implement schema fingerprint/cache;
	10	add Docker Compose with PostgreSQL and TimescaleDB;
	11	add tests;
	12	make the frontend capable of testing and displaying datasource connections;
	13	update README with exact local development instructions.
	14	Save this prompt.
Do NOT implement LangGraph yet.
Do NOT implement external agents yet.
Do NOT add authentication, billing, cloud deployment, or unrelated product infrastructure.
Keep Milestone 1 small, clean, runnable, tested, and appropriate for a public GitHub portfolio repository.
