"""ProgrammaticEvalBridge — 兼容 facade（委托 ResponseContractChecker）。"""

from __future__ import annotations

from ..types import EvalContext, EvalResult
from ...eval.analyzers import checks
from ...eval.analyzers.contract_compiler import ContractCompiler
from ...eval.analyzers.response_contract_checker import ResponseContractChecker


class ProgrammaticEvalBridge:
    def __init__(self) -> None:
        self._checker = ResponseContractChecker(ContractCompiler())

    def evaluate(self, ctx: EvalContext) -> EvalResult:
        candidate_content = checks.read_content(ctx.candidate)
        baseline_content = checks.read_content(ctx.baseline) if ctx.baseline else ""
        return self._checker.check(candidate_content, baseline_content)
