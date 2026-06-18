"""
NovaMind 工具基类

提供两种创建工具的方式：
  1. 装饰器模式：用 @novamind_tool 装饰简单函数
  2. 类继承模式：继承 NovaMindBaseTool 实现复杂有状态工具

类继承模式适合需要：
  - 数据库长连接
  - 内部状态管理
  - 权限控制
  - 超时配置
的高级场景。
"""
from typing import Any, Type
from langchain_core.tools import BaseTool, tool
from abc import ABC, abstractmethod
import asyncio
from pydantic import BaseModel

# 将 LangChain 原生的 @tool 装饰器重命名并暴露出去
# 开发者在写简单工具时，只需加一个装饰器和写好 docstring 即可
novamind_tool = tool


class NovaMindBaseTool(BaseTool, ABC):
    """
    NovaMind 标准工具基类

    如果你的工具需要复杂的初始化逻辑（比如维持一个数据库长连接），
    或者需要保存内部状态，请继承此类并实现 `_run` 方法。

    用法示例：
        class MyTool(NovaMindBaseTool):
            name: str = "my_tool"
            description: str = "我的自定义工具"
            args_schema: Type[BaseModel] = MyToolInput

            def _run(self, **kwargs):
                return "执行结果"
    """

    name: str
    description: str
    args_schema: Type[BaseModel]

    @abstractmethod
    def _run(self, **kwargs: Any) -> Any:
        """工具的同步执行逻辑，子类必须实现"""
        raise NotImplementedError("子类必须实现 _run 方法")

    async def _arun(self, **kwargs: Any) -> Any:
        """
        工具的异步执行逻辑（可选）

        如果工具涉及网络请求，强烈建议实现此方法。
        默认回退到同步执行。
        """
        return await asyncio.to_thread(self._run, **kwargs)
