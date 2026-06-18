# NovaMind

透明化 AI 智能体框架 — 让 AI 的每一步决策都可追溯、可审计、可控制

自定义状态机引擎 + 中间件管道 + Token 成本追踪

[快速开始](#快速开始) · [核心能力](#核心能力) · [架构设计](#系统架构)

---

## 简介

NovaMind 是一个**透明可控的 AI 智能体框架**，核心设计理念：

- **白盒化决策** — 6 类事件审计 + JSONL 日志 + Rich 监控终端，所有行为可追溯
- **持续学习** — 分层记忆机制（长期画像 + 近期摘要），跨会话保持上下文
- **成本可控** — Token 追踪与成本估算，每次调用花了多少钱一目了然
- **中间件管道** — 可插拔的请求拦截器，计时/日志/限流自由组合
- **多会话隔离** — 按 thread_id 隔离状态、日志、持久化，支持多会话并行

### 核心能力

| 能力 | 说明 |
|------|------|
| **🧠 分层记忆** | 长期画像（`user_profile.md`）+ 对话摘要（SQLite 持久化）；超过 40 轮后保留最近 10 轮，其余压缩为摘要 |
| **🔍 全行为审计** | 6 类事件（`llm_input`, `tool_call`, `tool_result`, `token_usage`, `ai_message`, `system_action`）写入 JSONL |
| **📊 Token 成本追踪** | 按模型定价自动估算成本，按 thread_id 独立统计 |
| **🔌 中间件管道** | 洋葱模型拦截器，计时/日志/限流可插拔 |
| **🖥️ 多会话隔离** | SQLite + JSONL 按 thread_id 隔离，支持会话恢复 |
| **🛡️ 沙盒执行** | 文件操作和 Shell 命令限制在 office 工位内，白名单命令机制 |

---

## 快速开始

### 1. 安装

```bash
# 克隆项目
git clone <your-repo-url>
cd NovaMind

# 推荐使用虚拟环境
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 安装默认依赖（支持 OpenAI / Anthropic / OpenAI 兼容接口）
pip install -e .

# 如需使用 Ollama 本地模型，额外安装可选依赖
pip install -e ".[ollama]"
```

**依赖说明：**

| 依赖组 | 包含内容 | 安装方式 |
|--------|----------|----------|
| 默认依赖 | OpenAI / Anthropic / OpenAI 兼容 provider、CLI、状态机、中间件 | `pip install -e .` |
| `[ollama]` | langchain-community（Ollama 本地模型支持） | `pip install -e ".[ollama]"` |
| `[dev]` | pytest、pytest-asyncio、pytest-cov | `pip install -e ".[dev]"` |

> 缺少 provider 依赖时，运行时会抛出 `ImportError` 并给出安装指引。

### 2. 配置

```bash
# 启动交互式配置向导
novamind config
```

配置向导会引导你：
1. 选择模型提供商
2. 输入 API Key
3. 配置 Base URL（可选）
4. 自动测试连接

或手动编辑 `.env` 文件：

```bash
DEFAULT_PROVIDER=openai
DEFAULT_MODEL=gpt-4o-mini
OPENAI_API_KEY=sk-your-api-key-here
# OPENAI_API_BASE=https://api.openai.com/v1  # 可选，代理时配置
```

**支持的提供商：**

| 提供商 | provider 名称 | 依赖 | 说明 |
|--------|--------------|------|------|
| OpenAI | `openai` | 默认安装 | GPT 系列 |
| Anthropic | `anthropic` | 默认安装 | Claude 系列 |
| 阿里云通义千问 | `aliyun` | 默认安装 | OpenAI 兼容接口 |
| 腾讯混元 | `tencent` | 默认安装 | OpenAI 兼容接口 |
| 智谱 AI | `z.ai` | 默认安装 | OpenAI 兼容接口 |
| Ollama | `ollama` | 需 `[ollama]` extra | 本地模型 |
| 其他 OpenAI 兼容 | `other` | 默认安装 | 自定义 BASE_URL |

### 3. 运行

```bash
# 启动新会话（每次运行自动创建唯一 thread_id）
novamind run

# 恢复指定会话
novamind run --thread-id my_session
```

### 4. 监控

```bash
# 监控最近有日志写入的会话
novamind monitor

# 监控指定会话
novamind monitor --thread-id my_session

# 列出所有可监控的会话
novamind monitor --list
```

### 5. 基本用法

| 类型 | 命令示例 | 说明 |
|------|----------|------|
| 时间查询 | `现在几点了？` | 获取当前时间 |
| 数学计算 | `帮我算一下 25 乘以 48` | AST 安全计算器 |
| 定时任务 | `每天早上 8 点提醒我喝水` | 创建循环任务 |
| 文件操作 | `看看 office 里有什么文件` | 列出工位文件 |
| 读取文件 | `读取 readme.txt` | 读取文件内容 |
| 退出 | `/exit` | 退出程序 |

> 文件操作和 Shell 命令受限于 office 沙盒，详见 [安全边界](#-安全边界)。

---

## 系统架构

```
                          ┌─────────────────────────┐
                          │      用户输入终端         │
                          │   prompt_toolkit + Rich  │
                          └───────────┬─────────────┘
                                      │
                                      ▼
                          ┌─────────────────────────┐
                          │      事件总线 EventBus    │
                          └───────────┬─────────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                   ▼
          ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
          │  用户输入处理  │  │  心跳任务引擎  │  │  监控面板     │
          └──────┬───────┘  └──────┬───────┘  └──────────────┘
                 │                 │
                 └─────────────────┼───────────────────┘
                                   ▼
                    ┌──────────────────────────────┐
                    │      智能体状态机 Engine       │
                    │   自定义异步有向图 (非 LangGraph) │
                    └──────────────┬───────────────┘
                                   │
                    ┌──────────────┼───────────────┐
                    ▼              ▼                ▼
          ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
          │  LLM 推理节点  │ │  工具执行节点  │ │  上下文管理   │
          └──────────────┘ └──────────────┘ └──────────────┘
                                   │
                    ┌──────────────┼───────────────┐
                    ▼              ▼                ▼
          ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
          │  12个内置工具  │ │  动态插件系统  │ │ 可选 MCP 工具 │
          └──────────────┘ └──────────────┘ └──────────────┘
```

### 项目结构

```
NovaMind/
├── novamind/                      # 核心 Python 包
│   └── core/
│       ├── agent.py               # 智能体组装（状态机+中间件+工具）
│       ├── state_machine.py       # 自定义异步状态机 + SQLite 持久化
│       ├── middleware.py          # 中间件管道（计时、日志、限流）
│       ├── event_bus.py           # 异步事件总线
│       ├── config.py              # 全局路径配置
│       ├── context.py             # 上下文管理（裁剪、摘要、系统提示词）
│       ├── provider.py            # 多模型适配器（工厂模式）
│       ├── token_tracker.py       # Token 追踪与成本估算
│       ├── heartbeat.py           # 心跳调度器（定时任务）
│       ├── logger.py              # 异步审计日志（JSONL）
│       ├── plugin_loader.py       # 懒加载插件系统
│       ├── mcp_adapter.py         # MCP 服务适配器（需先注册服务）
│       └── tools/
│           ├── base.py            # 工具基类 + 装饰器
│           ├── builtins.py        # 12 个内置工具
│           └── sandbox_tools.py   # 沙盒文件/Shell 工具
├── entry/                         # 应用入口
│   ├── main.py                    # 主循环（用户输入+Agent处理+心跳）
│   ├── cli.py                     # CLI 命令（config/run/monitor）
│   └── monitor.py                 # Rich 实时监控面板
├── tests/                         # 测试套件（114 个测试）
├── examples/                      # 使用示例
├── Dockerfile                     # Docker 部署
├── pyproject.toml                 # Python 包配置
└── requirements.txt               # 依赖列表
```

---

## 内置工具

| 工具 | 功能 | 说明 |
|------|------|------|
| `get_current_time` | 获取当前时间 | |
| `calculator` | AST 安全数学计算器 | 只允许数字和四则运算 |
| `save_user_profile` | 更新用户画像 | 写入 user_profile.md |
| `get_system_model_info` | 获取模型信息 | |
| `schedule_task` | 创建定时任务 | 支持 hourly/daily/weekly/monthly |
| `list_scheduled_tasks` | 查看任务列表 | |
| `delete_scheduled_task` | 删除任务 | |
| `modify_scheduled_task` | 修改任务 | |
| `list_office_files` | 列出工位文件 | 受沙盒限制 |
| `read_office_file` | 读取文件 | 受沙盒限制，超过 10000 字符时截断 |
| `write_office_file` | 写入文件 | 受沙盒限制 |
| `execute_office_shell` | 执行 Shell 命令 | 白名单命令，详见安全边界 |

---

## 插件系统

NovaMind 默认从 `workspace/office/skills/` 目录加载动态技能插件。

**技能格式要求：**
- 每个技能是一个目录，包含 `SKILL.md` 或 `README.md` 描述文件
- 建议在描述文件中提供 `name:` 和 `description:` 字段；缺失时会回退为目录名和默认描述
- 技能通过两段式调用：`mode='help'` 读取说明书，`mode='run'` 执行命令

**沙盒限制：**
- `mode='run'` 的命令必须位于 office 工位内
- 命令通过 `execute_office_shell` 执行，受白名单限制
- 不在 office 内的技能无法执行 `run` 模式

> 插件系统兼容 SKILL.md 格式。用户可参考该格式编写自定义技能，但外部生态的技能需要适配沙盒限制才能运行。

---

## 安全边界

### 文件访问

- 所有文件操作限制在 `workspace/office/` 目录内
- 路径穿越攻击（`../`）被拦截
- 文件读取超过 10000 字符时会截断

### Shell 命令

- **白名单机制**：只允许 `pwd`, `echo`, `ls/dir`, `cat/type`, `mkdir`
- 拦截 shell 元字符（`&`, `|`, `;`, `<`, `>`等）
- 拦截环境变量展开（`$HOME`, `%PATH%`等）
- 所有命令在 office 工位内执行

> 当前 Shell 工具是安全白名单模式，不是任意命令执行器。

---

## 测试

项目包含 114 个自动化测试，分为三层：

| 层级 | 文件 | 测试数 | 说明 |
|------|------|--------|------|
| 单元测试 | `test_state_machine.py`, `test_context.py`, `test_middleware.py` 等 | 85 | 核心模块行为验证 |
| Agent Eval | `test_agent_eval.py` | 14 | 用 fake LLM 验证 agent 行为闭环 |
| Integration Smoke | `test_integration.py` | 15 | 会话/日志/monitor 链路冒烟 |

```bash
# 运行全部测试
python -m pytest tests/ -v

# 或使用 unittest
python -m unittest discover -s tests -v
```

---

## License

MIT License

---

## 致谢

- [LangChain](https://python.langchain.com/) - LLM 调用层
- [Rich](https://github.com/Textualize/rich) - 终端 UI
- [Typer](https://github.com/tiangolo/tiangolo) - CLI 框架
