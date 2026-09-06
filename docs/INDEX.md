# NovaMind Docs Index

This directory is the structured truth source for NovaMind's runtime harness layer.

Start here, then drill down by topic:

- runtime-overview.md: runtime responsibilities, boundaries, and operating model.
- sandbox-policy.md: zero-trust sandbox contract and prohibited actions.
- tool-contracts.md: builtin tool inventory and intended use boundaries.
- session-model.md: thread isolation, persistence, and context lifecycle.
- playbooks/file-edit.md: file-edit workflow inside the sandbox.
- NEXT_HARDENING_IMPLEMENTATION_PLAN.md: executable plan for wiring the sandbox into the default tool path and hardening WebUI concurrency, lifecycle, pagination, and API boundaries.
- FINALIZATION_REPAIR_DESIGN.md: follow-up design for atomic turn persistence, visible frontend failures, and bounded monitor updates after review of df8aa03.
- REAL_MODEL_LOAD_TEST_PLAN.md: real-provider load-test design covering isolation, cost controls, SSE metrics, staged workloads, and desktop acceptance criteria; execution pending.

Rules for agents:

1. Treat these docs as operational references, not user-facing prose.
2. Prefer the narrowest relevant document instead of overloading the prompt.
3. When runtime behavior conflicts with assumptions, trust the docs and the code over conversational guesswork.
