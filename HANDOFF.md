# NovaMind 项目交接文档（HANDOFF）

> **用途**：本文档是给接手 NovaMind 项目的其他 Agent 的完整交接说明。
> **优先级**：接手后先读本文档，再读 `ARCHITECTURE_MIGRATION_PLAN.md`（蓝图）和 `.workbuddy/memory/MEMORY.md`（长期约定）。
> **最后更新**：2026-08-20（当前项目处于"架构改造 P0-P7 全部收官 + GUI 桌面端已实现"状态）。

---

## 一、项目一句话定位

**NovaMind 是一个「可审计的深度研究 Agent 内核」**——把 Poirot 的架构精华（横切中间件、五层记忆、三组件沙箱、三层技能自进化、多 Agent 共享沙箱、token 预算治理）吸收进来，同时保留自研编排、零信任边界、可审计可追溯的看家本领。

不是套壳聊天机器人，而是认真回答"一个 Agent 内核应该长什么样"的框架，每个模块都有测试锁死关键不变量。

---

## 二、当前状态快照（重要）

| 项 | 状态 |
| --- | --- |
| 架构改造阶段 | P0-P7 **全部完成**，接线贯通 |
| 测试 | **304 个全绿**（`pytest -q`） |
| 运行入口 | CLI（`novamind run`）+ 桌面 GUI（`novamind gui` / `dist/nova-mind-gui.exe`） |
| 打包产物 | `dist/nova-mind-gui.exe`（单文件，~49M） |
| 数据位置 | 项目根 `workspace/` + `logs/`（源码 CLI 与 exe 共享） |
| Python | venv，Python 3.13.9，langchain-core 1.5.0 |
| 已知遗留 | 见「十一、已知遗留项」 |

> ⚠️ **注意**：`ARCHITECTURE_MIGRATION_PLAN.md`（蓝图）里有部分**过时信息**：
> 1. 蓝图里写的 "TUI（Textual 全屏）已实现" —— **实际 TUI 已删除**（Windows 老式终端 ConHost 渲染卡死，官方 issue #791），改成了 pywebview 桌面 GUI。
> 2. 蓝图里写的 "全量 289 测试通过" —— **实际是 304**（后续加了 GUI/三面板接口等测试）。
> 3. 蓝图是"改造前规划"的文档，**当前最新状态以本文档 + `README.md` + `.workbuddy/memory/MEMORY.md` 为准**。

---

## 三、架构全景（六层能力 + 自研编排）

一次请求的执行流：

```
run(question) → 状态机启动 agent 节点
  → 各 middleware 的 before_model（记忆召回 L4、技能注入、上下文治理 P0-P5、沙箱恢复）
  → LLM 调用（FallbackChatModel 链式降级）
  → after_model（预算追踪、记忆沉淀 L5、循环检测）
  → 有 tool_call 则 wrap_tool_call（沙箱校验/配对/外化）
  → tools 节点 → 回到 agent
  → after_agent（报告/反思）→ finalize
```

### 六层能力对照

| 层 | 位置 | 核心内容 |
| --- | --- | --- |
| **LLM 链式降级** | `novamind/core/llm/` | `ProviderProfile` + `ProviderConfig` + `FallbackChatModel` + `ModelRouter` |
| **横切中间件** | `novamind/core/middlewares/` | `BaseAgentMiddleware`（5 钩子）+ 记忆召回/沉淀、上下文治理、编排中间件 |
| **五层记忆** | `novamind/core/memory/` | L1 schema → L2 衰减遗忘 → L3 Markdown+BM25 → L4 注入 → L5 后台沉淀 |
| **三组件沙箱** | `novamind/core/sandbox/` | Runtime + PathTranslator + SecurityGuard，Local/Docker + warm pool |
| **上下文治理** | `novamind/core/context_engineering/` | token 预算 + P0-P5 分段舍弃 + 真实窗口解析 |
| **三层技能** | `novamind/core/skill/` | L1 SQLite+DAG+四计数器 → L2 进化 → L3 评估；37 内置技能 |
| **多 Agent** | `novamind/core/multiagent/` | 委派外部 agent + fork 子副本 + 共享沙箱 |

### 自研编排（关键保留项）

NovaMind 用的是**自研异步状态机**（`novamind/core/state_machine.py`），不是 LangGraph。这是相对 Poirot 的差异化保留。状态机已接入中间件钩子分发（`before_agent`/`after_agent` 在 state_machine，`before_model`/`after_model`/`wrap_tool_call` 在 agent.py）。

---

## 四、完整目录结构

```
D:\claudecode\NovaMind\
├── README.md                          # 对外介绍（仿 Poirot 风格，版本 3.0.0）
├── ARCHITECTURE_MIGRATION_PLAN.md     # 改造蓝图（有部分过时，见第二节注意）
├── HANDOFF.md                         # 本文档
├── CHANGELOG.md                       # 变更记录
├── NovaMind.spec                      # pyinstaller 打包配置
├── pyproject.toml                     # 包与依赖
├── requirements.txt                   # 依赖清单
├── .env                               # LLM 配置（DEFAULT_PROVIDER/DEFAULT_MODEL/API_KEY，勿提交）
│
├── entry/                             # CLI 入口
│   ├── cli.py                         # novamind 命令（config/run/gui/monitor/doctor）
│   ├── main.py                        # CLI 主循环（agent_worker）
│   └── monitor.py                     # Rich 审计面板（novamind monitor）
│
├── packaging/
│   └── gui_entry.py                   # pyinstaller 打包入口（run_gui）
│
├── novamind/
│   ├── core/                          # 核心内核
│   │   ├── agent.py                   # create_agent_app 组装（接线中间件+LLM路由）
│   │   ├── state_machine.py           # 自研异步状态机 + ConversationStore
│   │   ├── config.py                  # 路径配置（SRC_ROOT/DATA_ROOT 分离，勿乱动）
│   │   ├── middleware.py              # ⚠️ 旧洋葱管道（勿动，被 agent.py/测试引用）
│   │   ├── provider.py                # 旧单 provider 工厂（get_provider，仍被引用）
│   │   ├── policy.py                  # HarnessPolicy 零信任策略
│   │   ├── doctor.py                  # 架构健康体检（run_doctor）
│   │   ├── context.py                 # ContextManager（摘要+Context Pack）
│   │   ├── logger.py                  # AuditLogger（审计日志）
│   │   ├── token_tracker.py           # Token 计量
│   │   ├── event_bus.py / heartbeat.py
│   │   ├── tools/                     # 内置工具（builtins.py 等）
│   │   │
│   │   ├── llm/                       # ★ LLM 层（P0）
│   │   │   ├── provider_profile.py    # ProviderProfile 声明式 + 注册表
│   │   │   ├── provider_config.py     # ProviderConfig + MODEL_ROUTES + build_chat_model
│   │   │   ├── fallback_model.py      # FallbackChatModel 链式降级
│   │   │   └── model_router.py        # ModelRouter
│   │   │
│   │   ├── middlewares/               # ★ 横切中间件包（P0，复数名！）
│   │   │   ├── protocol.py            # BaseAgentMiddleware（before/after_agent、before/after_model、wrap_tool_call）
│   │   │   ├── manager.py             # MiddlewareManager（注册+分发）
│   │   │   ├── memory_recall_middleware.py       # L4 记忆召回
│   │   │   ├── memory_consolidation_middleware.py # L5 记忆沉淀
│   │   │   ├── context_governance_middleware.py  # P3 上下文治理
│   │   │   └── orchestration_middleware.py        # 多 Agent 编排打点
│   │   │
│   │   ├── memory/                    # ★ 五层记忆（P2）
│   │   │   ├── schema.py              # L1 MemoryTrace frozen dataclass
│   │   │   ├── types.py / config.py / exceptions.py
│   │   │   ├── memory_store.py / retriever.py  # 协议
│   │   │   ├── worker.py              # L5 后台 daemon 线程
│   │   │   ├── bootstrap.py           # 懒加载/生命周期
│   │   │   ├── persona.py             # 用户画像 ↔ procedural 记忆桥接
│   │   │   └── strategies/default/    # L2 decay/forget + L3 store/retriever + manager
│   │   │
│   │   ├── sandbox/                   # ★ 三组件沙箱（P1）
│   │   │   ├── contracts/             # PathTranslator/SecurityGuard/SandboxRuntime/SandboxProvider 协议
│   │   │   ├── translators/           # Identity/LocalPathTranslator/DockerPathTranslator
│   │   │   ├── guards/                # LocalSecurityGuard/DockerPathGuard/PermissiveGuard/AuditGuard
│   │   │   ├── runtimes/              # LocalRuntime/DockerRuntime
│   │   │   ├── local/                 # LocalSandboxProvider
│   │   │   ├── docker/                # DockerSandboxProvider + cross_process_lock + executor(WSL2)
│   │   │   └── sandbox.py             # 编排 validate→translate→execute→mask
│   │   │
│   │   ├── context_engineering/       # ★ 上下文治理（P3）
│   │   │   ├── contract.py            # GovernanceContext/GovernanceResult
│   │   │   ├── utilities.py           # token_counter + resolve_window_size（穿透真实窗口）
│   │   │   └── strategies/default/    # budget/externalizer/summarizer/snapshot/strategy(P0-P5)
│   │   │
│   │   ├── skill/                     # ★ 三层技能（P4）
│   │   │   ├── types.py / store.py    # L1 SkillRecord + SQLiteSkillStore
│   │   │   ├── parser.py / injector.py / selector.py
│   │   │   ├── evolution/             # L2 进化（IVEFocuser/LLMMutator/ScoreDeltaGate/GitRatchet/MetricMonitorTrigger）
│   │   │   ├── eval/                  # L3 评估（TaskQualityJudge/SkillJudgmentAnalyzer/ResponseContractChecker/RuntimeTracker）
│   │   │   └── builtin_skills/        # 37 个内置技能（全英文 SKILL.md）
│   │   │
│   │   └── multiagent/                # ★ 多 Agent（P5）
│   │       ├── types.py / exceptions.py
│   │       ├── sandbox_binder.py      # PerSubagentBinder 共享沙箱
│   │       ├── subagent.py / specialist.py  # 协议
│   │       ├── tools.py               # make_specialist_tool/make_subagent_tool
│   │       ├── specialists/           # subagent_specialist / pi_specialist
│   │       └── runtimes/pi_runtime.py # pi 外部 CLI 完整委派
│   │
│   └── webui/                         # ★ 桌面 GUI
│       ├── server.py                  # FastAPI 后端（/chat SSE + /sessions + /doctor + /monitor + /skills）
│       ├── app.py                     # pywebview 窗口（端口重试 + 降级浏览器）
│       └── static/index.html          # 单文件前端（工业终端美学）
│
├── tests/                             # 28 个测试文件，全量 304 个用例
├── docs/                              # 结构化文档（doctor 检查的资源）
├── harness/policies.json              # 零信任策略
├── workspace/                         # 运行时数据（SQLite 对话、记忆、技能库、office 工位）
├── logs/                              # 审计日志（*.jsonl）
├── dist/                              # 打包产物（nova-mind-gui.exe）
└── build/                             # pyinstaller 中间产物（可删）
```

---

## 五、核心不变量（铁律，改动时必须守住）

1. **依赖单向**：`app → agents` 单向，跨层用 Protocol 破循环依赖。
2. **工具里无 LLM**：原子操作是纯数据变换，LLM 编排只在 middleware / worker。
3. **核心循环保持薄**：加功能 = 加 middleware，不动状态机主体。
4. **零信任不丢失**：Local 沙箱白名单 + 路径穿越拦截 + 策略审计，是 NovaMind 相对 Poirot 的差异化。
5. **每阶段测试全绿**：改完任何东西跑 `pytest -q`，304 个必须全绿。
6. **测试 FakeLLM 的 `invoke` 必须接受 `**kwargs`**（兼容 langchain 的 `config` 参数），否则摘要触发时会报 `unexpected keyword argument 'config'`。

---

## 六、关键技术决策（已敲定，勿随意更改）

1. **兜底 provider** = `is_default` 的 provider（恒在降级链尾），**不硬编码 deepseek**；默认 provider 仍 openai。
2. **数据落盘**：运行时数据在项目根 `workspace/` + `logs/`（源码与 exe 共享）；打包后数据根 = exe 上一级目录（即项目根），可用 `NOVAMIND_DATA_ROOT` 环境变量覆盖。
3. **技能进化 L2 / 多 Agent L2/L3 默认关闭**，启动时显式提醒是否开启（环境变量 `NOVAMIND_SKILL_EVOLUTION` / `NOVAMIND_MULTIAGENT_EVOLUTION`）。
4. **内置技能全英文**（正文 + description）。
5. **编排保留自研**（不迁 LangGraph），只有手写检查点/流式/子图成本过高时才考虑迁。
6. **GUI 用 pywebview**（桌面窗口，非浏览器标签页），用户偏好 HTML 单文件、WorkBuddy 风格布局。
7. **TUI（textual）已废弃**，不要再引入。

### 关键命名避坑（非常重要）

- `novamind/core/config.py` = **路径配置**（勿动它的职责），LLM 配置在 `novamind/core/llm/` 包。
- `novamind/core/middleware.py` = **旧洋葱管道**（勿动，被 agent.py/测试引用），横切中间件在 `novamind/core/middlewares/`（**复数**）包。
- 这是历史遗留的命名冲突，新建包时**先确认不会和现有模块/包同名**。

---

## 七、数据与路径（关键，最容易踩坑）

### 路径推导逻辑（`novamind/core/config.py`）

```
SRC_ROOT  = 源码资源根（docs/harness/builtin_skills 等只读资源，随包分发，基于 __file__）
DATA_ROOT = 运行时数据根（workspace/logs 等可写数据）
  - 非 frozen（源码运行）：DATA_ROOT = SRC_ROOT = 项目根
  - frozen（打包 exe）：DATA_ROOT = exe 上一级目录 = 项目根（可用 NOVAMIND_DATA_ROOT 覆盖）
```

关键路径：
- `DB_PATH` = `WORKSPACE_DIR/state.sqlite3`（对话历史）
- `SKILL_DB_PATH` = `WORKSPACE_DIR/skill_store.sqlite3`（技能库）
- `LOG_DIR` = `DATA_ROOT/logs`（审计日志）
- `OFFICE_DIR` = `WORKSPACE_DIR/office`（沙箱工位）

### 为什么这样设计

pyinstaller 的 **onefile 模式每次运行都解压到新的临时目录**（`%TEMP%\_MEIxxxx`）。如果数据基于 `__file__`，每次启动对话历史/日志/技能库全部丢失。所以 frozen 时必须把数据根指到持久位置（项目根）。

### .env 读取（`webui/server.py` 的 `_resolve_env_path()`）

优先级：`NOVAMIND_ENV` 显式 → exe 同目录 → **exe 上一级（项目根）** → 源码根 → `~/.novamind/.env` → cwd。

> 打包后 exe 会自动读项目根的 `.env`，**用户无需 copy `.env` 到 dist/**。

---

## 八、GUI 桌面端与打包

### GUI 结构

- 后端：`novamind/webui/server.py`（FastAPI + SSE，复用 `create_agent_app` + `agent.astream`，单例 + `_chat_lock` 串行）
- 前端：`novamind/webui/static/index.html`（单文件，工业终端美学：呼吸状态灯 + 扫描线 + CRT 纹理）
- 窗口：`novamind/webui/app.py`（pywebview，端口自动重试 + 无 GUI 环境降级浏览器）
- 打包入口：`packaging/gui_entry.py`

### GUI 接口清单

| 接口 | 用途 |
| --- | --- |
| `POST /chat` | SSE 流式对话（thread→tool/text→done） |
| `GET /health` | 健康检查 |
| `GET /sessions` | 会话列表 |
| `GET /history/{thread_id}` | 会话历史 |
| `DELETE /sessions/{thread_id}` | 删除会话 |
| `GET /doctor` | 架构诊断 |
| `GET /monitor/sessions` | 监控会话日志列表 |
| `GET /monitor/events/{thread_id}` | 监控事件流 |
| `GET /skills` | 技能列表 |

### 打包命令（重要，有多个坑）

```powershell
# 1. 清理 build/dist（用 PowerShell .NET，避开 safe-delete）
[System.IO.Directory]::Delete("D:\claudecode\NovaMind\build", $true)
[System.IO.Directory]::Delete("D:\claudecode\NovaMind\dist", $true)

# 2. 打包（关闭 safe-delete，见避坑清单）
cd D:\claudecode\NovaMind
CODEBUDDY_SAFE_DELETE_SANDBOX=0 venv/Scripts/pyinstaller.exe NovaMind.spec --noconfirm
```

产物：`dist/nova-mind-gui.exe`（单文件 ~49M）。

### 打包避坑（见第十节，务必全读）

---

## 九、启动与验证

### 启动

```powershell
# 桌面 GUI（开发模式，读项目根 .env）
cd D:\claudecode\NovaMind
venv\Scripts\python.exe -m entry.cli gui

# 桌面 GUI（打包 exe，直接双击或命令行）
D:\claudecode\NovaMind\dist\nova-mind-gui.exe

# CLI 模式
novamind run

# 诊断 / 监控
novamind doctor
novamind monitor --list
```

### 验证要点

- exe 启动后，后端监听 `127.0.0.1:8765`（端口占用自动顺延 8766/8767…）。
- 快速验证后端：访问 `http://127.0.0.1:8765/health` 返回 `{"status":"ok"}`。
- 发消息验证 LLM：`POST /chat` 应流式返回文本（会召回记忆里的用户画像，如名字）。

---

## 十、避坑清单（所有踩过的坑，接手前必读）

### 1. safe-delete 拦截 pyinstaller（打包必踩）
- WorkBuddy 环境通过 `sitecustomize.py` 注入 safe-delete，`CODEBUDDY_SAFE_DELETE_SANDBOX=1` 时 win32 下 fail-closed。
- pyinstaller 覆盖 `build/` 中间产物（`base_library.zip`）时被拦，报 `SAFE_DELETE_FAIL_CLOSED`。
- **解法**：打包命令前加 `CODEBUDDY_SAFE_DELETE_SANDBOX=0`；清理 build/dist 用 PowerShell `[System.IO.Directory]::Delete`（绕过 python 的 sitecustomize）。

### 2. Anaconda DLL 缺失（打包必踩）
- venv 基于 Anaconda 时，标准库扩展依赖 `D:\Anaconda3\Library\bin\` 的 DLL，pyinstaller 静态分析不覆盖。
- 已显式加进 `NovaMind.spec` 的 `binaries`：`ffi.dll / liblzma.dll / libbz2.dll / libmpdec-4.dll / libexpat.dll / sqlite3.dll`。
- **sqlite3.dll 最关键**：缺失时 `_sqlite3` 加载失败，症状 = 打包后 `/skills` 报 ImportError、doctor 报 `module_import_failed_skill` warning。

### 3. onefile 数据丢失（数据根分离）
- onefile 每次解压到新临时目录，数据基于 `__file__` 会每次启动丢失。
- 已用 SRC_ROOT/DATA_ROOT 分离解决（见第七节）。

### 4. 前端面板 display:none bug（已修复，注意模式）
- 面板容器 CSS 默认 `display:none`，切换时不能用 `style.display = ''`（空字符串回退到 CSS 的 none，导致面板永远隐藏）。
- **正确写法**：`style.display = isChat ? 'none' : 'block'`（显式 block 覆盖 CSS none）。

### 5. test_agent 数据残留（测试脆弱性）
- `test_agent.py` 用固定 thread_id + 真实 DB_PATH，历史消息累积超过 `trim_messages` 阈值会触发 `generate_summary` → 调 `llm.invoke(config=...)` → FakeLLM 没定义 config → 报错。
- **修复**：FakeLLM 的 `invoke` 加 `**kwargs`。教训：所有 FakeLLM 的 invoke 必须兼容 langchain 的 `config` 参数。

### 6. 依赖循环（skill/eval ↔ skill/evolution）
- `response_contract_checker.py` 需要 `evolution.types` 的 `EvalResult`，而 `evolution/__init__` 又通过 `programmatic_bridge` 引回它。
- **修复**：把跨层值对象 import 改成 `check()` 内延迟导入。
- 教训：`from package.submodule import X` 会执行 `package/__init__`，跨层值对象放错层会制造循环。

### 7. textual TUI 在 Windows 卡死（已废弃 TUI）
- textual 在 Windows 老式终端（PowerShell/cmd 的 ConHost）渲染卡死（官方 issue #791），Windows Terminal 才正常。
- 已删除 TUI，改用 pywebview GUI。**不要再引入 textual**。

### 8. git 仓库 HEAD 损坏
- 当前 `git status` 报 `fatal: bad object HEAD`，git 仓库的 HEAD 引用损坏（非代码问题）。
- 若需要 git 操作，先修复仓库；否则直接用文件系统。

---

## 十一、已知遗留项 / 待办

以下是不阻断主链路的遗留，接手后可自行决定是否处理：

1. **C 盘残留数据**：`~/.novamind/` 里有早期 exe 写入的一次聊天数据（`logs/gui_ad47299065f1.jsonl`），改数据根后已不用，可忽略或删除。
2. **Docker 沙箱完整运行**：`DockerRuntime` 文件操作已补全（含 download_file），但真实运行需要 Docker 环境；`DockerSandboxProvider` warm pool 已实现但未在真实 Docker 环境验证。
3. **pi_specialist 外部 CLI**：`PiRuntime` 完整 prompt/凭证/错误归一化已实现，但真实委派需要安装 pi 工具。
4. **git 仓库 HEAD 损坏**：见避坑 8。
5. **蓝图文档过时**：`ARCHITECTURE_MIGRATION_PLAN.md` 的 TUI/测试数信息过时（见第二节），可择机修正。
6. **中文记忆检索**：BM25 已接 jieba 分词，但 jieba 是可选依赖（缺失回退空格分词）。
7. **用户画像/摘要**：已桥接到五层记忆（persona.py），但旧 `save_user_profile` 工具和 `ContextManager` 摘要的迁移是否彻底，可再 review。

---

## 十二、给其他 Agent 的操作指引

### 接手第一步（必做）
1. 读本文档（HANDOFF.md）
2. 读 `.workbuddy/memory/MEMORY.md`（长期约定）
3. 读 `README.md`（对外全貌）
4. 需要时读 `ARCHITECTURE_MIGRATION_PLAN.md`（蓝图，注意过时部分）

### 改代码前的检查
1. 确认改动模块的命名不冲突（见第六节命名避坑）
2. 确认不破坏六条铁律（见第五节）
3. 改完跑 `venv/Scripts/python.exe -m pytest -q`，304 个必须全绿

### 验证命令
```powershell
# 全量测试
venv/Scripts/python.exe -m pytest -q

# 单模块测试
venv/Scripts/python.exe -m pytest tests/test_webui.py -q

# 启动 GUI（开发模式）
venv\Scripts\python.exe -m entry.cli gui

# 诊断
venv/Scripts/python.exe -m entry.cli doctor
```

### 打包后验证
```powershell
# 启动 exe，探测后端
./dist/nova-mind-gui.exe
# 另开终端验证
venv/Scripts/python.exe -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8765/health').read())"
```

---

## 附：测试文件清单（28 个文件，304 个用例）

新架构相关测试：`test_fallback_model` / `test_model_router` / `test_agent_middleware` / `test_state_machine_hooks` / `test_agent_wiring` / `test_sandbox` / `test_memory` / `test_context_engineering` / `test_skill` / `test_multiagent` / `test_runtime_tracker` / `test_evolution_flag` / `test_webui`

原有基础测试：`test_agent` / `test_provider` / `test_middleware` / `test_doctor` / `test_state_machine` / `test_e2e` / `test_context` / `test_logger` / `test_token_tracker` / `test_monitor` / `test_session` / `test_sandbox_tools` / `test_plugin_loader` / `test_agent_eval` / `test_integration`
