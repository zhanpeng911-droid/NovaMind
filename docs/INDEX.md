# NovaMind Docs Index

This directory is the structured truth source for NovaMind's runtime harness layer.

Start here, then drill down by topic:

- ../NovaMind_Personal_Agent_任务书.md: current Personal Agent product and implementation taskbook; code-based gaps, preserved capabilities, phased work, and personal-use acceptance scenarios (2026-10-20).
- PERSONAL_AGENT_IMPLEMENTATION.md: initial implementation, unified desktop launch, images, schedules, memory controls and migration; that implementation round did not run runtime tests.
- GUI_REFINEMENT_REPORT.md: subsequent desktop visibility fixes, assistant entry, refined controls and real offline test evidence (2026-10-22).
- GUI_PERSONAL_POLISH_2026-10-22.md: screenshot follow-up and subsequent repairs; initial screenshot round did not run tests, later sections record offline/native verification separately.
- ITERATION_POLISH_2026-10-24.md: approved ten-item personal-use polish, authorized Pillow 13 upgrade, actual regressions and build verification.
- ../tasks/plan.md: next personal-use iteration; seven accepted improvements awaiting implementation and five NovaMind-specific capability proposals awaiting user selection (2026-10-24). Task tracking: ../tasks/todo.md.
- PERSONAL_SKILL_EVOLUTION_DESIGN.md: approved design for learning personal methods from repeated daily tasks; post-commit observation, review, budgeted validation, publication and rollback. Implemented offline; actual scope and limitations are recorded separately.
- PERSONAL_SKILL_EVOLUTION_DELIVERY_2026-10-25.md: implementation, user entry points, real offline regression/native checks, packaging status and outstanding real-model effect validation.
- DEPENDENCY_AUDIT_2026-10-22.md: historical and current native audit results, compatibility updates and unresolved reachable image risk; not a passed security gate.

- runtime-overview.md: runtime responsibilities, boundaries, and operating model.
- sandbox-policy.md: zero-trust sandbox contract and prohibited actions.
- tool-contracts.md: builtin tool inventory and intended use boundaries.
- session-model.md: thread isolation, persistence, and context lifecycle.
- playbooks/file-edit.md: file-edit workflow inside the sandbox.
- NEXT_HARDENING_IMPLEMENTATION_PLAN.md: executable plan for wiring the sandbox into the default tool path and hardening WebUI concurrency, lifecycle, pagination, and API boundaries.
- FINALIZATION_REPAIR_DESIGN.md: follow-up design for atomic turn persistence, visible frontend failures, and bounded monitor updates after review of df8aa03.
- REAL_MODEL_LOAD_TEST_PLAN.md: real-provider load-test design covering isolation, cost controls, SSE metrics, staged workloads, and desktop acceptance criteria; execution pending.
- PERSONAL_USE_FINALIZATION_PLAN.md: current lightweight follow-up scope for personal local use; budget safeguards, benchmark corrections, and five workflow checks instead of full load-test acceptance.
- B0_MEMORY_GOVERNANCE_AUDIT.md: memory chain audit, lifecycle states, tombstone/version barrier, migration and counterexample tests (Phase 0 / B0).
- adr/0001-desktop-shell.md: desktop shell selection ADR with measured PySide6 vs pywebview+ctypes data (Phase 0 / A0).
- PHASE0_ACCEPTANCE_REPORT.md: Phase 0 acceptance (A0+B0), evidence, and the precise A1/B1/B2/B3 task list.
- VNEXT_ITERATION_REPORT.md: full VNext acceptance (A1–A3 + B1–B3), API surface, security model, B3 evaluation metrics, and remaining manual checks.

Rules for agents:

1. Treat these docs as operational references, not user-facing prose.
2. Prefer the narrowest relevant document instead of overloading the prompt.
3. When runtime behavior conflicts with assumptions, trust the docs and the code over conversational guesswork.
