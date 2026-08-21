"""PiSpecialist — 外部 CLI coding specialist。

吸收 Poirot `specialists/pi_specialist.py`：specialist 黑盒，自带 model + ReAct loop，
通过 subprocess 调 pi CLI。

组合 PiRuntime（完整委派：完整 prompt + 凭证透传 + 错误归一化 + duration 统计）。
说明：真实运行需 pi CLI 安装；未安装时 invoke 抛 SpecialistError。
（pi 0.84.x 不支持 --sandbox-url，故不做沙箱透传。）
"""

from __future__ import annotations

from typing import Any

from ..exceptions import SpecialistError
from ..runtimes.pi_runtime import PiRuntime, PiRuntimeConfig
from ..types import (
    SpecialistCapabilities,
    SpecialistCapability,
    SpecialistRawResult,
    SpecialistRequest,
)


class PiSpecialist:
    """Pi coding specialist（组合 PiRuntime，name=pi, capability=CODING）。"""

    def __init__(
        self,
        runtime: PiRuntime | None = None,
        credential: Any | None = None,
        *,
        command: str | list[str] = "pi",
    ) -> None:
        self._runtime = runtime or PiRuntime(PiRuntimeConfig(command=command))
        self._credential = credential  # 凭证由 bootstrap 检测，仅供 runtime 用

    @property
    def name(self) -> str:
        return "pi"

    @property
    def capabilities(self) -> SpecialistCapabilities:
        return SpecialistCapabilities(capabilities=(SpecialistCapability.CODING,))

    def invoke(self, request: SpecialistRequest) -> SpecialistRawResult:
        try:
            return self._runtime.invoke(request)
        except SpecialistError:
            raise
        except Exception as exc:
            raise SpecialistError(f"pi specialist crashed: {exc}") from exc
