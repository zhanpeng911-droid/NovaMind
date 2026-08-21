"""多 Agent 异常体系。"""

from __future__ import annotations


class MultiAgentError(Exception):
    """多 Agent 错误基类。"""


class SpecialistError(MultiAgentError):
    """specialist 调用失败。"""


class SpecialistTimeoutError(SpecialistError):
    """specialist 超时。"""


class SpecialistCredentialError(SpecialistError):
    """specialist 凭证缺失。"""


class SubagentError(MultiAgentError):
    """subagent 调用失败。"""


class SubagentTimeoutError(SubagentError):
    """subagent 超时。"""


class SubagentMaxStepsError(SubagentError):
    """subagent 达到最大步数。"""
