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

## ADR-027

### Title

Make Run terminal transitions atomic, CAS-guarded, and side-effect-free for losing executors

### Status

Accepted

### Context

A Run's terminal status and its terminal event (`final` / terminal `error`) were written in
separate units of work, `_finalize_run` wrote the assistant Conversation message _before_ the
terminal status was committed, and terminal updates carried no `required_status` condition. A
stale executor (for example the original executor after the recovery flow re-dispatched the same
successor) could therefore overwrite a newer terminal state, publish a second terminal event,
write a duplicate assistant message, or leave `awaiting_approval` unguarded.

### Decision

1. Every terminal transition is applied through `backend/application/terminal.py`
   (`apply_terminal_transition` / `record_terminal_transition`) inside one
   `MySQLConnectionPool.transaction()`: conditional Run status update (`required_status`, e.g.
   `running`, `awaiting_approval`, `accepted`) plus the terminal event row with a deterministic
   `event_id` (uuid5 of `run_id`, `event_type`, `logical_transition`). Fan-out happens after commit.
2. The assistant Conversation message and the Conversation timestamp are written with the
   cursor-based repository methods (`add_message_with_cursor`, `touch_with_cursor`) inside that
   same transaction, and only when the CAS was won.
3. Losing the CAS produces no side effect at all: the caller returns the current authoritative
   status without writing a message, an event, or a conversation touch.
4. `awaiting_approval` is also a CAS transition from `running`, so an already-terminal Run cannot
   be pulled back into the waiting state.
5. `RunService.fail_run_safely()` is the single safe-terminalization entry point for abnormal
   paths (dispatcher exception, unschedulable Run); it is CAS-guarded and idempotent.

### Consequences

Terminal effects (status, closing event, assistant message) are written at most once per Run, and
a stale executor cannot overwrite a newer terminal state. Duplicate non-terminal work (for example
a repeated Tool Call) remains possible only when the execution lease is misjudged; that residual
window is addressed by ADR-032 and limited by the export-level idempotency constraints.

### Change Conditions

Changing the terminal-transition entry point, the CAS status set, or moving Conversation writes
outside the terminal transaction requires State, Graph, and API Contract review.

### Related Contracts

- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md) (`CC-STATE-002`)
- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md) (`CC-GRAPH-001`)
- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md) (`CC-API-001`)
- [AGENTS.md](AGENTS.md)

## ADR-028

### Title

Count the crash-recovery budget from persisted `agent_start` events published by both `execute()` and `resume()`

### Status

Accepted

### Context

Bounded successor recovery counts a Run's persisted `agent_start` events to decide whether the
`RUN_MAX_RECOVERY_ATTEMPTS` budget is exhausted. `RunService.execute()` published `agent_start`,
but `RunService.resume()` did not, so a real crash-recovery cycle never increased the count:
`RUN_MAX_RECOVERY_ATTEMPTS` could never trigger and a repeatedly crashing successor could loop
`running -> accepted -> running` indefinitely. The previous test inserted the events by hand and
therefore never covered the real path.

### Decision

1. `RunService.resume()` publishes `agent_start` immediately after winning
   `claim_for_execution()`, exactly like `execute()`.
2. The count of persisted `agent_start` events for a Run is the single source of truth for
   execution attempts; `RUN_MAX_RECOVERY_ATTEMPTS = 2` counts total attempts, including the first
   execution and every recovery execution.
3. When the budget is exhausted, the maintenance worker terminates the successor as
   `execution_interrupted` (terminal `error`, `required_status="running"`) instead of releasing it
   again, so recovery is finite.

### Consequences

Crash recovery is genuinely bounded and observable. Tests must exercise the real `resume()` path
(no synthetic events); `tests/integration/test_hitl_faults_live.py::test_real_resume_crash_consumes_the_recovery_budget_and_stops`
covers it.

### Change Conditions

Changing the attempt counter, its event type, or the meaning of `RUN_MAX_RECOVERY_ATTEMPTS`
requires Graph/State review.

### Related Contracts

- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md)
- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md) (`CC-STATE-002`)

## ADR-029

### Title

Terminalize a Run when background execution raises outside its own failure handling

### Status

Accepted

### Context

`RunDispatcher._execute` caught every background exception and only wrote a log line. If
`claim_for_execution()` had already succeeded and `execute()` / `resume()` raised before their own
failure handling, the Run stayed `running` with no terminal event until (or beyond) the stale
recovery path, and non-HITL Runs were never recovered at all.

### Decision

The dispatcher's exception path calls
`RunService.fail_run_safely(run_id, code="execution_failed", message=..., required_status="running")`
before releasing its concurrency slot. The call is CAS-guarded and idempotent: an already-terminal
Run is never overwritten, and a second terminal event is never written. Failure of the fallback
itself is logged (`run_dispatch_terminalization_failed`) and does not crash the background thread.

### Consequences

Abnormal background execution always ends in a terminal state with a closing event. The terminal
`error` code `execution_failed` is implementation-level payload detail (the `error` payload shape
is unchanged).

### Change Conditions

Changing the fallback code, its required status, or removing the fallback requires API/State
review.

### Related Contracts

- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md) (`CC-API-001`)
- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md)

## ADR-030

### Title

Terminate an unschedulable ordinary Run instead of leaving it `accepted`

### Status

Accepted

### Context

`POST /api/v1/conversations/{conversation_id}/runs` persisted the Run and then returned
`409 conflict` when the bounded dispatcher had no capacity. The Run stayed `accepted` forever
because the maintenance recovery path only covers approval-bound successors, so ordinary Runs had
no recovery route and could not be observed as finished.

### Decision

When `dispatcher.dispatch()` reports capacity exhaustion, `create_run` terminalizes the freshly
created Run with `required_status="accepted"` and code `scheduler_unavailable` (terminal `error`
event) before returning `409`. The `409` response and its semantics are unchanged.

### Consequences

No Run is left permanently `accepted`; the failed Run's stream closes with a terminal error if the
client ever reads it. A client that retries creates a new Run. Persistent scheduling recovery for
ordinary (non-HITL) Runs remains out of scope and is documented as a known limitation in
`docs/OPERATIONS_RUNBOOK.md`.

### Change Conditions

Introducing automatic recovery for ordinary crashed Runs requires State/Graph/API review and an
explicit lease/fencing decision.

### Related Contracts

- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md) (`CC-API-001`)
- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md)

## ADR-031

### Title

Make approval expiry atomic and enforce expiry in the database decision CAS

### Status

Accepted

### Context

The maintenance path called the repository's `expire_due()`, which committed
`approvals.status = 'expired'` row by row and only afterwards updated the Run and wrote the
terminal event. A failure in between left `approval = expired`, `run = awaiting_approval`, and no
terminal event, and the maintenance task could never revisit that approval because it was no longer
`pending`. In addition, `decide_with_cursor` only required `status = 'pending'`, so an approval
that expired between the application-level `is_expired()` check and the update could still be
approved.

### Decision

1. `ApprovalService.expire_one()` performs the approval CAS (`pending -> expired`), the Run
   terminal transition (`awaiting_approval -> expired`), and the terminal event inside one
   transaction; a failure rolls back all three. `expire_due()` iterates `list_due_pending()`, and
   the read path (`ApprovalService.get`) uses the same atomic transition.
2. The maintenance worker calls the Application-layer expiry (`ApprovalService`) instead of the
   repository, so a row can never be flipped to `expired` without its Run transition.
3. `decide_with_cursor` requires `expires_at > NOW`, making database-authoritative time the
   decider for expiry. When the CAS is rejected by expiry, the service atomically expires the
   request and returns `409 conflict`; it never leaves the request `pending`.
4. Repository-level `expire_due` is removed. The Tool-level `mark_expired` remains a narrow
   `pending -> expired` CAS used defensively by `data_export` when it encounters an expired
   binding; it performs no Run transition and cannot be reached with a `pending` approval through
   the resume path (successors only exist after a committed approval).
5. A bounded maintenance reconciliation (`MaintenanceWorker.reconcile_expired_approvals` ->
   `ApprovalService.finalize_expired`) repairs the residue class that the old non-atomic path
   already produced: an approval that is `expired` while its Run is still `awaiting_approval` and
   has no terminal event. Because such a row is no longer `pending`, the ordinary expiry set never
   revisits it; the reconciliation applies the same CAS-guarded terminal transition (identical
   deterministic `event_id`), so it can only ever add the missing closing event and is idempotent.

### Consequences

There is no unrecoverable half-expired state, and an expired approval can never be approved (the
decision CAS is the authority, not only the application read). `approval_required` / `expired`
behavior visible through the API is unchanged (`409 conflict`, terminal `expired`).

### Change Conditions

Changing the expiry authority, the decision CAS conditions, or re-introducing a non-transactional
expiry path requires HITL/State/API review.

### Related Contracts

- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md) (`CC-API-001`, API-006)
- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md) (Graph-007)
- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md) (`CC-STATE-002`)

## ADR-032

### Title

Renew the execution lease with `updated_at` heartbeats instead of adding a fencing column

### Status

Accepted

### Context

Stale detection used only `updated_at`, so a legitimate execution lasting longer than
`RUN_STALE_RUNNING_SECONDS` could be treated as crashed: the maintenance task would release the
claim, re-dispatch the same successor, and two executors could run the same Run concurrently. A
fencing token or lease column would require a schema and State change.

### Decision

1. While a Run executes, the executor refreshes `updated_at` every `RUN_HEARTBEAT_SECONDS`
   (default 30, `RunService._heartbeat` -> `RunRepository.heartbeat`). The repository update is
   conditional on `status = 'running'`, so a terminal Run is never revived.
2. The heartbeat is sent from the Graph streaming loop, so the cadence is bounded by snapshot
   arrival; a heartbeat failure only weakens staleness detection and never aborts the business
   execution.
3. Stale recovery keeps its dual condition (status plus staleness) and `RUN_STALE_RUNNING_SECONDS`
   must stay far larger than `RUN_HEARTBEAT_SECONDS`; both values are documented in `.env.example`
   and passed through `compose.yaml`.

### Consequences

Legitimate long executions are no longer misjudged as crashed, so the at-most-one-executor intent of
Graph-007 is preserved in practice. Honest limits:

- This is not a full fencing-token lease. If every heartbeat is missed for longer than the stale
  threshold (for example a single Graph step taking longer than the threshold), two executors can
  still overlap. The terminal CAS (ADR-027) still guarantees a single terminal state and a single
  assistant message, and export artifacts keep their approval-level uniqueness.
- Ordinary (non-HITL) Runs are not automatically recovered after a crash; only approval-bound
  successors participate in stale recovery.
- Therefore the project does not claim strict at-most-once execution for every Run: it claims
  at-most-once terminal effects, at-most-once approved side effects, and bounded successor
  recovery.

### Change Conditions

Introducing a lease token, fencing epoch, or an execution ownership column requires a State and
schema change with Architecture Review.

### Related Contracts

- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md) (`CC-STATE-002`)
- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md) (Graph-007)
- [AGENTS.md](AGENTS.md)

## ADR-033

### Title

Move the project from MVP scope to V1, authorizing hybrid retrieval, rerank, MinerU sharding, and Chinese UI under recorded boundaries

### Status

Accepted

### Context

The project has passed MVP. `AGENTS.md` (RAG rule 5, Forbidden Patterns), `PROJECT_SPEC.md` §4,
and `docs/MASTER_AGENT_PROMPT.md` still list Hybrid Search and Reranker as MVP-forbidden, while
the owner has authorized: large-file upload with MinerU sharding (each remote submission
≤200,000,000 bytes **and** ≤200 pages), repeated MinerU calls with shard reuse, `BAAI/bge-m3`
Dense plus Chinese BM25 hybrid recall with RRF, `BAAI/bge-reranker-v2-m3` rerank over the same
Embedding base URL and Secret reference, real Parent/Child expansion, full Chinese UI, and real
answer streaming. `Change ID: CR-PRODUCT-20261009-01`.

### Decision

1. Replace the MVP prohibition list with V1 scope plus explicit new boundaries. Authorized
   additions are allowed only under their recorded `CR-*` change records and the corresponding
   `CC-*` Contract Change Records: `CR-RUNTIME-20261009-01` (durable tasks, execution ownership,
   recovery, cancellation), `CR-INGEST-20261009-01` (original files, shards, structure,
   completeness), `CR-RAG-20261009-01` (Parent/structure, BM25, RRF, rerank, citations),
   `CR-UX-20261009-01` (document binding, Chinese UI, preview, streaming).
2. Keep every safety and identity boundary unchanged: SQL Validator/read-only path, HITL
   approval-before-side-effect and idempotent decisions, registry-owned active versions,
   per-user resource scope, SSE contract, and the three independent counters
   (`max_tool_iterations = 8`, `max_repair_attempts = 3`, `max_answer_revisions = 1`).
   Ingestion retry, recovery attempts, and whole-book processing get their own separate bounded
   budgets.
3. Additional V1-specific boundaries: authorization is enforced before any provider receives
   content; BM25/Dense candidates are fused within one authorized version set (never compare raw
   BM25 scores across collections; never drop sparse candidates by a Dense threshold before
   fusion); rerank responses are validated (candidate index, duplicates, finite scores, count)
   with bounded timeout/retry and an explicit hybrid-only degradation or safe outcome; MinerU
   result URLs/redirects and ZIP extraction are constrained to controlled provider/storage
   boundaries (size, file count, path traversal); documents and retrieval results remain
   untrusted data that cannot override system rules or trigger tools.
4. Still excluded without a new approved change: Planner/Supervisor/Multi-Agent orchestration,
   Query Rewrite, Web Search/arbitrary URL fetching, arbitrary SQL/Tool/filesystem endpoints,
   Milvus Cluster, Kafka/Pulsar/Celery/RabbitMQ, SSO/complete RBAC, WebSocket as the primary
   protocol, and unbounded retries/loops/context growth.

### Consequences

Governance text (`AGENTS.md`, `PROJECT_SPEC.md`, `MASTER_AGENT_PROMPT.md`, `README.md`) now
matches the authorized scope; the phase plan (`docs/开发计划/POST_MVP_DEVELOPMENT_PLAN.md`) and the
implementation registry (`docs/POST_MVP_CHANGE_RECORDS.md`) carry the per-area impact analysis,
migration, rollback, and test plans. Existing runtime behavior is unchanged by this ADR; each
capability becomes real only when its phase is implemented, tested, and recorded.

### Change Conditions

Re-enabling any excluded item, weakening a retained safety boundary, or changing the remote
MinerU limits (200,000,000 bytes / 200 pages both satisfied) requires Architecture Review and an
updated Contract Change Record.

### Related Contracts

- [PROJECT_SPEC.md](docs/PROJECT_SPEC.md)
- [AGENTS.md](AGENTS.md)
- [MASTER_AGENT_PROMPT.md](docs/MASTER_AGENT_PROMPT.md)
- [POST_MVP_DEVELOPMENT_PLAN.md](docs/开发计划/POST_MVP_DEVELOPMENT_PLAN.md)
- [POST_MVP_CHANGE_RECORDS.md](docs/POST_MVP_CHANGE_RECORDS.md)

## ADR-034

### Title

Persist uploaded originals in managed content-addressed storage and make ingestion scheduling durable

### Status

Accepted

### Context

Upload previously read the whole file into memory (`await file.read()`), passed the bytes
through a worker thread, and kept no original file. Consequences: memory and concurrency risk when
raising the size limit, no way to retry a parse failure (the stored extracted text does not exist
when parsing failed), and a misleading failure mode when the bounded worker had no capacity — the
API returned `422` _after_ the Document/version/Job rows were already committed, leaving an
unrecoverable `pending` Job and a Document the client never learned about.
`Change ID: CR-RUNTIME-20261009-01` (batch 1 of the runtime-reliability change).

### Decision

1. Upload streams into a managed, content-addressed store
   (`backend/infrastructure/file_storage.py`, root `UPLOAD_ROOT`, default `runtime/uploads`,
   inside the persistent `/app/runtime` volume in Compose). Memory use is bounded by the upload
   chunk size; the SHA-256 and byte size are computed while streaming; the final object is
   published by an atomic `os.replace` from `.staging/<uuid>.part`, so a rejected or interrupted
   upload never leaves a half file. Client-supplied paths are never used: the DB stores only the
   controlled `storage_key` (`<sha256[:2]>/<sha256>`).
2. A new `document_files` table records the original file per Document (key, sha256, bytes,
   content type, source name). Composition-root wiring supplies `DocumentFileRepository` and
   `ManagedUploadStorage` to the Ingestion service.
3. `IngestionService.submit_upload()` takes a `StoredUpload` reference instead of raw bytes;
   `process_job()` no longer receives a payload and instead loads the original from the store
   (falling back to the stored normalized text for Documents that predate this change).
   Retrying a parse-failed Job therefore becomes possible without a re-upload.
4. Worker scheduling is durable: when the bounded worker has no capacity the API still returns
   `202` with the persisted Job, and `MaintenanceWorker.schedule_pending_jobs()` re-schedules
   never-claimed `pending` Jobs each maintenance cycle (bounded batch, atomic `claim`). Startup
   recovery keeps its declared semantics (`fail_orphaned_pending` remains the last-resort net for
   Jobs that were never scheduled even after repeated cycles).
5. Deleting a Document also removes its `document_files` row and, only when no other Document
   references the same content object, the stored object itself.

### Consequences

- The API no longer creates Documents it does not report, and a capacity blip no longer loses an
  upload; there is no unrecoverable `pending` state introduced by the upload path.
- Restart-safe ingestion is now possible for ingest-type Jobs (the bytes exist on disk), and the
  retry endpoint works for parse failures.
- Batch 1 covers persistence, scheduling, and cleanup. Still to come in this Change ID
  (registered, not yet implemented): execution ownership/fencing for ingestion and Run
  (`owner_token`/`heartbeat_at`/`attempt` columns), independent heartbeats that do not depend on
  model snapshots, cancellation, and the ordinary-Run crash-recovery decision
  (`docs/POST_MVP_PROGRESS.md` G17–G19).

### Change Conditions

Changing the storage layout, the content-addressing rule, the `document_files` schema, the
scheduling semantics, or the startup-recovery semantics for orphaned `pending` Jobs requires
State/API/Ingestion review and an updated `CC-*` Change Record.

### Related Contracts

- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md) (`CC-STATE-002` for the Run-side lease)
- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md) (upload/Job behavior)
- [AGENTS.md](AGENTS.md) (Ingestion and Data Lifecycle rules)
- [POST_MVP_CHANGE_RECORDS.md](docs/POST_MVP_CHANGE_RECORDS.md)

## ADR-035

### Title

Give ingestion Jobs and Runs explicit execution ownership with independent lease heartbeats and owner-conditional commit points

### Status

Accepted

### Context

ADR-032 renewed the Run lease through `updated_at` heartbeats emitted from the Graph streaming
loop, and recorded its own residual holes: a single Graph step lasting longer than
`RUN_STALE_RUNNING_SECONDS` could still let two executors overlap; `heartbeat_at` was not a
first-class column; ordinary (non-HITL) Runs were never recovered; and ingestion Jobs had no
execution ownership at all (staleness came only from `updated_at`). More importantly, the stale
release path (`release_stale_claim`: `running -> accepted`, then re-dispatch) recreates exactly
the `running` status the old CAS condition tested, so a released zombie executor could still win
`required_status='running'` transitions and write events, a terminal state, or an assistant
message after a second executor had been dispatched. `Change ID: CR-RUNTIME-20261009-01`
(batch 2 of the runtime-reliability change).

### Decision

1. **Explicit ownership columns (expand).** `runs` and `ingestion_jobs` gain
   `owner_token VARCHAR(64) NULL` and `heartbeat_at DATETIME(6) NULL`, added idempotently on
   startup (existing tables get `ALTER TABLE ... ADD COLUMN` only when the column is missing,
   checked through `information_schema`; fresh installs get them from the `CREATE TABLE`). Old
   rows keep `NULL` ownership and are read exactly as before.
2. **Claim records the owner.** `RunRepository.claim_for_execution[_with_cursor]` CAS
   (`accepted -> running`) and `IngestionJobRepository.claim` CAS (`pending -> running`) now
   require an `owner_token` and write it together with the first `heartbeat_at`. A claim remains
   the only place that sets an owner: an ingestion Job is `pending`-only claimable, a Run is
   `accepted`-only claimable, so there is still at most one executor per unit of work.
3. **Independent lease heartbeat.** `backend/application/lease.py::LeaseHeartbeat` is a bounded
   daemon thread that renews the lease every `RUN_HEARTBEAT_SECONDS` (Runs) or
   `INGESTION_HEARTBEAT_SECONDS` (Jobs, new setting, default 30s) and is stopped when the
   execution finishes. It does not depend on Graph snapshots, model output, or any step
   boundary: a single long step (large parse, large embedding batch, long SQL) is covered.
   `renew()` returning `False` means the database says the lease was revoked (terminal,
   released, or owned by someone else): the renewal stops and `lost` is latched. Infrastructure
   exceptions are logged and retried on the next tick, bounded by the execution lifetime (the
   thread is stopped with the execution; it is not a retry loop).
4. **Staleness is defined by the heartbeat.** All stale queries use
   `COALESCE(heartbeat_at, updated_at)` and recovery also revokes ownership
   (`owner_token = NULL`) in the same statement that releases or fails the row, so a recovered
   row can never be written by its previous executor.
5. **Every commit point is owner-conditional.** A lost executor writes nothing:
   - execution-time SSE events (`intent`, `retrieval`, `tool_call`, `tool_result`, `citation`)
     are inserted inside a transaction that locks the Run row and verifies
     `status = 'running' AND owner_token = ?`; on mismatch the write is abandoned with
     `ExecutionOwnershipLost` and no event row is created;
   - the executor checks ownership at every Graph snapshot boundary and aborts the stream on
     loss, bounding the exposure window to the single node already in flight;
   - the `awaiting_approval` transition and every terminal transition during execution carry
     `required_owner` in the same CAS that already requires the status, and any non-executing
     status clears `owner_token`;
   - ingestion `finish()` requires `status = 'running' AND owner_token = ?`;
   - ingestion promote (`mark_validated` + `registry.promote`) and delete cleanup
     (`_run_delete`) are gated by an explicit `still_owned()` check immediately before their
     side effects.
   - `data_export` remains governed by its approval-level at-most-once constraint
     (`uq_artifacts_approval` + approval status CAS); ownership fencing on the Run side is
     additive, not a replacement.
6. **Ordinary Run crash semantics.** A `running` Run whose lease went stale is _terminalized_
   as a safe failure (`execution_interrupted`, terminal `error` event, CAS + deterministic
   `event_id`, bounded per maintenance cycle) instead of being re-dispatched. Re-dispatch stays
   limited to approval-bound successors, whose continue-state is durable in MySQL (Run row,
   approval binding, `query_result_id`) and for which an approved resume entry exists.
   Re-running a non-HITL Run would execute the whole Graph again (new `database_query` snapshot,
   new generation) with no approved resume state, so a visible safe failure plus an explicit
   client retry (a new Run) is the contract-preserving choice. `recover_stale_running_successors`
   keeps its release/re-dispatch path only for successors and now uses the heartbeat-based
   staleness and clears ownership on release.
7. **`CHECKPOINT_MODE` (G17).** The deployed container profile switches its default to
   `sqlite` (`CHECKPOINT_MODE=${CHECKPOINT_MODE:-sqlite}` in `compose.yaml`, `CHECKPOINT_SQLITE_PATH`
   already `/app/runtime/checkpoints.sqlite` on the persistent named volume `checkpoint_data`),
   so Checkpoint state — notably the consumed `answer_revision_count` / `error.repair_attempts`
   budgets that a resume must carry over (Graph-005) — survives a process restart. The code
   default stays `memory` for isolated tests/host runs, and the semantics that resume
   correctness must not depend on in-process memory are preserved: successor continue-state
   comes from durable MySQL rows, not from the checkpoint.
8. **Cancellation is deferred.** A cancel endpoint is an API Contract change (a new endpoint,
   its states, and the interaction with approval/HITL terminality) and is not implemented in
   this batch. It is recorded as a known gap in `docs/OPERATIONS_RUNBOOK.md` and
   `docs/POST_MVP_PROGRESS.md` so it cannot be mistaken for implemented behavior.
9. **Honest limits (not claimed):** strict at-most-once execution is still not claimed for
   every Run. Fencing bounds _writes_: the losing executor cannot publish events, terminal
   state, assistant messages, promotions, or cleanup. It may still have one Graph node in
   flight when the lease is lost. Tool-level effects inside that node are covered by their own
   constraints (SQL is read-only and bounded, knowledge retrieval is read-only, export is
   approval-idempotent).

### Consequences

- A recovered/re-dispatched unit of work can no longer be written by its previous executor:
  the `running` status alone is no longer sufficient to publish, which closes the hole ADR-032
  documented.
- Ingestion and Run staleness now have the same, explicit, observable lease semantics
  (`owner_token` + `heartbeat_at`), and both are renewed by an independent thread.
- Ordinary crashed Runs reach a visible terminal state (previously they could stay `running`
  until a restart, and were not recovered at all).
- New settings and columns are additive; no SSE, HITL, SQL, or Graph topology semantics change,
  and the three independent counters keep their owners and budgets.

### Change Conditions

Changing the ownership column semantics, the lease renewal source, the set of owner-conditional
commit points, the ordinary-Run recovery outcome (`execution_interrupted` safe failure), or
introducing cancellation requires State/Graph/API Contract review with an updated `CC-*` Change
Record and Architecture Review.

### Related Contracts

- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md) (`CC-STATE-002`)
- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md) (Graph-005, Graph-007)
- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md) (`CC-API-001` terminal/error payloads)
- [AGENTS.md](AGENTS.md) (Ingestion and Data Lifecycle rules)
- [POST_MVP_CHANGE_RECORDS.md](docs/POST_MVP_CHANGE_RECORDS.md) (`CR-RUNTIME-20261009-01`)

## ADR-036

### Title

Bound ingestion content explicitly: a 1 GiB local cap, provider byte limits kept separate, no silent truncation, routed local parsing for text formats, and hardened parser result handling

### Status

Accepted

### Context

`CR-INGEST-20261009-01` (P2) requires whole-document ingestion without silent loss. The P0 audit
found: `MAX_EXTRACTED_CHARS = 400_000` silently truncated long documents (G2); the MinerU
comment claimed "≤600 pages" while the provider's documented limit is 200 pages, and the local
`max_document_bytes` default of 209,715,200 bytes (200 MiB) was larger than the provider's
200,000,000-byte limit while being documented as if they were the same number (G3/G7);
`MINERU_SUFFIXES` lacked `.xls/.xlsx` and, because `create_parser` returned a single adapter,
text files (`.md/.txt/.csv/.json`) were sent to MinerU in the `mineru` profile even though the
provider rejects them (G4 and the recorded runbook limitation); the result ZIP was read without
entry-count, uncompressed-size, or path checks (G6); and the promote gate only compared a vector
count instead of checking the indexed content itself. This batch also prepares the ground for
MinerU sharding: shard merging is only meaningful once silent truncation is gone.

### Decision

1. **Separate, explicit limits.** `MAX_DOCUMENT_BYTES` (local cap) defaults to `1,073,741,824`
   bytes (1 GiB) and is documented as the _local_ acceptance bound.
   `MINERU_MAX_BYTES = 200,000,000` and `MINERU_MAX_PAGES = 200` are the provider's per-file
   bounds; the two numbers are never mixed and each is labelled with its unit and owner. The
   remote adapter receives the provider bounds, so an oversize file fails with an explicit
   reason instead of being uploaded and rejected by the provider.
2. **No silent truncation.** `MAX_EXTRACTED_CHARS` becomes an explicit budget, not a cut: text
   longer than `MAX_EXTRACTED_CHARS` (default 4,000,000 characters) fails the Job with
   `extracted_text_too_large` instead of indexing a prefix as a whole document.
   `LightweightParser` raises `extracted_text_too_large` for the same reason and never sets
   `truncated = True`; `document_contents.truncated` therefore records "no silent loss" rather
   than "we dropped the tail".
3. **Batched persistence and indexing.** Children are embedded, written to the vector store, and
   persisted in bounded batches (`EMBEDDING_BATCH_SIZE` for embeddings,
   `VECTOR_UPSERT_BATCH_SIZE` for vector writes), so a large document does not require one
   unbounded provider call, one unbounded Milvus request, or one unbounded SQL statement.
4. **Completeness is checked against content, not only vector count.** Before promote the
   ingestion path verifies: every expected child has a stored chunk row for the version; child
   IDs are unique; the embedding dimension and collection name match the provider profile; the
   stored text is non-empty; and the vector probe (existing) still succeeds. A mismatch fails
   the Job with a safe code and promotes nothing.
5. **Local parsing for text formats, routed per file.** `create_parser` returns a router: when
   `PARSER_PROVIDER=mineru`, text-like files (`text/*`, `.txt/.md/.markdown/.csv/.json`) are
   parsed by the local lightweight parser and everything else by MinerU. The parser reports the
   provider that actually parsed the file (`provider_for`), and the index version records that
   effective provider instead of the configured family. This removes the recorded limitation
   that `.md` uploads fail in the `mineru` profile.
6. **Parser result handling is bounded.** Downloaded archives are read with a bounded byte
   limit; ZIP entries are validated (no absolute paths, no `..`, no backslashes), the entry
   count and total uncompressed size are bounded, and the selected Markdown entry is read
   through a bounded stream. Violations fail with safe parser codes and never touch the
   filesystem.
7. **Storage capacity is checked before accepting bytes.** The upload endpoint checks free space
   on the managed storage root (`MIN_STORAGE_FREE_BYTES`, default 256 MiB) before streaming and
   reports a `conflict` (409) with a safe message when the service cannot accept uploads; the
   proxy body limit (`client_max_body_size`) is kept above the configured local cap, and both
   are documented as a pair that must move together.
8. **Format matrix is explicit.** Formats are only advertised when they are supported by the
   configured provider tier: the gate accepts a format only if the tier can actually parse it.
   `.xls/.xlsx`, provider Office/HTML capability, and per-format success samples are recorded as
   remaining P2 work (they depend on the provider capability check and the sharding/manifest
   batch); until then those formats are rejected before any bytes are accepted.

### Consequences

- A long document is either fully indexed or explicitly failed; the "313-page book is searchable
  at head/middle/tail" acceptance becomes meaningful because nothing is silently cut.
- Oversize uploads no longer reach the provider just to be rejected there, and the local cap can
  be raised to 1 GiB without the provider limit drifting with it.
- Text uploads work in the `mineru` tier (previously a documented failure), and the recorded
  parser provider is truthful per file.
- A malformed or hostile provider archive cannot exhaust memory or escape the extraction
  directory; failures are explicit.
- Remaining P2 work is unchanged in intent but now unblocked: PDF page counting and page-range
  splitting, `ingestion_shards` manifest with reuse/resume, real page mapping, and the
  `.xls/.xlsx` + Office capability matrix.

### Change Conditions

Changing the local cap policy, the provider limits, the no-truncation rule (i.e. re-introducing
prefix indexing), the completeness gate, the per-file parser routing, or the storage-capacity
policy requires Ingestion/API review with an updated `CR-*`/`CC-*` record.

### Related Contracts

- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md) (Document and Index Version lifecycle)
- [TOOL_CONTRACT.md](docs/contracts/TOOL_CONTRACT.md) (parser adapter boundary)
- [AGENTS.md](AGENTS.md) (Ingestion and Data Lifecycle rules, Version Verification)
- [POST_MVP_CHANGE_RECORDS.md](docs/POST_MVP_CHANGE_RECORDS.md) (`CR-INGEST-20261009-01`)

## ADR-037

### Title

PDF page counting, dual-limit sharding, a persistent shard manifest with reuse and resume, and honest page mapping

### Status

Accepted

### Context

`CR-INGEST-20261009-01` batch 2 requires whole-document ingestion for documents that exceed the
provider's per-file bounds. The P0 audit left three unresolved facts. First, the 313-page sample
failed with `parse_failed` in six seconds and the code could not distinguish "page limit exceeded"
from "damaged file", because every non-zero provider outcome collapsed into one code
(`POST_MVP_PROGRESS.md` §1.4). Second, `ParsedDocument.pages` was hardcoded to `1` (G5), so no
page information existed anywhere in the pipeline and "the tail is searchable" could not be tied
to a real page. Third, `MinerUParser.parse` performed submit-and-poll as one indivisible call, so a
poll timeout was reported as `parser_unavailable` — indistinguishable from "the task was never
created". Retrying on that signal would re-submit identical bytes and re-charge provider quota,
while not retrying would abandon a task the provider is still working on.

The provider's documented per-file bounds are `≤200,000,000` bytes **and** `≤200` pages. A 313-page
book therefore cannot be submitted as one file regardless of its byte size.

### Decision

1. **PDF library: `pypdf`, version verified.** `pypdf==6.19.0` (BSD-3-Clause, pure Python, no
   native toolchain and no network at import). Verification was done against the installed package
   rather than memory: `PdfReader`, `len(reader.pages)`, `reader.is_encrypted`, `PdfWriter.add_page`,
   `PdfWriter.write`, and the error classes `PdfReadError` / `EmptyFileError` /
   `FileNotDecryptedError` / `DependencyError` were inspected in the installed module, and the
   313-page acceptance sample was read with it (313 pages, not encrypted). The pinned version is
   recorded in `pyproject.toml` and `uv.lock`. Impact: one additional pure-Python dependency;
   no new infrastructure.
2. **Page counting and slicing are pure functions** (`backend/infrastructure/pdf_slicing.py`):
   `count_pages(data)`, `slice_pages(data, first_page, last_page)`, and
   `plan_shards(page_count, total_bytes, ...)`. They touch no network, disk, or database.
   An encrypted PDF fails with `pdf_encrypted`, a structurally broken one with `pdf_damaged`, an
   empty one with `empty_document`. A page count is never guessed.
3. **Dual-limit sharding, no unbounded recursion.** Ranges are filled greedily up to
   `MINERU_MAX_PAGES`, then any range whose (estimated or measured) byte size exceeds
   `MINERU_MAX_BYTES` is bisected. A single page that still exceeds the byte limit fails with
   `shard_too_large`. The bisection uses an explicit stack and its depth is bounded by
   `log2(page_count)`; there is no recursive split without a floor. The 313-page sample therefore
   plans `1-200` and `201-313`, and the plan is deterministic and reproducible.
4. **A shard manifest is the execution record.** New table `ingestion_shards` (expand:
   `CREATE TABLE IF NOT EXISTS` inside `SCHEMA_STATEMENTS`, no one-off migration script) with
   `UNIQUE(index_version, shard_index)` and `KEY(sha256, parser_provider, parser_options)`.
   The status machine is `pending -> running -> success | failed`. `reused` is a boolean flag on
   `success`, deliberately **not** a fifth status, so "the manifest is closed" remains the single
   predicate "every row is `success`".
5. **The reuse key is `(sha256 of the shard bytes, parser_provider, parser_options)`.** A hit copies
   the stored text into the new manifest row and marks it `reused` without calling the provider.
   Bytes and parse profile being equal, the parse result is equal, so a successful shard is never
   paid for twice.
6. **Timeout is a distinct outcome, and a timeout is not "not created".** `MinerUParser` is split
   into `submit(...) -> ProviderTask(batch_id)` (the side-effect step) and
   `fetch(batch_id, *, deadline)` (the bounded wait); `parse()` remains as the convenience wrapper
   so existing callers and tests are unchanged. The batch id is persisted to the manifest
   **immediately after submit**. `fetch` raises `parse_timeout` on deadline expiry, separately from
   `parser_unavailable` (transport/submission) and `parse_failed` (provider-declared failure).
   On timeout the shard stays `running` with its batch id, the job fails with `parse_timeout`, and a
   retry resumes polling the **same** batch id instead of re-submitting.
7. **A failing shard is isolated.** Only the failed shard is re-attempted (a new `attempt`); shards
   that already succeeded are reused. Failures are never retried to bypass a security or permission
   boundary — this applies only to transport/parse outcomes.
8. **Page mapping is honest and queryable.** Page ranges are 1-based closed intervals, and the
   manifest is the authoritative page source: the document's shards expose exactly which pages each
   provider call covered. Where no reliable page information exists (non-PDF remote files, the
   legacy stored-text fallback, plain text), the shard records `NULL` and `ParsedDocument.pages`
   is `0`. A page is never fabricated as `1`. Chunk-level page spans remain P3 work (structure
   chunking), and this ADR does not claim them.
9. **The completeness gate covers shard closure.** Before promote the ingestion path verifies: the
   manifest row count equals the planned shard count; `shard_index` is contiguous from `0`; page
   ranges are contiguous and cover the whole document; every shard is `success`; and the stored
   text length equals the merged text length. A missing or unfinished shard fails the job
   (`shard_manifest_mismatch` / `shard_incomplete` / `shard_range_gap`) and promotes nothing.

### Consequences

- The 313-page acceptance becomes expressible: the book is split into `1-200` and `201-313`, both
  shards succeed, the merged text is indexed, and the head/middle/tail of the book are searchable.
- A poll timeout no longer risks duplicate provider charges, because the batch id survives the
  failure and the retry path is "resume", not "resubmit".
- A single failing shard costs one shard of quota to recover, not the whole document.
- Page numbers that appear anywhere in the pipeline are real or explicitly absent; the previous
  hardcoded `1` is gone.
- `document_chunks` semantics are untouched: chunk identity, roles, and ordinals are unchanged.
  Page ranges live in the shard manifest, not in the chunk rows.
- Costs: one new table, one new repository, one new pure-function module, and one new dependency.
  All are additive; no existing read path changes meaning.

### Change Conditions

Changing the PDF library, the sharding policy (page/byte limits, bisection floor), the reuse key,
the shard status machine, the timeout semantics (i.e. treating a timeout as "not created" or
re-submitting automatically), the page-mapping honesty rule, or the shard-closure gate requires
Ingestion/API review with an updated `CR-*`/`CC-*` record and Architecture Review.

### Related Contracts

- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md) (Document and Index Version lifecycle)
- [TOOL_CONTRACT.md](docs/contracts/TOOL_CONTRACT.md) (parser adapter boundary)
- [AGENTS.md](AGENTS.md) (Ingestion and Data Lifecycle rules, Version Verification)
- [POST_MVP_CHANGE_RECORDS.md](docs/POST_MVP_CHANGE_RECORDS.md) (`CR-INGEST-20261009-01`)
## ADR-038

### Title

Structure-aware chunking from provider block output, real page mapping, a conservative token budget, and bounded parent expansion in the RAG path

### Status

Accepted

### Context

`CR-RAG-20261009-01` (P3) targets G8/G9/G10. The P0 audit recorded: `chunk_document` splits
normalized text by blank lines and character counts, so no section path, page, block type, order,
or content hash survives ingestion (G8); `parent_child_mapping` merely filters the retrieval
results instead of expanding the real parent chunk, even though
`IndexRegistry.chunks_for_context` already exists (G9); and `context_build` concatenates child
`content` and truncates by **character** count, so the context has no token budget and no evidence
selection (G10).

P2 batch 2 left chunk-level page spans explicitly unclaimed: the shard manifest records real page
ranges per provider call, but a chunk could not be traced to a page.

A decisive fact was established before designing: MinerU's result archive contains
`<id>_content_list.json`, a list of typed blocks. Verified on the real 35-page sample (one real
call): 343 blocks, keys `type`, `text`, `text_level`, `bbox`, `page_idx`, `sub_type`, `img_path`,
`code_body`, `code_caption`, `code_footnote`, `image_caption`, `image_footnote`; observed types
`text` (225), `code` (117), `image` (1); `page_idx` spans `0-34`; `text_level` is `1`/`2` for
headings. The archive also carries `layout.json`, `*_model.json`, `*_origin.pdf`, images, and
`full.md`.

### Decision

1. **Structure comes from the provider's block output, not from heuristics on merged text.**
   `MinerUParser` reads `content_list.json` from the bounded archive in addition to `full.md` and
   returns normalized `DocumentBlock` records (`text`, `block_type`, `page_index`, `heading_level`,
   `order`). The read is bounded by the same archive limits as `full.md` (entry count, total
   uncompressed size, per-entry size, path safety). When the entry is missing, malformed, or over
   budget, ingestion falls back to the existing text chunking and records the fallback; it never
   invents structure.
2. **Page numbers are absolute and derived, never fabricated.** Each shard covers
   `[page_start, page_end]` from the manifest; a block's absolute 1-based page is
   `shard.page_start + block.page_index`. For non-PDF remote files, or when the provider gives no
   page index, the chunk records `NULL`. A page is never guessed as `1`. This closes the P2
   "chunk-level page span" gap with real data.
3. **Block types and heading levels are preserved.** `block_type` is one of
   `paragraph | heading | table | code | image | list` (normalized from the provider's `type` /
   `sub_type`). A heading opens a section; `section_path` is the bounded stack of enclosing
   headings joined by ` / `, capped at a configured depth and length. Tables, code, and images keep
   their own block type so a citation can say what kind of evidence it is.
4. **Parents follow structure; children follow a token budget.** A parent is a section-scoped
   group of consecutive blocks bounded by the parent token budget; a long single block is split so
   no parent is unbounded. Children are token-budgeted windows with the configured overlap inside
   their parent. Parent and child identity schemes are unchanged
   (`<index_version>-p<ordinal>` / `<index_version>-c<ordinal>`), so `document_chunks` keeps its
   existing meaning and only gains additive columns.
5. **The token budget is a conservative estimate, not a character count and not a real
   tokenizer.** `estimate_tokens` counts each CJK character as `1` token and each non-CJK
   character as `1/3` of a token, then takes the ceiling, so the estimate is an over-estimate for
   both scripts. The pipeline never equates a Chinese character with a token (the P3 requirement)
   and never under-fills the model window silently; the consequence is deliberately conservative
   context sizes. The estimator is bounded, monotonic, and unit-tested. If a real tokenizer for the
   configured model becomes available offline, the estimator is the single place to replace.
6. **Parent expansion happens in the Graph through an injected provider.** `RagWorkflow` receives a
   narrow `ParentContextProvider` protocol implemented over
   `IndexRegistry.chunks_for_context`. `parent_child_mapping` uses it to fetch the real parent
   records for the hit children, deduplicates by parent chunk ID, drops parents that are not
   `parent` role or do not belong to the same index version, and bounds the expansion count. The
   node still does not touch the database directly and does not mutate State outside its declared
   fields (Graph-002, Graph-004).
7. **Context is built under a token budget with explicit evidence selection.** For each unique
   parent, the hit children's text is always kept (that is the evidence that actually matched) and
   the parent contributes a bounded neighborhood as background; a parent is emitted at most once.
   Sections are added while the token budget allows; the build is bounded by both a token budget
   and a character ceiling, and the result records whether it was truncated. A large parent can
   therefore never displace other evidence.
8. **Chunk metadata is additive in the schema.** `document_chunks` gains
   `section_path`, `block_type`, `page_start`, `page_end`, `content_hash`, `token_count` through
   the existing idempotent expand path. No existing column changes meaning; `location` remains the
   human-readable locator. Old rows keep `NULL`/defaults and remain readable.

### Consequences

- A citation can name a real page and a section path instead of a synthetic `chunk-7`, and the
  page comes from the provider's own block index rather than from a guess.
- The context that reaches generation is bounded by tokens, contains the actual matching evidence,
  and cannot be flooded by one oversized parent.
- Structured chunking is available for provider-backed documents; text-only documents keep the
  previous behavior and are explicitly recorded as a fallback rather than silently degraded.
- Costs: one new pure-function module, one new expand migration, one new parser dependency on the
  archive entry, one new injected protocol in the Graph composition, and six additive columns.
- BM25/RRF/Rerank (P4) now have the block-level fields they need; this ADR does not implement them.

### Change Conditions

Changing the structure source, the page-derivation rule, the token estimator's calibration, the
parent/child identity scheme, the parent-expansion ownership (i.e. letting a Graph Node read the
database directly or adding a new Tool for it), or the context selection policy requires
Ingestion/Graph/Tool review with updated `CR-*`/`CC-*` records and Architecture Review.

### Related Contracts

- [TOOL_CONTRACT.md](docs/contracts/TOOL_CONTRACT.md) (`CC-TOOL-002`, `knowledge_search` output)
- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md) (`CC-GRAPH-002`, RAG subgraph)
- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md) (`CC-STATE-003`, `retrieval_results` shape)
- [AGENTS.md](AGENTS.md) (RAG rules, bounded context and evidence)
- [POST_MVP_CHANGE_RECORDS.md](docs/POST_MVP_CHANGE_RECORDS.md) (`CR-RAG-20261009-01`)
## ADR-039

### Title

Document Scope: a user-selected subset of the authorized document set, enforced at the request boundary and re-checked at the retrieval boundary

### Status

Accepted

### Context

Knowledge retrieval today has an implicit and maximal scope. `RunService._authorized_document_ids()`
returns every `available` Document the user owns; `RunCreateRequest` has no scope field;
`RagNodes.retrieval` sends only `query` and `top_k` to `knowledge_search`; and although
`KnowledgeSearchInput.document_ids` and the tool's `scope & requested` intersection already exist,
nothing ever sets them. Three concrete defects follow.

First, **evidence cross-contamination**: a question about document A may be answered from
document B, because both are in the same candidate pool and nothing lets the user say "only this
one".

Second, **mis-routing**: `IntentRouter` is a deterministic keyword classifier
(`KNOWLEDGE_TERMS` / `STRUCTURED_TERMS`). A user who explicitly selects a document and asks "what
is the approval amount limit in this document" hits `STRUCTURED_TERMS` through the word "amount"
and is routed to the SQL path, which never looks at the selected document at all. Selecting a
document is itself strong evidence of Knowledge intent, and the router currently ignores it.

Third, **silent failure**: `KnowledgeSearchTool._authorized_documents` filters candidates to
`status == "available"` and returns an empty successful result when the intersection is empty. A
selected but deleted, re-indexing, or foreign document therefore looks exactly like "the document
does not contain the answer", which is a false statement to the user.

Three identifiers must stay distinct and must never be conflated:
`authorized_document_ids` (server-computed, never from user input), `requested_document_ids`
(user-submitted, unproven), and `document_scope` (server-validated, the value that reaches the
Graph).

### Decision

1. **Document Scope is a subset of the authorized document set.** The server computes
   `authorized_document_ids` as "Documents owned by the current user whose status is `available`".
   The user may submit `requested_document_ids`; after server validation the value that enters the
   Graph is `document_scope`, and `document_scope` must be a subset of `authorized_document_ids`.
   A scope can only narrow; it can never widen.
2. **Three names, three meanings.** `authorized_document_ids` is computed at execution time and
   is never placed in Graph State and never returned to the frontend. `requested_document_ids` is
   unproven input. `document_scope` is the validated execution input. Code must not use the
   requested list as an authorization set anywhere.
3. **The API accepts a bounded, deduplicated list.** `RunCreateRequest` gains
   `document_ids: list[str] | None = None`: at most 20 identifiers, each at most 64 characters,
   deduplicated before the limit is applied, preserving first-occurrence order. An empty array and
   an absent field both mean "no scope". Paths, URLs, filenames, and arbitrary query conditions are
   not accepted as scope. The API layer owns format validation; the Application layer owns
   authorization validation.
4. **Create-time fast validation, with no side effects on rejection.** In `start_run`, when a
   non-empty `document_ids` is submitted, the server loads the current user's `available` Documents
   and requires every requested identifier to be a member. Any failure returns
   `409 conflict` with code `document_scope_unavailable`, and the message must not reveal whether
   the document does not exist, belongs to another user, was deleted, is re-indexing, or is in a
   particular version state. On rejection no Run, Checkpoint, Application Event, or dispatch is
   created and no Embedding, Milvus, Reranker, or LLM call is made. On success the validated list is
   written into the initial Graph State.
5. **State carries the scope and Resume must preserve it.** `document_scope` is a request-level
   State field owned by the request initializer, consumed by the router and the retrieval node,
   with Run lifetime and Checkpoint persistence. `initial_state` accepts and stores it;
   `request_initialize` must **not** reset, clear, or overwrite it, so a HITL Resume and an approval
   successor Run inherit the scope from the Checkpoint instead of falling back to "no scope"
   (which would silently widen retrieval). The field is bounded and deduplicated; the full
   authorized set is never copied into State.
6. **A′ routing: scope is a strong Knowledge signal, but only for requests that carry one.**
   With a non-empty `document_scope`, a request that matches `KNOWLEDGE_TERMS`, `STRUCTURED_TERMS`,
   or both is routed to `knowledge`; a request that matches neither is routed to `general`. The
   mixed case therefore resolves through the user's own disambiguation instead of returning
   `unsupported_mixed_query`. Without a scope the existing classification is unchanged, so
   greetings still reach `general` and no-scope requests keep their current routing and retrieval
   semantics exactly. `current_path` continues to follow the State Contract and stays `null` for
   non-`routed` outcomes. No Planner, Supervisor, Query Rewrite, or free-planning Agent is added.
7. **The retrieval boundary is the authoritative re-check.** Create-time validation is a fast
   fail, not the security boundary. `knowledge_search` re-validates, before any provider is called:
   whether the scope is empty; whether the user still owns the documents; whether they are still
   `available`; whether they still have a valid active index version; whether an explicit
   `index_version` is still active, available, and authorized; and the races between creation and
   retrieval (deletion, re-index, version switch). The `scope & requested` intersection logic is
   unchanged and must never become "user request overrides authorization".
8. **An empty intersection is an explicit rejection, not an empty answer.** When a non-empty scope
   intersects the currently available authorized set to nothing, `knowledge_search` returns a
   normalized `rejected` Tool Result with code `document_scope_unavailable`, without calling Dense
   embedding, BM25, Milvus, Reranker, or the LLM; the Graph maps it through the existing safe
   failure path; and the user-visible message is explicit but does not reveal the reason. It is
   never presented as "the document does not contain the answer".

   The same rejection applies when a selected Document has no usable active index version. That
   case is caught at the **retrieval boundary**, not at create time: create time checks membership
   in the available set only (decision 4), because a Document whose new version is still indexing
   legitimately keeps serving its previous active version. The candidate-version scan therefore
   runs **before** the embedding call, so this rejection path also makes zero provider calls.
   Which boundary catches which case is deliberate and recorded here: a foreign, deleted, or
   unknown identifier is refused at create time with `409`; a Document that lost its active version
   after the Run was created is refused at retrieval with the same code inside the stream.
9. **Parent expansion, citations, and caches stay inside the scope.** Any Child, Parent, or
   Evidence whose document identifier is not in the effective scope, or whose index version is not
   the authorized active version, must be dropped. The existing parent-context provider already
   filters by index version and role; scope filtering is added to the same boundary.
10. **The scope is not persisted on the Run row.** It is an execution input carried by Graph State
    and Checkpoint; adding a column would create a second source of truth with no read path. The
    Run row, API responses, SSE payloads, and logs never expose the scope contents beyond what the
    client itself submitted.
11. **The guarantee is bounded, and stated honestly.** The system guarantees that unauthorized,
    deleted, re-indexing, or non-active-version content never reaches a provider or the final
    context, because every boundary re-validates and fails safe. It does **not** guarantee that an
    old version cannot change during one question, since there is no database-level strong
    consistency snapshot across the request lifetime.
12. **Scope enforcement precedes the retrieval stages that exist today.** The V1 target chain is
    `Dense + Chinese BM25 -> RRF -> BGE Rerank`; BM25, RRF, and Rerank are `CR-RAG` P4 work and are
    **not implemented**. What is implemented is the scope resolution and the intersection check,
    which happen before the embedding call. P4's stages run after that point and therefore inherit
    the enforcement by construction. This ADR does not claim that BM25, RRF, or Rerank exist.

### Consequences

- A user can ask about one document and get evidence only from it; a question whose wording
  contains business-data keywords is no longer taken away from an explicitly selected document.
- Selecting an unavailable or foreign document produces an explicit, non-leaking conflict instead
  of a silent empty answer that reads as "not in the document".
- Greetings keep working with a scope selected, because the A′ rule only overrides the
  knowledge/structured decision, not the "neither" decision.
- No-scope requests are behaviorally unchanged, so the existing suite remains the regression net.
- The scope survives HITL Interrupt/Resume and approval successors through the Checkpoint, which
  closes the failure mode where a resumed run would silently widen back to the whole corpus.
- Costs: one request field, one State field, one error code, one router parameter, one tool branch,
  and contract updates across Terminology, State, Tool, Graph, and API.
- The recorded guarantee is weaker than "stable snapshot for the duration of a question"; every
  boundary re-validates and fails safe instead.

### Change Conditions

Changing the definition of the authorized set, the subset rule, the rejection code or status,
the A′ routing table, the requirement that `request_initialize` preserve the scope, the
authoritative re-check at the retrieval boundary, the "empty intersection is a rejection" rule, or
the decision not to persist the scope on the Run row requires API/State/Tool/Graph review with
updated `CR-*`/`CC-*` records and Architecture Review.

### Related Contracts

- [TERMINOLOGY.md](docs/contracts/TERMINOLOGY.md) (TERM-044/045/046)
- [STATE_CONTRACT.md](docs/contracts/STATE_CONTRACT.md) (`CC-STATE-004`)
- [TOOL_CONTRACT.md](docs/contracts/TOOL_CONTRACT.md) (`CC-TOOL-003`)
- [GRAPH_CONTRACT.md](docs/contracts/GRAPH_CONTRACT.md) (`CC-GRAPH-003`)
- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md) (`CC-API-002`)
- [AGENTS.md](AGENTS.md) (RAG and authorization rules)
- [POST_MVP_CHANGE_RECORDS.md](docs/POST_MVP_CHANGE_RECORDS.md) (`CR-RAG-20261009-01`)
## ADR-040

### Title

Document lifecycle: a Document is `indexing` until a version is promoted, and a duplicate upload reports the real state

### Status

Accepted

### Context

`CR-INGEST-20261009-01` batch 1 established that the original file and the Ingestion Job are persisted before scheduling, so a failed parse is retryable. Two defects remained open in the P0 matrix (G19, G20) and were re-confirmed during the P2/P3 acceptance runs.

**G19 — a Document is `available` before it is usable.** `DocumentRepository.create` inserts
`status = 'available'` at upload time, while `active_index_version` stays `NULL` until a version is
promoted. `available` therefore means two different things: "the user uploaded it" and "it has a
valid active index version". Consequences: the document list shows a broken document as available;
the Document Scope picker offers it; `IndexRegistry.resolve_active_version` silently returns
`None`; and if ingestion fails, the Document stays `available` with no active version forever. This
was observed directly: a 313-page document left over from the P0 audit sits at
`status=available` with `active_index_version=NULL`, and it is offered in the scope picker while
being unretrievable.

**G20 — a duplicate upload always reports success.** `submit_upload`'s dedup branch returns
`UploadOutcome(..., ingestion_job_id="", index_version=<active or "">, status="success", duplicate=True)`
with the status hardcoded. A client re-uploading content whose Job is `pending`, `running`, or
`failed` is told `success`, so it cannot tell "already indexed" from "still indexing" from
"indexing failed". Combined with G19 this is unrecoverable through the API: re-uploading the broken
313-page document returns `success`, and `reindex` refuses with `409` because there is no stored
content to re-index, so the only escape is to delete and re-upload.

### Decision

1. **A Document is created as `indexing`.** `available` is reserved for a Document that has a valid
   active index version and can be retrieved. `documents.status` therefore has four values:
   `indexing`, `available`, `failed`, `deleted`.
2. **Promotion is the single transition into `available`.** `IndexRegistry.promote` sets the active
   version and the `available` status in **one conditional UPDATE** that refuses `deleted`
   documents, so a deleted Document can never be reactivated and the two facts cannot diverge. The
   previous "is the document available?" precondition is replaced by the conditional update itself,
   because requiring `available` before promoting would deadlock the new lifecycle.
3. **A failed ingestion marks the Document `failed`, but only when it has no active version.** A
   failed re-index must not degrade a Document that is still serving its previous version, so the
   update is guarded by `active_index_version IS NULL`. The same guard is applied by stale-running
   and orphaned-pending recovery, so a Job that dies without a failure record still leaves an
   honest Document state instead of a permanent `indexing`.
4. **A duplicate upload reports the real state.** The dedup branch returns the latest Job's
   `status` and `ingestion_job_id` when a Job exists, and otherwise falls back to
   `success`/`failed` based on whether the Document actually has an active version. It never
   reports `success` for a Document that is not indexed, and it does not schedule anything (the
   duplicate path still does not dispatch).
5. **Deletion stays available for every non-deleted Document.** `submit_delete` accepts
   `indexing` and `failed` as well as `available`, so a stuck or broken Document can always be
   removed. `deleted` remains terminal.
6. **Retrieval predicates do not change meaning.** Only `available` Documents are members of the
   Authorized Document Set, so an `indexing` or `failed` Document cannot be retrieved, cannot be
   selected as a Document Scope, and cannot become an evidence source. Re-index still requires an
   `available` Document, because it needs existing stored content.
7. **No data migration is performed for legacy rows.** A Document that is `available` without an
   active version can exist from before this change. Backfilling would have to guess whether it is
   indexing, failed, or deliberately empty, so instead: it is not retrievable and not selectable
   (unchanged), a duplicate upload now reports `failed` for it, and it can be deleted. This legacy
   state is recorded as a known condition rather than silently rewritten.
8. **Document status is application-owned business data, not Graph State.** No State field, no
   Checkpoint format, and no Graph topology changes. The status is a Conversation/Application
   persistence fact that the API exposes and the retrieval boundary reads.

### Consequences

- The document list, the scope picker, and the retrieval boundary now agree on what "usable" means;
  a broken document can no longer be presented as available or selected for retrieval.
- A client re-uploading content learns the truth (pending / running / failed / success) instead of
  a hardcoded `success`, which makes the failure recoverable through the API.
- A failed ingestion no longer leaves a Document permanently `indexing`; a failed **re-index**
  still leaves the working version untouched.
- Deleting a stuck Document is always possible, which removes the need for the manual purge path
  that the P3 acceptance script had to add.
- Costs: one new status vocabulary, one new repository method for the atomic activation, one for
  the guarded failure marking, and updated predicates in promotion and deletion.
- Risk accepted: a legacy `available`-without-version row is not repaired automatically. It is
  inert (not retrievable, not selectable) and deletable.

### Change Conditions

Changing the status vocabulary, the single-transition rule for `available`, the guard on
failure marking (`active_index_version IS NULL`), the duplicate-upload response semantics, or the
deletion predicate requires Ingestion/API review with updated `CR-*`/`CC-*` records and
Architecture Review.

### Related Contracts

- [API_CONTRACT.md](docs/contracts/API_CONTRACT.md) (`CC-API-003`)
- [TOOL_CONTRACT.md](docs/contracts/TOOL_CONTRACT.md) (`CC-TOOL-003`, Authorized Document Set)
- [AGENTS.md](AGENTS.md) (Ingestion and Data Lifecycle rules)
- [POST_MVP_CHANGE_RECORDS.md](docs/POST_MVP_CHANGE_RECORDS.md) (`CR-INGEST-20261009-01`)

## ADR-041: Deterministic compatibility failures and versioned hybrid retrieval

Status: Accepted for implementation under CR-RAG-20261009-01 (2026-10-10).

Architecture review: preserve Tool Registry, active-version authority, request authorization,
Graph topology, HITL and bounded context. No State, HTTP endpoint or SQL permission change.
`knowledge_index_incompatible` is a non-recoverable Tool/Graph failure; accepted Runs report it
through their existing terminal events, never a new HTTP status after acceptance.

New ingestion uses a profile-derived `_hybrid_v1` collection. Complete child text (maximum 65535
UTF-8 bytes, fail explicitly above it) feeds native BM25 with the Chinese analyzer. The preview
remains independently truncated. Legacy Dense collections remain readable through registry metadata.
Re-index is the controlled migration; failure retains the previous serving version. Rollback of
retrieval disables rerank and uses Dense reads; no volume or legacy collection is deleted.

Dense and sparse are queried with the same registry-derived active version filter. Dense ranks
are global within the compatible embedding space. BM25 ranks are used only from one compatible
hybrid collection and its entire authorized version set, never compared across collections.
RRF k=60; missing rank contributes zero. Candidate window <=200, rerank input <=50, output <=50.
Rerank references the Embedding URL/Secret directly. Invalid, duplicate, non-finite or missing
rerank results fall back to retained bounded candidates with explicit degradation metadata.
Returned chunk identities and active versions are checked again before content reaches rerank.

Validation: unit boundary tests plus an isolated real Milvus collection and actual rerank request;
quality claims require the separate fixed evaluation and are not implied by this review.
# ADR-042: Evidence-aware validation and authoritative final citations

Status: Accepted for the authorized V1 RAG implementation.

Context: Truncation retained unused sources and a single token/label could pass grounding;
exhausted revisions could still publish an invalid candidate.
Decision: Keep graph topology and budgets. Select children before background, retain exact
excerpts, screen claims deterministically and use bounded evidence-only remote adjudication.
Fail closed on invalid judge output; commit only validated sources. Final SSE adds citations
and safe error/validation codes. No provider SDK enters State or Application.
Impact: CC-GRAPH-004, CC-STATE-005, CC-API-005; focused regression and live quality evaluation.
Rollback: Restore the prior validation implementation only together with its contracts;
retrieval can be configured Dense independently, but unsafe validation is not a release option.


## ADR-043：修正分页 cursor 的 UTC 语义

日期：2026-10-11。状态：Reviewed（用户授权继续修复阶段一已复现阻断项）。
关联：CR-PAGINATION-20261011-01、CC-API-006、API-012/014。

### 背景与问题

历史审计 RUN_LIFECYCLE_REPAIR_SUMMARY §8 的 P1 分页时区项，在本机 UTC+8
环境已复现：数据库朴素 UTC 值直接调用 datetime.timestamp()，生成值偏移 8 小时。
Conversations/Messages 解码按 UTC；Documents 的 FROM_UNIXTIME() 还依赖 MySQL
session 时区。浮点转换也不适合严格保持 DATETIME(6) 的微秒边界。

### 架构评审与决定

这是现有 API-012 不透明 cursor 的实现纠错，不引入接口字段、State、Graph、数据库
列、依赖或新的分页架构。复用 domain.identifiers 的时间辅助边界，提供统一 UTC
整数微秒编码/解码：朴素输入附加 UTC，aware 输入先转换 UTC；解码返回朴素 UTC。
Conversations、Messages、Documents 使用同一规则；Documents 查询绑定 datetime，
不使用 FROM_UNIXTIME()，从而不依赖 session 时区。

### 影响与兼容

保留不透明十进制微秒字符串、列表排序、上限及资源授权语义。不修改业务数据。
旧非 UTC 主机发出的错误 cursor 无法无歧义地区分，不回填或自动补偿；升级后客户端
应从第一页重新获取 cursor。客户端已有重取列表入口，无需新增前端协议。
本轮只修复时区/精度，不扩展为复合 cursor。同一 created_at 的多个记录原有分页
tie-break 缺口须单独登记，不能将本轮结果称作所有分页行为已经验收。

### 验证与回滚

tests/unit/test_pagination_cursor.py 覆盖三个生产仓储入口连续翻页、UTC-5/UTC/UTC+8
确定性时间替身、微秒边界及 aware UTC 转换。旧仓储源码隔离进程复现 7 failed/9 passed；
修复后的 focused 27 passed，最终回归结果见本批报告，不以代码存在替代验收。
补充验证：专用隔离 MySQL 三个 session 时区、三个列表入口共 9 passed，项目资源已清理。
未执行用户数据迁移或恢复演练，HTTP 完整分页与阶段一其他门禁仍需独立核销。回滚时仅还原本批 cursor 增量及相应登记，
保留本轮之前 documents.py 的用户未提交内容；回滚会重新引入时区问题，仍 NO-GO。
