"""
NovaMind 事件总线

基于 asyncio.Queue 的异步事件分发系统。
用于解耦各组件间的通信：
  - 用户输入 -> 智能体处理
  - 定时任务触发 -> 智能体响应
  - 系统事件 -> 多个消费者并行处理
"""
import asyncio
from typing import Any, Callable


class EventBus:
    """
    异步事件总线

    用法：
        bus = EventBus()
        bus.register_handler("user_input", my_handler)
        await bus.emit("user_input", "你好")
    """

    def __init__(self, maxsize: int = 100):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._handlers: dict[str, list[Callable]] = {}

    async def emit(self, event_type: str, data: Any = None) -> None:
        """发布事件"""
        await self._queue.put({"type": event_type, "data": data})

    async def consume(self) -> dict:
        """消费一个事件（阻塞等待）"""
        return await self._queue.get()

    def task_done(self) -> None:
        """标记当前事件处理完成"""
        self._queue.task_done()

    @property
    def queue(self) -> asyncio.Queue:
        """获取底层队列（兼容旧版代码的 task_queue.put 调用）"""
        return self._queue

    def register_handler(self, event_type: str, handler: Callable) -> None:
        """注册事件处理器"""
        self._handlers.setdefault(event_type, []).append(handler)

    async def dispatch(self) -> None:
        """持续消费事件并分发给注册的处理器"""
        while True:
            event = await self._queue.get()
            event_type = event.get("type", "")
            handlers = self._handlers.get(event_type, [])
            for handler in handlers:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(event.get("data"))
                    else:
                        handler(event.get("data"))
                except Exception as e:
                    print(f"[EventBus] 处理事件 {event_type} 时出错: {e}")
            self._queue.task_done()


# 全局事件总线实例
event_bus = EventBus()
