# Runtime Overview

NovaMind is an auditable deep-research agent runtime.

Core runtime properties:

- Every significant step should be auditable through structured events.
- Tool execution is explicit and observable.
- Thread state is isolated by thread_id; long-term memory persists across threads.
- Long conversations are governed by a token budget with staged context shedding (externalize, summarize, circuit-break) instead of unbounded growth.
- The runtime is designed to expose what the agent did, why it did it, and under which boundaries.

Architecture layers:

- LLM routing with chained fallback: only transient errors (timeout, rate limit, 5xx) switch providers; the default provider always stays at the tail of the chain.
- Cross-cutting middleware on five hooks (before/after agent, before/after model, wrap tool call): memory recall, skill injection, context governance, orchestration.
- Five-layer long-term memory: schema, Ebbinghaus decay, BM25 retrieval (jieba for Chinese), per-call injection, background consolidation.
- Three-component sandbox (runtime + path translator + security guard): local zero-trust allowlist, docker mount-zone enforcement, warm pool.
- Three-layer skill system: SQLite store with version DAG and four counters, L2 evolution (mutate → gate → ratchet), L3 evaluation; 37 builtin skills.
- Multi-agent delegation (subagent fork and external specialist) sharing the parent sandbox.

Harness implications:

- Context should come from structured docs instead of one oversized prompt.
- Runtime decisions should be grounded in the narrowest relevant reference.
- Operators should be able to inspect both actions and the context pack used for those actions.
