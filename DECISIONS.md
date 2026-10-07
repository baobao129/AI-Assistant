# Architecture Decisions

Status: Draft for Final Consistency Review  

This file records the rationale and consequences of approved architecture decisions. Core runtime facts remain in [docs/contracts/](docs/contracts/) and are not redefined here.

## ADR-001

### Title

Use a Contract-First architecture source of truth

### Status

Accepted

### Context

The project spans LangGraph State, Graph topology, Tools, APIs, SSE, persistence, security, and frontend behavior. Duplicating these facts across project documents would allow drift.

### Decision

Maintain stable, dependency-ordered Contracts:

```text
TERMINOLOGY -> STATE -> TOOL -> GRAPH -> API
```

Project and Coding Agent documents reference these Contracts rather than redefining their schemas or topology.

### Consequences

Core changes require impact analysis and synchronized updates. Documentation generation and implementation reviews have a clear authority hierarchy.

### Related Contracts

- [TERMINOLOGY.md](docs/contracts/TERMINOLOGY.md)
- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md)
- [TOOL_CONTRACT.md](docs/contracts/TOOL_CONTRACT.md)
- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md)
- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md)

## ADR-002

### Title

Separate Conversation Persistence from Graph State and Checkpoint

### Status

Accepted

### Context

User-visible chat history, resumable execution state, and event delivery have different consumers, retention needs, and security boundaries. Treating them as one record would expose internal execution details or make recovery depend on presentation data.

### Decision

Conversation Persistence stores user-visible messages, citations, Tool summaries, approvals, and final outcomes. Graph State carries bounded execution data. Checkpoint persists resumable execution state for Interrupt/Resume. Application Events are delivery/observation records.

### Consequences

The system has separate repositories and lifecycles. Correlation uses `conversation_id`, `thread_id`, and `run_id`, but none of those boundaries may be collapsed.

### Related Contracts

- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md)
- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md)
- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md)

## ADR-003

### Title

Use rule-directed Workflows with bounded Agents

### Status

Accepted

### Context

The platform has deterministic data and safety flows as well as dynamic model decisions. A single unrestricted Agent would make security, testing, and execution behavior difficult to control.

### Decision

Use rule-directed Workflows for explicit application control flow and bounded Agents for dynamic decisions. The SQL Agent is limited by configured Tool iterations and repair attempts. Conditional routing predicates are not business Nodes.

### Consequences

Graph topology remains inspectable and testable. Dynamic model behavior stays within Tool, State, security, and termination boundaries.

### Related Contracts

- [TERMINOLOGY.md](docs/contracts/TERMINOLOGY.md)
- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md)
- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md)

## ADR-004

### Title

Use separate Knowledge, Structured Data, and General paths

### Status

Accepted

### Context

Enterprise document retrieval, structured business data, and general conversation have different evidence, tools, validation, and security requirements. Silently combining or selecting one source for a mixed request would produce misleading answers.

### Decision

Route MVP requests to `knowledge`, `structured_data`, or `general` Intent paths. Mixed Knowledge/Structured Data requests produce an explicit unsupported Routing Outcome or follow-up guidance. The General path does not silently call enterprise data Tools.

### Consequences

MVP behavior is predictable and easier to validate. Multi-source planning is deferred to a future Contract and Architecture Review.

### Related Contracts

- [TERMINOLOGY.md](docs/contracts/TERMINOLOGY.md)
- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md)
- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md)

## ADR-005

### Title

Use a layered SQL security boundary

### Status

Accepted

### Context

Prompt instructions and string matching are insufficient to protect a database from unsafe, expensive, or data-exfiltrating queries.

### Decision

All SQL generated or requested by the SQL Agent follows:

```text
LLM -> database_query Tool -> SQL Validator -> read-only Database
```

The boundary combines input validation, AST validation, statement and schema/table restrictions, dangerous-function restrictions, complexity and result limits, timeout, and a read-only account.

Structured Data Preview is a separate controlled Application/API read through an authorized Data Source Adapter or Repository using parameterized read-only queries. It does not accept arbitrary SQL and is outside the Agent Tool Calling Loop, while retaining authorization, read-only credentials, limits, and resource controls.

### Consequences

SQL Agent implementation is more constrained but defensible and testable. The project does not expose arbitrary SQL execution through HTTP.

### Related Contracts

- [TOOL_CONTRACT.md](docs/contracts/TOOL_CONTRACT.md)
- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md)
- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md)

## ADR-006

### Title

Use Milvus Standalone

### Status

Accepted

### Context

Milvus Cluster introduces substantial infrastructure and message-system requirements that are unsuitable for the Windows Docker Desktop MVP.

### Decision

Deploy Milvus Standalone with the compatible official Compose arrangement. Do not introduce Milvus Cluster, Kafka, or Pulsar.

### Consequences

The runtime is smaller and easier to demonstrate. Capacity and high availability are outside MVP. SDK and image versions must be verified before implementation.

### Related Contracts

- [TERMINOLOGY.md](docs/contracts/TERMINOLOGY.md)
- [PROJECT_SPEC.md](docs/PROJECT_SPEC.md)
- [AGENTS.md](AGENTS.md)

## ADR-007

### Title

Keep Parser Provider Behind an Adapter

### Status

Accepted

### Context

MinerU may be operationally heavy and its deployment mode has not been implemented in the current repository. Binding the system directly to its SDK could block development and restrict file formats.

### Decision

Use a `DocumentParser`/Parser Adapter boundary with MinerU as the planned formal Parser and a lightweight development Parser fallback. Deployment and connection details are implementation-stage concerns.

### Consequences

The RAG chain can be validated before MinerU is fully deployed. Parser replacement remains possible without changing Domain or Application Contracts.

### Related Contracts

- [TERMINOLOGY.md](docs/contracts/TERMINOLOGY.md)
- [PROJECT_SPEC.md](docs/PROJECT_SPEC.md)
- [AGENTS.md](AGENTS.md)

## ADR-008

### Title

Use Memory and SQLite Checkpoint modes for MVP

### Status

Accepted

### Context

LangGraph Checkpoint support is version-sensitive, and an official MySQL Checkpointer must not be assumed merely because MySQL is used for business data.

### Decision

Support in-memory and SQLite persistent Checkpoint modes behind an adapter/configuration boundary. A MySQL Checkpoint implementation requires a separate design and ADR.

### Consequences

MVP avoids an unverified custom persistence implementation while still demonstrating Interrupt/Resume and restart-aware execution. Conversation Persistence remains independent of Checkpoint.

### Related Contracts

- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md)
- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md)
- [PROJECT_SPEC.md](docs/PROJECT_SPEC.md)

## ADR-009

### Title

Use persisted BackgroundTasks for ingestion MVP

### Status

Accepted

### Context

Document parsing, chunking, embedding, and vector writes should not block an HTTP request, but a personal MVP should avoid a distributed queue before the workload requires it.

### Decision

Use FastAPI BackgroundTasks with a persisted MySQL `ingestion_jobs` lifecycle. Mark stale `running` Jobs as `failed` during startup recovery. Retry explicitly through `POST /api/v1/ingestion-jobs/{ingestion_job_id}/retry`: only failed Jobs are retryable, the original remains historical, and a new Job ID with an incremented attempt is created for the same document/index-version intent. The operation is idempotent; pending, running, and success Jobs are rejected. No queue or broker is introduced.

### Consequences

The MVP remains operationally small and restart behavior is explicit. BackgroundTasks are not treated as a durable distributed worker system; future scaling requires an Architecture Review.

### Related Contracts

- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md)
- [PROJECT_SPEC.md](docs/PROJECT_SPEC.md)
- [AGENTS.md](AGENTS.md)

## ADR-010

### Title

Use SSE as the primary streaming protocol

### Status

Accepted

### Context

The Chat UI needs tokens, Agent and Tool progress, citations, approvals, final results, and errors. SSE is sufficient for server-to-client event delivery and simpler than a bidirectional socket for MVP.

### Decision

Use SSE with a stable Event Envelope, event IDs, per-Run sequence starting at 1, terminal events, replay via `Last-Event-ID`, and client deduplication by `event_id`. Approval decisions use ordinary HTTP endpoints.

### Consequences

Frontend reconnect and replay behavior must be tested. WebSocket is not a primary MVP dependency.

### Related Contracts

- [TERMINOLOGY.md](docs/contracts/TERMINOLOGY.md)
- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md)
- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md)

## ADR-011

### Title

Use capability-based LLM provider selection

### Status

Accepted

### Context

Local and Remote LLMs may differ in streaming, structured output, and Tool Calling. Selecting behavior only by provider name would create false capability assumptions.

### Decision

Declare and check `streaming`, `structured_output`, and `tool_calling` independently. Remote mode is the complete-capability default; Local mode is opt-in and may be used only for supported paths.

### Consequences

Provider adapters need capability metadata and explicit unsupported behavior. SQL Agent and structured paths cannot silently run on an incapable Local model.

### Related Contracts

- [TERMINOLOGY.md](docs/contracts/TERMINOLOGY.md)
- [PROJECT_SPEC.md](docs/PROJECT_SPEC.md)
- [AGENTS.md](AGENTS.md)

## ADR-012

### Title

Use application registry for active index versions

### Status

Accepted

### Context

Milvus is a vector execution store, while Document lifecycle, deletion, and Re-index need transactional application status and promotion semantics.

### Decision

Store active Document/index-version metadata in an application registry. Promote a new version only after validation, then retire the previous active version and clean its vectors.

### Consequences

Document APIs and retrieval resolution consult the registry. Vector metadata supports lookup but is not authoritative for active status.

### Related Contracts

- [TERMINOLOGY.md](docs/contracts/TERMINOLOGY.md)
- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md)
- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md)

## ADR-013

### Title

Use explicit HITL for high-risk data export

### Status

Accepted

### Context

The project needs a meaningful, demonstrable Human-in-the-loop flow without implementing a full enterprise approval platform or allowing unreviewed export side effects.

### Decision

Use `data_export` as a high-risk Tool. It creates an Approval Request, enters Interrupt/Checkpoint, emits `approval_required`, and executes only once after an idempotent Approve. Reject and Expire produce no export side effect.

The waiting Run remains `awaiting_approval`. Approve preserves `original_run_id`, marks the original Run terminal `handoff_complete`, and creates a successor Run with a new `run_id`, the same Thread and Checkpoint lineage, and a new SSE sequence; the API returns `successor_run_id` and its stream URL. Reject marks the original Run terminal `rejected` and creates no execution successor. Expiry marks it terminal `expired` and cannot Resume. The `approval_required` stream may close while waiting and that close is not final business completion.

### Consequences

The MVP demonstrates approval semantics with a controlled destination and may simulate the final Artifact. Full RBAC and workflow administration remain out of scope.

### Related Contracts

- [TOOL_CONTRACT.md](docs/contracts/TOOL_CONTRACT.md)
- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md)
- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md)

## ADR-014

### Title

Keep MVP testing focused and acceptance-driven

### Status

Accepted

### Context

The project needs meaningful verification without turning a personal MVP into a full production test program or manufacturing low-value coverage.

### Decision

Require core Unit Tests, targeted Integration/API Tests, and a small set of critical E2E/Smoke Tests, plus a manually executed Evaluation Dataset/Script/Report. Phase acceptance and the clean Windows/Docker Demo startup gate are mandatory.

### Consequences

Testing effort follows risk and contract boundaries. Complete production-grade E2E coverage and CI-blocking evaluation are deferred.

### Related Contracts

- [PROJECT_SPEC.md](docs/PROJECT_SPEC.md)
- [MASTER_AGENT_PROMPT.md](docs/MASTER_AGENT_PROMPT.md)
- [AGENTS.md](AGENTS.md)

## ADR-015

### Title

Use Windows + Docker Desktop as the official runtime

### Status

Accepted

### Context

The project must be reproducible on the target development environment without depending on services installed on the host.

### Decision

Windows with Docker Desktop is the official runtime. MySQL 8.x runs in Docker with Volume persistence. Host MySQL is not a project dependency.

### Consequences

Compose configuration and startup documentation must provide the database service and volume. Container communication uses the Compose network and service names.

### Change Conditions

Changing the official runtime, database deployment model, or persistence boundary requires Architecture Review and an ADR update.

### Related Contracts

- [PROJECT_SPEC.md](docs/PROJECT_SPEC.md)
- [AGENTS.md](AGENTS.md)

## ADR-016

### Title

Bound the SQL Agent Tool Loop and separate retry semantics

### Status

Accepted

### Context

An unrestricted Agent Loop can consume resources indefinitely and obscure the difference between normal Tool continuation and recovery from an error.

### Decision

Use configuration values `max_tool_iterations = 8`, `max_repair_attempts = 3`, and `max_answer_revisions = 1`. Normal Tool Calling Loop continuation, error Retry, and Answer Validation revision are separate behaviors and budgets; deterministic invalid/routing/mixed safe outcomes bypass ordinary validation, while ordinary model answers require it.

### Consequences

The SQL Agent has deterministic termination and observable failure paths. Security, permission, and dangerous-operation failures cannot be repaired by retry.

### Change Conditions

Changing the limits or merging normal Loop and Retry semantics requires Graph and Tool Contract review.

### Related Contracts

- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md)
- [TOOL_CONTRACT.md](docs/contracts/TOOL_CONTRACT.md)

## ADR-017

### Title

Do not execute mixed Knowledge and Structured Data queries in MVP

### Status

Accepted

### Context

A single request that requires both document evidence and structured data needs orchestration not included in the MVP Graph.

### Decision

MVP does not support one request executing both Knowledge and Structured Data paths. A mixed request must produce an explicit unsupported or safe outcome and must not silently execute only one side.

### Consequences

Behavior is honest and testable. Multi-source planning remains a future architecture extension.

### Change Conditions

Supporting mixed execution requires a new Graph/State/API design and Architecture Review.

### Related Contracts

- [TERMINOLOGY.md](docs/contracts/TERMINOLOGY.md)
- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md)
- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md)

## ADR-018

### Title

Use Minimal Authentication for MVP

### Status

Accepted

### Context

The application needs authenticated resource ownership without taking on a complete enterprise identity and authorization platform.

### Decision

Provide Login, Logout, and Current User operations. Do not implement complete RBAC, OAuth, or SSO. Keep the resource and identity boundaries extensible for future RBAC.

### Consequences

Protected APIs can identify the current user while permission policy remains deliberately minimal. Introducing organization-wide identity or role policy requires a later decision.

### Change Conditions

Adding OAuth, SSO, or full RBAC requires Architecture Review and API/security impact analysis.

### Related Contracts

- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md)
- [PROJECT_SPEC.md](docs/PROJECT_SPEC.md)

## ADR-019

### Title

Manage Document, Ingestion Job, and Index Version lifecycles explicitly

### Status

Accepted

### Context

Document replacement and deletion must not make retrieval point at partially indexed data or lose the last known-good version.

### Decision

Use stable `document_id`, `ingestion_job_id`, and `index_version` identities. A new version is parsed, indexed, and validated before promotion; only then is the old active version retired. Active index-version metadata is managed by the application registry.

### Consequences

Re-index can fail without taking the existing active version offline. Delete, retry, and startup recovery can be made idempotent and observable.

### Change Conditions

Changing promotion, deletion, or registry authority requires State/API/Data Architecture Review.

### Related Contracts

- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md)
- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md)
- [TERMINOLOGY.md](docs/contracts/TERMINOLOGY.md)

## ADR-020

### Title

Keep real Secrets out of repository and runtime artifacts

### Status

Accepted

### Context

The project uses provider credentials, database passwords, session secrets, and other sensitive configuration that must not enter source control or user-visible diagnostics.

### Decision

Real API Keys, Tokens, Passwords, JWT Secrets, and equivalent credentials must not appear in source, tests, fixtures, README files, logs, screenshots, or Git. Use `.env`, `.env.example` placeholders, and Docker environment configuration.

### Consequences

Setup documentation must describe safe configuration without publishing credentials. Logging and test fixtures require redaction and secret scanning.

### Change Conditions

Any exception requires an explicit security review; convenience is not a valid reason to embed a Secret.

### Related Contracts

- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md)
- [TOOL_CONTRACT.md](docs/contracts/TOOL_CONTRACT.md)
- [AGENTS.md](AGENTS.md)

## ADR-021

### Title

Verify External API and SDK behavior against actual versions

### Status

Accepted

### Context

LangGraph, LangChain, FastAPI, Pydantic, Milvus, MinerU, LLM, and Embedding APIs are version-sensitive. Guessing APIs from memory creates avoidable runtime failures.

### Decision

Use the project's actual locked or installed version and its corresponding official documentation as the authority for version-sensitive behavior. Verify before implementation and record any dependency change and its impact.

### Consequences

Implementation may require an environment inspection step, but compatibility is based on evidence rather than assumptions.

### Change Conditions

Changing a core dependency or relying on a different major API requires Architecture Review and a documented migration decision.

### Related Contracts

- [PROJECT_SPEC.md](docs/PROJECT_SPEC.md)
- [MASTER_AGENT_PROMPT.md](docs/MASTER_AGENT_PROMPT.md)
- [AGENTS.md](AGENTS.md)

## ADR-022

### Title

Require a fresh-environment Demo startup path

### Status

Accepted

### Context

The project must be demonstrable and transferable, not dependent on undocumented local state.

### Decision

The final target is a fresh Windows + Docker Desktop environment with only the necessary `.env` prepared. Following the README commands must start infrastructure, initialize databases, initialize Demo data and the Knowledge index, and enter a usable Demo.

### Consequences

README commands, initialization scripts, Compose configuration, health checks, and smoke tests must be maintained as one acceptance path. Local manual setup that is not documented is not sufficient evidence.

### Change Conditions

Changing the clean-environment gate or required initialization sequence requires Project Specification and acceptance review.

### Related Contracts

- [PROJECT_SPEC.md](docs/PROJECT_SPEC.md)
- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md)
- [AGENTS.md](AGENTS.md)

## ADR-023

### Title

Add the Answer Validation revision counter and the offline development LLM/Embedding profiles

### Status

Accepted

### Context

`GRAPH_CONTRACT.md` (Graph-005 / Graph-008) requires `max_answer_revisions = 1` to bound the
Answer Validation revision path for one answer candidate, but the State Contract field table
does not enumerate a counter field for it. The MVP also needs a way to demonstrate and verify
the Knowledge, Structured Data, HITL, and SSE paths on a machine with no provider credential.

### Decision

1. Add the single candidate-local counter `answer_revision_count` to Graph State, owned by the
   Answer Validation policy and consumed only by the Graph validation routing predicate. It
   implements the already-contracted `max_answer_revisions = 1` bound; it does not add a new
   budget or merge the three independent counters.
2. Add explicitly opt-in development profiles: `EMBEDDING_PROVIDER=deterministic` for
   reproducible vectors and `LLM_PROVIDER=extractive` for a reader that can only quote
   sentences from the authorized context. Both are selected by configuration only, never
   inferred, and neither introduces pretrained enterprise facts. `SQL_AGENT_PROVIDER=scripted`
   selects the deterministic Structured Data model for offline runs.

### Consequences

Acceptance runs are reproducible without external providers, and the Answer Validation
revision loop is bounded by a declared State field. Production-like behavior still requires the
remote profiles. No Core Contract limit, topology, Tool schema, or API shape changes.

### Change Conditions

Formalizing `answer_revision_count` in `STATE_CONTRACT.md`, or changing the revision budget,
requires State/Graph Contract review under the Contract Change Protocol.

### Related Contracts

- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md)
- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md)
- [PROJECT_SPEC.md](docs/PROJECT_SPEC.md)

## ADR-024

### Title

Use sqlglot for SQL AST validation and python-multipart for the upload endpoint

### Status

Accepted

### Context

`TOOL_CONTRACT.md` TOOL-003 requires AST validation, a statement allowlist, multi-statement
blocking, schema/table restriction, dangerous-function restriction, complexity controls, and
LIMIT enforcement before any database access. String matching cannot satisfy that boundary.
`API-007` requires `multipart/form-data` on the Document upload endpoint.

### Decision

- Add `sqlglot==27.9.0` (verified installed version) as the SQL parser for the SQL Validator.
  It parses the statement, and the Validator decides allowlist, scope, complexity, LIMIT, and
  dangerous-function policy. sqlglot is used only inside the Validator boundary.
- Add `python-multipart==0.0.20` (verified installed version) so FastAPI can parse the upload
  form. It performs no business logic.

### Consequences

The SQL boundary is enforced on a real AST rather than on text patterns. Both dependencies are
ordinary supporting libraries with recorded reasons, versions, and impact; neither changes
Core Contracts, State, Graph topology, Tool schemas, or API shapes.

### Change Conditions

Replacing the SQL parser or the multipart parser, or adding a new core dependency, requires
Architecture Review and a documented migration decision.

### Related Contracts

- [TOOL_CONTRACT.md](docs/contracts/TOOL_CONTRACT.md)
- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md)
- [AGENTS.md](AGENTS.md)

## ADR-025

### Title

Give `max_repair_attempts` a bounded, declared execution path in the Graph

### Status

Accepted

### Context

`GRAPH_CONTRACT.md` Graph-005 and Graph-009 require three independent counters, including
`max_repair_attempts = 3` for "same-question automatic repairs within one Run", owned by the
Graph/Application retry policy. The counter existed in `ExecutionBudgets` but nothing consumed
it, so the budget was documentation rather than behavior.

### Decision

1. The Graph owns a bounded repair step. A repair-eligible failure leaves the path, consumes
   exactly one repair attempt, and re-enters the declared stage that failed:
   `knowledge -> retrieval`, `structured_data -> model_node`, `general -> general_chat`.
2. Only genuinely transient failures are repair-eligible: `llm_unavailable` and
   `retrieval_unavailable` (the latter normalizes transient embedding/vector-store failures).
   Security rejections, permission failures, unsafe SQL, approval outcomes, validation
   outcomes, and completed side effects are never repair-eligible and stay terminal.
3. The counter is carried inside the bounded `error` envelope as `error.repair_attempts`, so
   no new Graph State field is introduced, the counter survives Resume on the same Thread, and
   a new Answer Validation candidate cannot reset it. Tool Loop iterations and Answer
   Validation revisions remain separate budgets.
4. Transient Tool-level failures remain owned by each Tool's own retry policy; the Graph
   repair path never overrides a Tool decision.

### Consequences

The second mandated counter is enforced and observable. A Run with a transient provider or
retrieval failure retries within budget instead of failing immediately, and every non-transient
outcome keeps its previous terminal behavior. `graph/main.py` gained the `failure`,
`is_recoverable`, `repair_count`, and `repair_guard` helpers and one `repair_guard` node with
one conditional edge. No Core Contract limit, Tool schema, or API shape changes.

### Change Conditions

Changing which failures are repair-eligible, increasing the repair budget, or merging the
repair budget with the Tool Loop or Answer Validation budgets requires Graph and Tool Contract
review.

### Related Contracts

- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md)
- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md)
- [TOOL_CONTRACT.md](docs/contracts/TOOL_CONTRACT.md)

## ADR-026

### Title

Register `answer_revision_count` in the State Contract (CC-STATE-001)

### Status

Accepted

### Context

`GRAPH_CONTRACT.md` Graph-005 / Graph-008 require the bounded revision counter for one answer
candidate, and ADR-023 had already introduced `answer_revision_count` in Graph State while
noting that `STATE_CONTRACT.md` did not enumerate the field in its table. ADR-023 recorded the
required follow-up as a State/Graph Contract review under the Contract Change Protocol
(suggested Change ID `CC-STATE-001`) instead of silently editing the Contract.

### Decision

Apply `CC-STATE-001` in `STATE_CONTRACT.md`:

1. Add the `answer_revision_count` row to Graph Execution State with its producer, consumer,
   mutator, candidate-local lifecycle, and Checkpoint persistence documented against the
   implementation (`should_revise` routing predicate, `revision_update` controlled increment,
   `ExecutionBudgets.can_revise` bound).
2. Reference the field in the independent-counters paragraph so the declared
   `max_answer_revisions = 1` budget and its State carrier are stated in one place.
3. Record the Change ID, impact analysis, affected documents, tests, and consistency review
   in the Contract's Change Record section.

### Consequences

The Contract, Graph Contract, and implementation now agree on the revision counter without any
runtime, topology, Tool schema, API shape, or budget change. The previously recorded
documentation gap is closed; `tests/integration/test_graph_paths.py` and
`tests/integration/test_repair_budget.py` continue to cover the bounded counter behavior.

### Change Conditions

Changing the revision budget, reassigning ownership of the counter, or letting candidate
regeneration create additional revision budget requires State/Graph Contract review and an
updated Change Record entry.

### Related Contracts

- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md)
- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md)
- [AGENTS.md](AGENTS.md)
