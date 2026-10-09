# Repository Agent Rules

This file contains mechanical rules for Coding Agents. Project scope is in [PROJECT_SPEC.md](docs/PROJECT_SPEC.md), workflow guidance is in [MASTER_AGENT_PROMPT.md](docs/MASTER_AGENT_PROMPT.md), and canonical architecture facts are in [docs/contracts/](docs/contracts/).

## Authority and Change Control

1. `docs/contracts/` is the source of truth for Terminology, State, Tools, Graph, and API behavior.
2. `PROJECT_SPEC.md` defines scope and acceptance; it does not replace Contracts.
3. Do not silently change a Core Contract to fit implementation.
4. Changes to State Schema, Graph topology, Agent Loop, Tool Contract, API/SSE Contract, SQL security, database schema, Checkpoint, Data Source abstraction, HITL flow, or core infrastructure require Architecture Review and Contract Change Protocol.
5. Ordinary implementation, tests, logging, configuration, documentation, and UI changes may proceed when they preserve Contracts.

## Repository Structure

Use these ownership boundaries, preserving equivalent existing boundaries:

```text
backend/api             HTTP schemas, authentication, SSE transport
backend/application     use cases and application services
backend/domain          State types, policies, errors, identifiers
backend/graph            LangGraph topology and Nodes
backend/tools            Tool Registry, schemas, executors
backend/infrastructure  LLM, Embedding, Parser, Milvus, MySQL, Checkpoint adapters
backend/workers          background ingestion and recovery
frontend                 Vue3 pages and API/SSE clients
tests/unit               isolated logic and policy tests
tests/integration        service and adapter tests
tests/e2e                small critical smoke flows
docs/contracts           canonical Contracts
scripts                  setup, evaluation, operational scripts
```

Do not create a second implementation of an existing abstraction.

## Version Verification

Before using a version-sensitive API, inspect actual dependency and lock files and current official documentation or installed APIs. Verify LangGraph, LangChain, FastAPI, Pydantic, Milvus/pymilvus, MySQL Driver, Vue/Vite, Docker, and Compose. Never rely on remembered APIs. Supporting dependencies require a recorded reason, version, impact, and verification. Core infrastructure or major dependency changes require Architecture Review.

## Commands

Use repository-documented commands. Expected categories include:

```text
docker compose config
docker compose up -d
docker compose ps
docker compose logs <service>
pytest
pytest tests/unit
pytest tests/integration
pytest tests/e2e -m smoke
```

Record exact commands and outcomes. Do not claim a passing result when infrastructure is unavailable.

## Python Rules

1. Use the configured Python version, formatter, and linter.
2. Type public functions and State boundaries.
3. Keep request, domain, and infrastructure schemas separate.
4. Do not put provider SDK types in Domain or Application Contracts.
5. Do not use module-level mutable request state or global conversation state.
6. Read configuration from typed settings/environment; never hard-code credentials.
7. Map boundary failures to project-defined safe errors.
8. Keep modules focused and reuse existing abstractions.

## LangGraph Rules

1. Use rule-directed Workflows for explicit control flow and bounded Agents for dynamic decisions.
2. Do not create a Super Agent, Supervisor, Planner, or decorative MVP Nodes.
3. A Graph Node must perform execution or State transformation.
4. A pure routing predicate or conditional edge, including `tools_condition`, is not a business Node.
5. `rag_start`, `rag_end`, `sql_agent_start`, and `sql_agent_end` are control-flow boundaries unless they perform real State transformation.
6. Every legal path must reach a terminal final, safe error, rejection, or unsupported outcome.
7. Use independent configured counters: `max_tool_iterations = 8`, `max_repair_attempts = 3`, and `max_answer_revisions = 1`.
8. Never create an infinite Tool Loop, validation loop, or retry loop.
9. `max_tool_iterations` counts SQL Agent Tool Calls, `max_repair_attempts` counts same-question repairs per Run, and `max_answer_revisions` counts revisions for one answer candidate. They have separate owners and budgets; deterministic `invalid_request`, `routing_error`, and `unsupported_mixed_query` outcomes may go directly to safe finalization, while ordinary model answers require Answer Validation.

## State Rules

1. Keep Conversation Persistence, Graph State, Checkpoint, and Application Event separate.
2. `conversation_id` groups user-visible dialogue; `thread_id` identifies execution continuity; `run_id` identifies one concrete Graph execution. Resume creates a new `run_id` on the same Thread.
3. `approval_request_id` identifies one high-risk approval; `event_id` identifies one emitted event.
4. Every State field must have an owner, producer, consumer, mutator, lifecycle, and persistence policy in `STATE_CONTRACT.md`.
5. Tools do not directly mutate Graph State. Graph/Application layers map normalized Tool Results to State.
6. `messages` is the primary model history; `tool_calls` and `tool_results` are bounded normalized execution views.
7. Never expose raw Graph State, Checkpoint snapshots, or hidden reasoning through API or SSE.
8. Keep State collections bounded, redacted, and execution-sized.
9. `current_path` is `knowledge | structured_data | general | null`; it is null for every non-`routed` Routing Outcome.
10. `query_result_id` is the immutable Query Result Snapshot reference produced by successful `database_query`; `data_export` may not use a vague Run ID.

## Tool Rules

1. Invoke Tools only through the Tool Registry and Tool Executor.
2. Tool Calls contain proposed arguments and are untrusted until validation.
3. Tools receive explicit input and return normalized `success | rejected | failed` Tool Results.
4. Tool Result mapping to State belongs to Graph/Application layers; the Event Adapter and API do not write State.
5. Respect each Tool's schema, risk, permissions, timeout, retry policy, and output limits.
6. Do not expose arbitrary Tool invocation as a frontend or public API.
7. Do not expose hidden reasoning, credentials, raw provider traces, or unrestricted source content.

## SQL Security Rules

Every SQL Agent-generated or Agent-requested SQL operation follows:

```text
LLM -> database_query Tool -> SQL Validator -> read-only Database
```

1. Enforce AST validation, statement allowlist, multi-statement blocking, schema/table restrictions, dangerous-function restrictions, complexity controls, LIMIT/result limits, and timeout.
2. Use a read-only database account and bound parameters.
3. `tool_result_normalize` maps SQL Validator outcomes; it cannot parse SQL or decide safety.
4. Never add an arbitrary SQL execution endpoint.
5. Never retry security rejection, permission failure, dangerous operation, or completed side effect to bypass a boundary.

Structured Data Preview is a separate controlled Application/API read through an authorized Data Source Adapter or Repository with parameterized read-only queries. It is outside the Agent Tool Calling Loop, accepts no arbitrary SQL, and still enforces authorization, read-only credentials, limits, and resource bounds.

## RAG Rules

```text
Dense + Chinese BM25 Retrieval -> RRF Fusion -> BGE Rerank
-> Parent-Child Expansion (authorized, bounded) -> Context Build -> Generation -> Answer Validation
```

1. Use `knowledge_search` through the Tool Registry and resolve active versions through the application registry.
2. Keep retrieval/context bounded and preserve source metadata (page/section spans), evidence IDs, and citations.
3. If sufficient, valid, authorized evidence is unavailable, return `insufficient-evidence` / safe outcome.
4. Never use pretrained LLM knowledge to fill enterprise facts.
5. V1 authorizes Chinese BM25 sparse retrieval, Dense/BM25 hybrid recall with RRF fusion, `bge-reranker-v2-m3` rerank (reusing the Embedding base URL and Secret reference, never copying keys), structured Parent/Child chunks with real parent expansion, and MinerU multi-call sharding for large files (`CR-RAG-20261009-01`, `CR-INGEST-20261009-01`). These changes require the recorded Architecture Review / Contract Change Protocol and must respect: authorization before any provider receives content; global candidate ranking within one authorized version set (never compare raw BM25 scores across collections, never drop sparse candidates by a Dense threshold before fusion); bounded rerank response validation with explicit hybrid-only degradation or safe outcome; ZIP/URL handling limited to controlled provider/storage boundaries.
6. Still not allowed without a new approved change: Query Rewrite, Planner, Supervisor, application-level Multi-Agent orchestration, or using a chapter summary as a fact source instead of re-checking the original text.

## HITL Rules

`data_export` preserves:

```text
proposal -> validation -> Approval Request -> Interrupt -> Checkpoint
-> approval_required -> Approve -> original handoff_complete -> successor Resume
                         Reject -> original terminal rejected
```

1. No side effect before approval.
2. Approve and Reject are idempotent by `approval_request_id`.
3. Reject cannot execute through another path.
4. Expired approval cannot Resume the original execution.
5. Terminal decisions cannot execute again.
6. Checkpoint is persistence for resumable execution, not a business Node.
7. The waiting Run remains `awaiting_approval`; Approve transitions it to terminal `handoff_complete`, returns `original_run_id` and `successor_run_id`, and the frontend listens to the successor stream. Reject transitions it to terminal `rejected` and creates no execution successor. Expiry transitions it to terminal `expired`. `approval_required` may close the waiting Run's SSE connection without meaning the business request is complete; the successor has its own sequence.

## API and SSE Rules

1. Use `/api/v1` and `API_CONTRACT.md` schemas.
2. Do not expose raw Graph State, Checkpoint mutation, arbitrary SQL, arbitrary Tool invocation, arbitrary filesystem paths, or arbitrary URL fetching.
3. Use SSE as primary streaming; WebSocket requires an approved architecture change.
4. SSE `sequence` starts at `1` and strictly increases within a Run.
5. Wire format is:

```text
id: <event_id>
event: <event_type>
data: <JSON encoded Event Envelope>

```

6. Use `Last-Event-ID` for replay and `event_id` for deduplication.
7. Close after terminal `final` or terminal `error`.
8. Do not expose hidden Chain-of-Thought or complete internal State.

## Authentication and Secrets

1. Protect Conversation, Run, Approval, Document, Ingestion, and Data APIs with approved minimal authentication.
2. Enforce current-user resource scope at API/Application boundaries.
3. Never commit real secrets or put them in source, tests, README, fixtures, logs, or Git.
4. Use `.env` and `.env.example`; ensure `.env` is Git-ignored.
5. Redact credentials, tokens, database errors, provider responses, and sensitive query data from logs.

## Ingestion and Data Lifecycle

1. Upload returns `202 Accepted` and a persisted Job.
2. Job states are `pending`, `running`, `success`, and `failed`.
3. Startup recovery marks stale `running` Jobs and orphaned `pending` Jobs as `failed` using configured age/claim thresholds.
4. Retries are explicit, bounded, and idempotent.
5. Retry uses `POST /api/v1/ingestion-jobs/{ingestion_job_id}/retry`; only `failed` Jobs are retryable, the original remains history, and the retry creates a new Job ID with an incremented attempt for the same document/version intent. `pending`, `running`, and `success` Jobs are not retryable.
6. Re-index writes a new version, validates and promotes it, then retires the old active version.
7. Delete removes the Document from active retrieval and cleans associated vectors.
8. Active index-version metadata belongs to the application registry, not Milvus.
9. Delete marks a Document unavailable/deleted before cleanup; promotion re-checks that state and cannot reactivate a deleted Document.
10. Explicit retrieval versions must be active, available, and authorized.

## Structured Data Preview

Data Preview uses controlled, authorized, read-only application queries. It does not accept arbitrary SQL and does not bypass resource scope. Agent-generated SQL alone follows `database_query -> SQL Validator -> read-only Database`.

## Dependencies and Runtime

1. Official runtime is Windows + Docker Desktop.
2. MySQL is Docker MySQL 8.x with volume persistence.
3. Milvus is Standalone; do not introduce Cluster, Kafka, or Pulsar.
4. Containers communicate with Compose service names, never container-to-container `localhost`.
5. Local LLM deployment is opt-in and not a default startup dependency.
6. MinerU is accessed through the Parser Adapter.

## File Modification Rules

1. Read existing code and tests before editing.
2. Keep changes scoped to the active Phase and request.
3. Use repository formatting and validation tools.
4. Do not overwrite unrelated user changes.
5. Update tests and documentation when behavior changes.
6. Contract changes require Change ID, impact analysis, affected Contract/document updates, tests, and consistency review.

## Definition of Done

A change is complete only when:

```text
Code + Test + Run + Documentation + Acceptance
```

Evidence must include exact commands, results, relevant Contract references, and known limitations. Code presence alone is not completion.

## Forbidden Patterns

Do not introduce unrestricted Super Agent, Planner, Supervisor, or Multi-Agent orchestration; arbitrary SQL, Tool, filesystem, URL, Graph State, or Checkpoint APIs; SQL execution bypassing Validator/read-only account; Tool execution bypassing Registry; HITL side effects before approval; infinite retries/loops/context growth; real secrets in repository artifacts; provider SDK leakage into Domain/Application Contracts; or Milvus Cluster, Kafka, Pulsar, Celery, RabbitMQ, OAuth/SSO, complete RBAC, or WebSocket infrastructure without an approved architecture change. The V1-authorized additions (Chinese BM25 hybrid retrieval, RRF, BGE rerank, MinerU multi-call sharding, large-file ingestion, structured Parent/Child expansion, full Chinese UI, real answer streaming) are allowed only under their recorded Change Records (`CR-*` / `CC-*`) and the boundaries stated in the RAG, Ingestion, and API rules above.
