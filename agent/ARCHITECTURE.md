# Architecture and engineering decisions

This case study implements an explicit Python pipeline for a restaurant CRM assistant. Its central concern is the boundary between model-generated intent and executable operations.

## Request pipeline

1. **Rewrite:** resolve conversational references using session history. The first turn can skip rewriting; subsequent requests can use a lighter model.
2. **Classify:** identify customer, reservation, order, or coupon domains through keywords and an LLM fallback.
3. **Select tools:** restrict the planning catalog to relevant domains, with a full-catalog fallback when needed.
4. **Plan:** produce JSON nodes for API calls, deterministic computation, verification, and read-only fan-out. Validate the structure with Pydantic and allow one JSON repair attempt.
5. **Execute:** resolve dependencies, run data operations, enforce risk checks, and pause destructive actions for confirmation. Replanning is bounded.
6. **Respond:** describe execution results, including missing information, unsupported requests, and partial effects.

## Typed plans and deterministic computation

The planner creates a structured execution graph rather than issuing arbitrary tool calls in an open-ended loop. Python implements filtering, lookup, counting, and sorting so that response generation does not need to recompute values.

**Trade-off:** explicit plans improve inspectability and make planning rules testable, while requiring custom graph execution and limiting flexibility to supported node types. Structural validation does not by itself prove that the chosen operations answer the user's question.

## Read/verify stages and writes

The executor uses sensing and action stages to perform reads and verification before deferred writes when dependencies allow. A write needed by later steps may still execute inline.

**Trade-off:** this reduces avoidable early mutations but does not make a multi-step request atomic. Failures after a write can leave partial effects.

## Confirmation and ambiguity

Destructive operations pass through risk controls and request explicit confirmation. The CLI uses `confirmo` for approval. Rejected confirmation prevents the pending destructive action.

Sensitive entity lookups with multiple candidates can return a clarification request. The existing disambiguation rule covers direct lookup-to-destructive-write paths; more complex dependency chains need additional coverage.

## Failure handling

| Failure | Behavior |
| --- | --- |
| Invalid plan JSON | Attempt one repair, then return a clear failure. |
| Relevant domain omitted | Permit bounded replanning with the full tool catalog. |
| Step fails before a mutation | Attempt bounded recovery where supported. |
| Verification fails | Halt the affected execution path. |
| Multiple sensitive lookup candidates | Request clarification. |
| Write fails or later work fails after a write | Report partial status and performed actions; suggest compensation where appropriate. |
| Unsupported menu or notification operation | Explain the missing capability and report any supported subset. |

Reads can use retry behavior for transient errors. Writes are not automatically retried because the API does not expose idempotency keys. Compensation is advisory; there is no general transactional rollback.

## Bounded concurrency and cost visibility

Read-only fan-out uses a thread pool with up to eight connections and a configured fan-out limit of 31 items. Mutations are excluded from fan-out. Oversized results expose a truncation marker and total information.

Lighter models can handle rewriting, classification, and memory-related tasks. Telemetry captures latency, token/cache usage, and provider-reported USD cost. This supports inspecting execution overhead without claiming a fixed cost or latency improvement.

## Knowledge and state

The repository includes product documentation, an ingestion utility, retrieval components, and in-process session memory. Session state is not persistent across restarts.

The tool catalog is static. An OpenAPI consistency test helps detect mismatches, but catalog maintenance remains part of the development workflow.

## Validation

The original project documentation records **31 benchmark scenarios and 127/127 checks**. The checks include API-derived expectations for reads, confirmation behavior, edge cases, and multi-turn interactions. See the [original documentation](https://github.com/GabrielNegreiro28/dionisio-agent/blob/cc097642cca5891609eac3a7fa1d763d202bc65a/agent/ARCHITECTURE.md).

Run `benchtest.py` to generate a fresh local report against the configured service. Focused tests cover data operations, planning rules, and OpenAPI/catalog synchronization. Follow [developer setup](README.md) for commands and prerequisites.

## Next engineering steps

- Persist sessions with an explicit lifecycle and storage model.
- Broaden ambiguity checks across multi-step action chains.
- Expand integration coverage for failures before and after writes.
- Introduce idempotent write semantics where the API supports them.
- Maintain tool/schema alignment as the external service changes.
