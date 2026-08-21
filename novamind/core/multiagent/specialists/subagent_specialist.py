"""SubagentSpecialist — 适配 SubagentProvider 为 SpecialistAgent。"""

from __future__ import annotations

from ..subagent import SubagentProvider
from ..types import (
    SpecialistCapabilities,
    SpecialistCapability,
    SpecialistRawResult,
    SpecialistRequest,
    SubagentRequest,
)


class SubagentSpecialist:
    """把 SubagentProvider 包装为 SpecialistAgent（self-copy 叶子任务）。"""

    def __init__(self, provider: SubagentProvider) -> None:
        self._provider = provider

    @property
    def name(self) -> str:
        return "subagent"

    @property
    def capabilities(self) -> SpecialistCapabilities:
        return SpecialistCapabilities(capabilities=(SpecialistCapability.CODING, SpecialistCapability.RESEARCH))

    def invoke(self, request: SpecialistRequest) -> SpecialistRawResult:
        sub_request = SubagentRequest(
            goal=request.goal,
            success_criteria=request.success_criteria,
            context_summary=request.context_summary,
            sandbox_id=request.sandbox_id,
            artifacts_path=request.artifacts_path,
            max_steps=request.max_steps,
            timeout_seconds=request.timeout_seconds,
            allowed_tools=request.allowed_tools,
            skill_injection=request.skill_injection,
        )
        result = self._provider.spawn(sub_request)
        return SpecialistRawResult(
            raw_output=result.summary,
            artifacts=result.artifacts,
            usage=result.usage,
            duration_seconds=result.duration_seconds,
        )
