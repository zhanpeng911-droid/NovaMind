"""
NovaMind 基础用法示例

展示如何在代码中直接使用NovaMind智能体，
无需通过CLI启动。
"""
import asyncio
import os
from dotenv import load_dotenv

# 加载环境配置
load_dotenv()

from novamind.core.agent import create_agent_app


async def main():
    """创建智能体并进行对话"""
    # 从环境变量读取配置
    provider = os.getenv("DEFAULT_PROVIDER", "openai")
    model = os.getenv("DEFAULT_MODEL", "gpt-4o-mini")

    print(f"正在初始化 NovaMind (provider={provider}, model={model})...")

    # 创建智能体应用
    agent = create_agent_app(
        provider_name=provider,
        model_name=model,
    )

    # 简单对话循环
    print("NovaMind 已就绪！输入 'quit' 退出。\n")

    while True:
        try:
            user_input = input("你: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ["quit", "exit", "q"]:
                print("再见！")
                break

            # 使用流式接口获取响应
            async for event in agent.astream(user_input, thread_id="example_session"):
                for node_name, node_data in event.items():
                    if node_name == "agent":
                        messages = node_data.get("messages", [])
                        if messages:
                            last_msg = messages[-1]
                            if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                                for tc in last_msg.tool_calls:
                                    print(f"  [调用工具: {tc['name']}]")
                            elif last_msg.content:
                                print(f"NovaMind: {last_msg.content}\n")

        except KeyboardInterrupt:
            print("\n再见！")
            break
        except Exception as e:
            print(f"发生错误: {e}\n")


if __name__ == "__main__":
    asyncio.run(main())
