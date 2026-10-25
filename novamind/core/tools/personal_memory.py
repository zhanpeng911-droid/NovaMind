"""Memory corrections shared by the CLI and both desktop conversation surfaces."""
from novamind.core.tools.base import novamind_tool
from novamind.core.personal import memory_allowed


def build_memory_tools():
    @novamind_tool
    def correct_memory(memory_id: str, content: str) -> str:
        """Correct a specific recalled memory when the user explicitly corrects it.

        memory_id is available in the recalled memory references. Preserve scope.
        """
        if not memory_allowed(write=True):
            return "本次对话不记录个人记忆。"
        from novamind.core.memory.governance_runtime import get_governance_service
        service = get_governance_service()
        old = service.store.get(memory_id)
        if old is None:
            return "没有找到该记忆，请在记忆中心选择。"
        updated = service.update_content(memory_id, content, expected_revision=old.revision)
        return f"记忆已修正：{updated.memory_id}"

    @novamind_tool
    def forget_memory(memory_id: str) -> str:
        """Stop using a specific memory when the user explicitly asks to forget it."""
        if not memory_allowed(write=True):
            return "本次对话不修改个人记忆，请从记忆中心操作。"
        from novamind.core.memory.governance_runtime import get_governance_service
        get_governance_service().delete(memory_id)
        return "已停止使用该记忆，并阻止旧后台任务恢复它。"

    return [correct_memory, forget_memory]
