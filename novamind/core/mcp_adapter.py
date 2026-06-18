"""
NovaMind MCP (Model Context Protocol) 适配器

提供与 MCP 服务的集成能力：
  - 发现本地运行的 MCP 服务
  - 将 MCP 工具转换为 NovaMind 工具格式
  - 支持通过 mcporter 连接外部 MCP 服务
  - 支持通过 mcp-builder 构建自定义 MCP 服务

MCP 协议简介：
  MCP 是一个开放协议，允许 AI 模型与外部服务通信。
  每个 MCP 服务暴露一组"工具"（tool），AI 可以调用这些工具来
  与外部系统交互（如数据库、API、文件系统等）。
"""
from __future__ import annotations
import os
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Optional
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


_MCP_IO_POOL = ThreadPoolExecutor(max_workers=4)
MCP_READ_TIMEOUT_SECONDS = 15


class MCPToolInput(BaseModel):
    """MCP工具的通用输入格式"""
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="传递给 MCP 工具的参数字典"
    )


class MCPService:
    """
    MCP 服务连接器

    通过 stdio 与本地 MCP 服务进程通信。
    每个 MCP 服务是一个独立进程，通过 stdin/stdout 交换 JSON-RPC 消息。
    """

    def __init__(self, name: str, command: str, args: list[str] | None = None, env: dict | None = None):
        """
        Args:
            name: 服务名称
            command: 启动命令（如 "npx", "python"）
            args: 命令参数
            env: 环境变量
        """
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self._process: subprocess.Popen | None = None
        self._tools_cache: list[dict] | None = None
        self._request_id = 0  # 自增请求ID
        self._started = False  # 防止递归启动

    def _readline_with_timeout(self, timeout: int = MCP_READ_TIMEOUT_SECONDS) -> str:
        if self._process is None or self._process.stdout is None:
            return ""
        future = _MCP_IO_POOL.submit(self._process.stdout.readline)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            self.stop()
            return ""

    def _send_request(self, method: str, params: dict | None = None) -> dict:
        """向 MCP 服务发送 JSON-RPC 请求"""
        if self._process is None or self._process.poll() is not None:
            if not self._start():
                return {"error": {"code": -1, "message": f"MCP服务 {self.name} 启动失败"}}

        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or {},
        }

        try:
            self._process.stdin.write(json.dumps(request) + "\n")
            self._process.stdin.flush()

            response_line = self._readline_with_timeout()
            if response_line:
                return json.loads(response_line)
        except Exception as e:
            return {"error": {"code": -1, "message": str(e)}}

        return {"error": {"code": -1, "message": "无响应"}}

    def _start(self) -> bool:
        """启动 MCP 服务进程，成功返回 True，失败返回 False"""
        if self._started:
            return False
        self._started = True

        try:
            full_env = os.environ.copy()
            full_env.update(self.env)

            self._process = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=full_env,
                text=True,
            )

            # 初始化握手（使用独立的请求ID，不走 _send_request 避免递归）
            self._request_id = 0
            init_request = {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "NovaMind", "version": "2.0.0"},
                },
            }
            self._process.stdin.write(json.dumps(init_request) + "\n")
            self._process.stdin.flush()
            init_response = self._readline_with_timeout()
            if not init_response:
                raise TimeoutError(f"MCP服务 {self.name} 握手超时")
            self._request_id = 1  # 后续请求从1开始
            return True
        except Exception as e:
            print(f" [警告] MCP服务 {self.name} 启动失败: {e}")
            self.stop()
            return False

    def list_tools(self) -> list[dict]:
        """获取服务暴露的所有工具"""
        if self._tools_cache is not None:
            return self._tools_cache

        response = self._send_request("tools/list")
        tools = response.get("result", {}).get("tools", [])
        self._tools_cache = tools
        return tools

    def call_tool(self, name: str, arguments: dict) -> str:
        """调用指定工具"""
        response = self._send_request("tools/call", {
            "name": name,
            "arguments": arguments,
        })

        result = response.get("result", {})
        if "content" in result:
            contents = result["content"]
            texts = [c.get("text", "") for c in contents if c.get("type") == "text"]
            return "\n".join(texts) if texts else json.dumps(result)

        if "error" in response:
            return f"MCP 工具调用失败: {response['error'].get('message', '未知错误')}"

        return json.dumps(result)

    def stop(self):
        """停止 MCP 服务进程"""
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
        self._started = False


class MCPManager:
    """
    MCP 服务管理器

    管理多个 MCP 服务连接，将它们的工具转换为 NovaMind 工具格式。

    用法：
        manager = MCPManager()
        manager.register("tavily", "npx", ["tavily-mcp"])
        tools = manager.get_all_tools()
    """

    def __init__(self):
        self._services: dict[str, MCPService] = {}

    def register(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict | None = None,
    ) -> None:
        """注册一个 MCP 服务"""
        self._services[name] = MCPService(name, command, args, env)

    def unregister(self, name: str) -> None:
        """注销一个 MCP 服务"""
        if name in self._services:
            self._services[name].stop()
            del self._services[name]

    def get_all_tools(self) -> list[StructuredTool]:
        """获取所有已注册 MCP 服务的工具（转换为 NovaMind 工具格式）"""
        tools = []
        for service in self._services.values():
            try:
                mcp_tools = service.list_tools()
                for mcp_tool in mcp_tools:
                    tool = self._wrap_mcp_tool(service, mcp_tool)
                    tools.append(tool)
            except Exception as e:
                print(f" [警告] 加载 MCP 服务 {service.name} 的工具失败: {e}")
        return tools

    def _wrap_mcp_tool(self, service: MCPService, mcp_tool: dict) -> StructuredTool:
        """将 MCP 工具包装为 NovaMind StructuredTool"""
        tool_name = mcp_tool.get("name", "unknown")
        tool_desc = mcp_tool.get("description", f"MCP工具: {tool_name}")
        prefixed_name = f"mcp_{service.name}_{tool_name}"

        # 从 MCP 工具的 inputSchema 生成参数描述
        input_schema = mcp_tool.get("inputSchema", {})
        properties = input_schema.get("properties", {})
        args_desc = "\n".join(
            f"  - {k}: {v.get('description', '')}" for k, v in properties.items()
        )
        full_desc = f"[MCP/{service.name}] {tool_desc}"
        if args_desc:
            full_desc += f"\n参数:\n{args_desc}"

        def runner(arguments: str = "{}") -> str:
            """MCP工具执行器"""
            try:
                args = json.loads(arguments) if isinstance(arguments, str) else arguments
            except json.JSONDecodeError:
                args = {"input": arguments}
            return service.call_tool(tool_name, args)

        return StructuredTool.from_function(
            func=runner,
            name=prefixed_name,
            description=full_desc,
        )

    def stop_all(self):
        """停止所有 MCP 服务"""
        for service in self._services.values():
            service.stop()


# 全局 MCP 管理器实例
_mcp_manager = MCPManager()


def get_mcp_manager() -> MCPManager:
    """获取全局 MCP 管理器"""
    return _mcp_manager


def load_mcp_tools() -> list[StructuredTool]:
    """加载所有已注册的 MCP 工具"""
    return _mcp_manager.get_all_tools()
