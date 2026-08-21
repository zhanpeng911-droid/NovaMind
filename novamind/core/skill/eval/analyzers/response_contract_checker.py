"""ResponseContractChecker — 响应层 eval（跑 contract 规则产 EvalResult）。"""

from __future__ import annotations

from . import checks
from .contract_compiler import ContractCompiler


class ResponseContractChecker:
    def __init__(self, compiler: ContractCompiler | None = None) -> None:
        self._compiler = compiler or ContractCompiler()

    def check(self, candidate_content: str, baseline_content: str):
        # 延迟导入破循环：evolution/__init__ → programmatic_bridge → 本模块 → evolution.types
        from ...evolution.types import EvalEvidence, EvalResult

        rules = self._compiler.compile(candidate_content)

        evidence: list[EvalEvidence] = []
        hard_failures: list[str] = []
        passed = 0
        total = 0

        for rule in rules:
            total += 1
            cand_pass = self._run_rule(rule.rule_id, candidate_content, rule.params)
            base_pass = self._run_rule(rule.rule_id, baseline_content, rule.params) if baseline_content else True
            if cand_pass:
                passed += 1
            if rule.hard and not cand_pass:
                hard_failures.append(rule.rule_id)
            evidence.append(EvalEvidence(
                kind="programmatic_rule", rule_name=rule.rule_id,
                baseline_pass=base_pass, candidate_pass=cand_pass,
            ))

        score = passed / total if total else 0.0
        recommendation = "reject" if hard_failures else "accept"
        return EvalResult(
            score=score, metric="hard", hard_failures=tuple(hard_failures),
            evidence=tuple(evidence), confidence=0.7, recommendation=recommendation,
        )

    @staticmethod
    def _run_rule(rule_id: str, content: str, params: dict) -> bool:
        if rule_id == "nonempty":
            return checks.check_nonempty(content)
        if rule_id == "json_parseable":
            return checks.check_json_parseable(content)
        if rule_id == "must_cite":
            return checks.check_must_cite(content)
        if rule_id == "lead_with_conclusion":
            return checks.check_lead_with_conclusion(content)
        if rule_id == "paragraph_limit":
            return checks.check_paragraph_limit(content)
        if rule_id == "no_unfounded_claims":
            return checks.check_no_unfounded_claims(content)
        if rule_id == "semantic_density":
            if not content:
                return False
            density = checks.semantic_density(content)
            return checks.SEMANTIC_DENSITY_MIN <= density <= checks.SEMANTIC_DENSITY_MAX
        return True
