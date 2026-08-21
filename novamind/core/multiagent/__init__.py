"""多 Agent 编排（委派外部 coding agent + fork 子副本 + 共享沙箱）。"""

from .types import (
    SpecialistRequest,
    SpecialistRawResult,
    SpecialistResult,
    SubagentRequest,
    SubagentResult,
    ArtifactRef,
    TokenUsage,
    SpecialistCapabilities,
    SpecialistCapability,
)
from .sandbox_binder import SandboxBinder, BoundSandbox, PerSubagentBinder
from .subagent import SubagentProvider
from .specialist import SpecialistAgent
from .exceptions import SpecialistError, SubagentError
from .tools import make_specialist_tool, make_subagent_tool, set_current_state, get_current_state

__all__ = [
    "SpecialistRequest",
    "SpecialistRawResult",
    "SpecialistResult",
    "SubagentRequest",
    "SubagentResult",
    "ArtifactRef",
    "TokenUsage",
    "SpecialistCapabilities",
    "SpecialistCapability",
    "SandboxBinder",
    "BoundSandbox",
    "PerSubagentBinder",
    "SubagentProvider",
    "SpecialistAgent",
    "SpecialistError",
    "SubagentError",
    "make_specialist_tool",
    "make_subagent_tool",
    "set_current_state",
    "get_current_state",
]
