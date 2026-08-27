"""
NovaMind 插件加载器（懒加载 + 热更新）

提供动态技能/插件的发现、加载和管理：
  1. 启动时只扫描元数据（名称、描述），不加载完整内容
  2. 首次调用插件时才加载完整内容并缓存
  3. 支持热更新：修改插件文件后自动重新加载
  4. 支持插件生命周期钩子（on_load, on_unload）
  5. 零信任执行模式：先读说明书，再决定是否执行
  6. 兼容 OpenClaw 和 Claude Code 技能格式
"""
from __future__ import annotations
import os
import re
import time
from typing import Any, Callable
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool
from functools import lru_cache

from .config import SKILLS_DIR, OFFICE_DIR
from .tools.sandbox_tools import execute_office_shell


class DynamicSkillInput(BaseModel):
    """动态技能工具的输入参数模型"""
    mode: str = Field(
        description="必须是 'help' 或 'run'。"
        "第一次使用时强烈建议先传入 'help' 阅读说明书。"
    )
    command: str = Field(
        default="",
        description="仅在 mode='run' 时需要。你要执行的完整命令，保留 {baseDir} 占位符。"
    )


class PluginInfo:
    """
    插件元信息

    存储从 SKILL.md / README.md 中解析出的元数据。
    """
    def __init__(
        self,
        folder: str,
        name: str,
        raw_name: str,
        description: str,
        md_path: str,
        run_dir: str | None,
        version: str = "1.0.0",
        enabled: bool = True,
    ):
        self.folder = folder
        self.name = name
        self.raw_name = raw_name
        self.description = description
        self.md_path = md_path
        self.run_dir = run_dir
        self.version = version
        self.enabled = enabled
        self.mtime = os.path.getmtime(md_path) if os.path.exists(md_path) else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "folder": self.folder,
            "name": self.name,
            "raw_name": self.raw_name,
            "description": self.description,
            "md_path": self.md_path,
            "run_dir": self.run_dir,
            "version": self.version,
            "enabled": self.enabled,
        }


class PluginManager:
    """
    插件管理器

    提供插件的发现、加载、缓存和生命周期管理。
    兼容三种技能格式：
      - NovaMind 原生格式（SKILL.md）
      - OpenClaw 格式（SKILL.md / README.md）
      - Claude Code 格式（.claude/skills/ 目录下的指令文件）

    用法：
        pm = PluginManager()
        tools = pm.get_all_tools()        # 获取所有插件工具（懒加载）
        pm.disable_plugin("my_skill")      # 禁用指定插件
        pm.reload_all()                    # 强制重新加载所有插件
    """

    def __init__(self, cache_size: int = 50, scan_interval: int = 60):
        self._plugins: dict[str, PluginInfo] = {}
        self._cache_size = cache_size
        self._last_scan_time: float = 0
        self._scan_interval = scan_interval
        self._on_load_hooks: list[Callable] = []
        self._on_unload_hooks: list[Callable] = []
        # 额外的技能扫描目录（支持 OpenClaw / Claude Code 生态）
        self._extra_scan_dirs: list[str] = []

    def register_hook(self, event: str, callback: Callable) -> None:
        """
        注册插件生命周期钩子

        支持的事件：
          - on_load: 插件首次加载时触发
          - on_unload: 插件卸载/禁用时触发
        """
        if event == "on_load":
            self._on_load_hooks.append(callback)
        elif event == "on_unload":
            self._on_unload_hooks.append(callback)

    def _scan_plugins(self, force_rescan: bool = False) -> dict[str, PluginInfo]:
        """
        扫描插件目录，只提取元数据

        扫描范围：
          1. NovaMind/OpenClaw 原生格式（SKILLS_DIR 下的 SKILL.md/README.md）
          2. 额外注册的扫描目录（OpenClaw/Claude Code 生态）

        扫描结果会缓存 scan_interval 秒，避免频繁IO。
        """
        current_time = time.time()

        if (
            not force_rescan
            and self._plugins
            and current_time - self._last_scan_time < self._scan_interval
        ):
            return self._plugins

        plugins: dict[str, PluginInfo] = {}

        # 扫描目录列表：主目录 + 额外目录
        scan_dirs = [SKILLS_DIR] + self._extra_scan_dirs

        for scan_dir in scan_dirs:
            if not os.path.exists(scan_dir):
                continue

            for item in os.listdir(scan_dir):
                folder_path = os.path.join(scan_dir, item)
                if not os.path.isdir(folder_path):
                    continue

                # 按优先级查找技能描述文件
                md_path = None
                for candidate in ["SKILL.md", "README.md", "skill.md", "readme.md"]:
                    candidate_path = os.path.join(folder_path, candidate)
                    if os.path.exists(candidate_path):
                        md_path = candidate_path
                        break

                if not md_path:
                    continue

                try:
                    metadata = self._extract_metadata(md_path)
                    if metadata:
                        # 用目录名作为唯一标识，避免跨目录重名冲突
                        unique_folder = f"{os.path.basename(scan_dir)}_{item}"
                        run_dir = None
                        try:
                            office_root = os.path.abspath(OFFICE_DIR)
                            real_folder = os.path.abspath(folder_path)
                            if os.path.commonpath([office_root, real_folder]) == office_root:
                                run_dir = os.path.relpath(real_folder, office_root).replace("\\", "/")
                        except ValueError:
                            run_dir = None
                        plugin = PluginInfo(
                            folder=unique_folder,
                            md_path=md_path,
                            run_dir=run_dir,
                            **metadata,
                        )
                        # 不覆盖已存在的同名插件（先扫描的优先）
                        if plugin.name not in plugins:
                            plugins[plugin.name] = plugin
                except Exception as e:
                    print(f" [警告] 扫描插件 {item} 失败: {e}")

        self._plugins = plugins
        self._last_scan_time = current_time

        if plugins:
            print(f" [OK] 扫描到 {len(plugins)} 个插件（懒加载模式，兼容 OpenClaw/Claude Code 格式）")

        return plugins

    def _extract_metadata(self, md_path: str) -> dict[str, str] | None:
        """从插件文件中提取元数据（只读取前50行）"""
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                lines = []
                for i, line in enumerate(f):
                    if i >= 50:
                        break
                    lines.append(line)
                content = "\n".join(lines)

            name_match = re.search(r"^name:\s*(.+)$", content, re.MULTILINE)
            desc_match = re.search(r"^description:\s*(.+)$", content, re.MULTILINE)
            version_match = re.search(r"^version:\s*(.+)$", content, re.MULTILINE)

            raw_name = (
                name_match.group(1).strip()
                if name_match
                else os.path.basename(os.path.dirname(md_path))
            )
            tool_name = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_name)

            raw_desc = (
                desc_match.group(1).strip()
                if desc_match
                else f"提供 {raw_name} 相关功能"
            )
            if raw_desc.startswith(('"', "'")) and raw_desc.endswith(('"', "'")):
                raw_desc = raw_desc[1:-1]

            version = version_match.group(1).strip() if version_match else "1.0.0"

            return {
                "raw_name": raw_name,
                "name": tool_name,
                "description": raw_desc,
                "version": version,
            }
        except Exception as e:
            print(f" [警告] 提取元数据失败 {md_path}: {e}")
            return None

    @lru_cache(maxsize=50)
    def _load_content(self, md_path: str, mtime: float) -> str:
        """加载插件完整内容（带LRU缓存）"""
        with open(md_path, "r", encoding="utf-8") as f:
            return f.read()

    def _create_tool(self, plugin: PluginInfo) -> StructuredTool:
        """为插件创建懒加载工具对象"""
        manager = self

        def lazy_runner(mode: str, command: str = "") -> str:
            """懒加载执行器：首次调用时才加载完整内容"""
            if mode == "help":
                # 懒加载：首次调用时才读取完整内容
                content = manager._load_content(plugin.md_path, plugin.mtime)

                # 触发 on_load 钩子
                for hook in manager._on_load_hooks:
                    try:
                        hook(plugin)
                    except Exception:
                        pass

                return (
                    f"========== 【{plugin.raw_name} 完整说明书】 ==========\n"
                    f"版本: {plugin.version}\n"
                    f"{content[:3000]}\n"
                    f"====================================\n"
                    f"提示：请根据以上说明，如果觉得能解决问题，就将 mode 设为 'run'，"
                    f"并将拼装好的执行命令填入 command 重新调用。"
                )
            elif mode == "run":
                if not command:
                    return "错误：在 'run' 模式下，必须提供 command 参数！"
                if not plugin.run_dir:
                    return "错误：该插件不在 office 工位内，出于沙盒安全限制无法执行 run 命令。"
                actual_cmd = command.replace("{baseDir}", plugin.run_dir)
                return execute_office_shell.invoke({"command": actual_cmd})
            else:
                return "错误：mode 参数只能是 'help' 或 'run'。"

        mini_description = (
            f"{plugin.description}\n\n"
            f"版本: {plugin.version} | "
            f"注意：这是一个外部扩展插件。首次使用请务必先传入 `mode='help'` 来阅读完整说明书，"
            f"之后再使用 `mode='run'` 配合 `command` 执行底层脚本。"
        )

        return StructuredTool.from_function(
            func=lazy_runner,
            name=plugin.name,
            description=mini_description,
            args_schema=DynamicSkillInput,
        )

    def get_all_tools(self, force_rescan: bool = False) -> list[StructuredTool]:
        """获取所有已启用插件的工具对象"""
        plugins = self._scan_plugins(force_rescan=force_rescan)
        tools = []
        for plugin in plugins.values():
            if plugin.enabled:
                tools.append(self._create_tool(plugin))
        return tools

    def get_plugin_count(self) -> int:
        """获取插件总数（不触发扫描）"""
        return len(self._scan_plugins())

    def get_enabled_count(self) -> int:
        """获取已启用的插件数"""
        return len([p for p in self._scan_plugins().values() if p.enabled])

    def disable_plugin(self, name: str) -> bool:
        """禁用指定插件"""
        plugins = self._scan_plugins()
        if name in plugins:
            plugins[name].enabled = False
            # 触发 on_unload 钩子
            for hook in self._on_unload_hooks:
                try:
                    hook(plugins[name])
                except Exception:
                    pass
            return True
        return False

    def enable_plugin(self, name: str) -> bool:
        """启用指定插件"""
        plugins = self._scan_plugins()
        if name in plugins:
            plugins[name].enabled = True
            return True
        return False

    def reload_all(self) -> list[StructuredTool]:
        """强制重新扫描并清除缓存"""
        # lru_cache 包装器的 cache_clear 挂在函数上，经实例访问会变成绑定方法
        # （没有 cache_clear 属性），必须用类级别访问。
        type(self)._load_content.cache_clear()
        self._plugins.clear()
        return self.get_all_tools(force_rescan=True)

    def list_plugins(self) -> list[dict[str, Any]]:
        """列出所有插件信息"""
        plugins = self._scan_plugins()
        return [p.to_dict() for p in plugins.values()]

    def add_scan_dir(self, dir_path: str) -> None:
        """
        注册额外的技能扫描目录

        支持 OpenClaw 和 Claude Code 生态的技能目录。
        例如：
            pm.add_scan_dir("~/.openclaw/skills")
            pm.add_scan_dir("~/.claude/skills")
        """
        expanded = os.path.expanduser(dir_path)
        if expanded not in self._extra_scan_dirs:
            self._extra_scan_dirs.append(expanded)
            # 清除缓存以触发重新扫描
            self._plugins.clear()
            self._last_scan_time = 0


# ==================== 便捷函数 ====================

_plugin_manager = PluginManager()


def load_dynamic_skills(force_rescan: bool = False) -> list[StructuredTool]:
    """加载所有动态插件技能"""
    return _plugin_manager.get_all_tools(force_rescan=force_rescan)


def reload_skills() -> list[StructuredTool]:
    """强制重新加载所有插件"""
    return _plugin_manager.reload_all()


def get_skill_count() -> int:
    """获取插件总数"""
    return _plugin_manager.get_plugin_count()


def clear_skill_cache():
    """清除插件缓存"""
    _plugin_manager._load_content.cache_clear()
    print(" [OK] 插件缓存已清除")
