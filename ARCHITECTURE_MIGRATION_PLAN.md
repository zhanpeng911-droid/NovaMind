# NovaMind 架构改造方案（对齐 Poirot 内核）

> 版本：v1.0　|　状态：待评审　|　适用范围：`D:\claudecode\NovaMind`（下文简称 **NovaMind**），参考项目 `D:\claudecode\poirot`（下文简称 **Poirot**）
>
> **本文档是改造的"方向性蓝图"**：所有参与 NovaMind 改造的 Agent / 开发者，必须以此文档为唯一对齐基准，避免各自为政、功能重复或方向漂移。开工前先读第 0、1、2 节，做具体模块时精读对应决策小节与"保留/替换/新增"清单。

---

## 0. 文档定位与使用约定

- **一句话目标**：把 NovaMind 从"可审计的 Agent 运行时"升级为"吸收 Poirot 内核精华、同时保留自身零信任/可观测/自研编排优势"的完整 Agent 内核。
- **改造不是复制 Poirot 代码**，而是把 Poirot 已验证的**架构决策与模块边界**迁移过来，落成 NovaMind 自己的实现（包名、CLI、术语、审计事件、策略文件格式保持 NovaMind 风格）。
- **每项决策的落地遵循同一套路**：`现状` → `要吸收什么` → `具体怎么落` → `边界（保留/替换/新增）` → `验收标准`。
- **任何新模块落地前，先查第 7 节对照清单**，确认没有和既有模块重复造轮子。
- **本文档中的"Poirot 参考文件"路径**，用于让执行者直接对照源码理解，不是要求照抄。

---

## 1. 总体目标与设计原则

### 1.1 目标

| 维度 | 现状（NovaMind） | 目标（改造后） |
| --- | --- | --- |
| 内核 | 自定义状态机，逻辑内聚在 agent_node | 状态机保持"薄"，横切逻辑全部抽到中间件 |
| LLM | 单 provider 工厂 + bind_tools | 声明式 ProviderProfile + 角色路由链 + 链式降级 + 真实窗口解析 |
| 中间件 | 洋葱模型（timing/logging/rate_limit） | 横切中间件（before/after_agent、before/after_model、wrap_tool_call） |
| 记忆 | SQLite 会话 + 摘要 + 用户画像 | 五层长期记忆 + 保留摘要/画像（可开关） |
| 沙箱 | office 工位（单本地目录） | 三组件（Runtime+PathTranslator+SecurityGuard）+ Local/Docker |
| 技能 | SKILL.md 动态发现（help→run） | 三层自进化技能（SQLite+DAG+四计数器→LLM 变异→四维评估） |
| 多 Agent | 无 | 委派外部 coding agent + fork 自身副本 + 共享沙箱 |
| 上下文 | 回合裁剪 + 摘要 | token 预算穿透真实窗口 + compaction/externalization 双策略 + P5 熔断 |
| UI | CLI + Rich monitor | GUI + CLI 双模式，保留 Rich monitor |
| 诊断 | doctor（文件存在性 + 策略覆盖） | 架构健康体检（多层自检 + 退化信号） |

### 1.2 五条设计原则（所有执行者必须遵守）

1. **核心循环保持薄**：加一个功能 = 加一个中间件，不往 agent loop 里塞逻辑。
2. **依赖严格单向**：`app → agents` 单向依赖，跨层用 `Protocol` 破循环依赖，禁止反向 import。
3. **工具里无 LLM**：原子操作（记忆 encode、skill 打点）是纯数据变换，LLM 编排只出现在 middleware / worker 层。
4. **零信任不丢失**：NovaMind 的 HarnessPolicy 拦截、审计事件、路径穿越拒绝，作为 SecurityGuard 与审计的底座保留，不因引入 Poirot 能力而弱化。
5. **每阶段测试落地**：任何模块合入前必须有对应单测；端到端链路每阶段末跑通。

---

## 2. 架构总览（改造后目标态）

```
                    ┌─────────────────────────────────────────────┐
                    │                 app 层（UI / CLI / bootstrap）│
                    │   GUI（Textual）+ CLI（prompt_toolkit+rich）  │
                    │   doctor（架构健康体检）  monitor（Rich 面板）│
                    └──────────────────────┬──────────────────────┘
                                           │ 单向依赖
                    ┌──────────────────────▼──────────────────────┐
                    │          agents 层（编排 + 能力装配）        │
                    │  NovaMindAgent（自研状态机，保持薄）          │
                    │  create_agent_app（工厂：装配中间件/工具/记忆）│
                    └───────┬──────────────┬──────────────┬───────┘
                            │              │              │
              ┌─────────────▼──┐  ┌────────▼──────┐  ┌────▼─────────────┐
              │  Middleware 栈 │  │  能力组件      │  │ 基础设施          │
              │  21 个横切钩子  │  │ 记忆(五层)     │  │ AuditLogger/审计  │
              │  before_agent  │  │ 技能(三层)     │  │ TokenTracker/budget│
              │  before_model  │  │ 沙箱(三组件)   │  │ RunJournal        │
              │  after_model   │  │ 上下文治理     │  │ Doctor            │
              │  wrap_tool_call│  │ 多Agent编排    │  │ SQLite/持久化     │
              │  after_agent   │  │ MCP/工具生态   │  │                   │
              └────────────────┘  └────────────────┘  └───────────────────┘
```

**执行流（一次请求）**：`run(question)` → 状态机启动 `agent` 节点 → 各 middleware 的 `before_model`（记忆召回 L4、技能注入、上下文治理 P0-P5、沙箱恢复）→ LLM 调用（FallbackChatModel 链式降级）→ `after_model`（预算追踪、记忆沉淀 L5、循环检测）→ 有 tool_call 则 `wrap_tool_call`（沙箱校验/配对/外化）→ `tools` 节点 → 回到 `agent` → `after_agent`（报告/反思）→ `finalize`。

---

## 3. 十一项决策逐条落地方案

### 决策 1 — 编排：保留自研状态机，但"瘦身 + 预留切换点"

- **现状**：`novamind/core/state_machine.py` 的 `NovaMindAgent`（节点+边+条件路由，asyncio），`agent_node` 里内聚了大量逻辑（裁剪、摘要、画像、Context Pack、审计、Token）。
- **决策**：保留自研状态机作为编排核心（**暂不迁移 LangGraph**）；但把 `agent_node` / `tool_executor` 里的横切逻辑全部抽到中间件，状态机只保留"节点调度 + 状态归约 + 条件路由 + 持久化"四件事。
- **风险与触发切换条件**：用户已指出"自研可能臃肿"。**切换开关设为**：当状态机为支持中间件钩子、流式、检查点（checkpointer）、多图子任务而需要手写大量 LangGraph 已有能力时，即迁移到 LangGraph。届时只需替换 `NovaMindAgent` 内部实现，中间件协议（决策 3 定义）与能力组件不受影响。
- **落地**：
  1. 定义 `AgentMiddleware` 协议（见决策 3），让 `NovaMindAgent.run/astream` 在关键生命周期点调用中间件。
  2. `agent_node` 拆为：`(before_model hooks) → LLM 调用 → (after_model hooks)`，裁剪/摘要/画像/Context Pack 逻辑移交 `ContextGovernanceMiddleware`、`MemoryMiddleware`、`SummarizationMiddleware`。
  3. `tool_executor` 拆为：`(wrap_tool_call hooks) → 工具查找 → 沙箱校验 → 执行 → 归约`，策略拦截移交 SecurityGuard + 审计中间件。
- **验收**：`agent_node`/`tool_executor` 函数体 < 80 行且不含业务分支；中间件可增删而不改状态机；现有 147 测试中与循环/会话/持久化相关的用例保持通过。

### 决策 2 — LLM：沿用 Poirot 的 ProviderProfile + 角色路由 + 链式降级 + 真实窗口

- **现状**：`novamind/core/provider.py` 的 `get_provider(provider_name, model_name)` 工厂，单一 provider，无降级。
- **要吸收**（Poirot 参考文件）：
  - `config/provider_profile.py` — `ProviderProfile`（frozen dataclass 声明式注册：name/kind/env_key/env_base_url/env_model/default_base_url/default_model/default_window/priority/is_default/no_key_required）。
  - `config/provider_config.py` — 运行时读 env 解析 `ProviderConfig`；`MODEL_ROUTES` 角色路由链（researcher/reporter/reflection），**deepseek 恒在链尾兜底**；`build_chat_model` 按 kind 分发。
  - `config/fallback_model.py` — `FallbackChatModel`：`_should_fallback` 只降级**瞬时错误**（Timeout/Connection/限流/5xx），**不降级** 400/401/404；`_active` 记忆活跃 provider 避免每轮重试主力；`bind_tools` 对链内每个 model 绑定。
  - `config/model_router.py` — `ModelRouter.build_model(role)` 按角色构造 FallbackChatModel。
- **落地**：
  1. 新建 `novamind/core/llm/` 包（`provider_profile.py`、`provider_config.py`、`fallback_model.py`、`model_router.py`），替换现有 `provider.py` 的工厂逻辑（旧 `get_provider` 保留为 `build_single` 兼容入口）。注：`novamind/core/config.py` 已被路径配置占用，LLM 配置层独立放 `llm/` 包以避冲突。
  2. 保留 NovaMind 现有 provider 覆盖（OpenAI/Anthropic/DashScope/腾讯混元/智谱/Ollama），按 Poirot 的 `ProviderProfile` 结构重写注册表，**新增** deepseek/qwen/gemini/moonshot/openrouter 等可选 profile。
  3. `.env` 改为 profile 声明式：`{NAME}_API_KEY` / `{NAME}_BASE_URL` / `{NAME}_MODEL` / `{NAME}_ENABLED`；默认 provider 由 `is_default` + `priority` 决定。
  4. 引入**角色路由**：researcher（主推理）/ reporter（报告合成）/ reflection（反思）各配一条降级链。
- **验收**：断开主 provider（模拟限流/超时）时自动切到下一 provider，客户端错误（401）不降级；`.env` 只配 key 即能跑通；新增 provider 只改注册表一处。

### 决策 3 — 中间件：沿用横切中间件，合并时去重

- **现状**：`novamind/core/middleware.py` 的洋葱模型 `(ctx, next)`，仅 timing/logging/rate_limit。
- **要吸收**（Poirot 参考文件）：`leader/factory.py` 的 `_build_middlewares` 挂载顺序，以及 21 个中间件各自职责。
- **落地**：
  1. 定义 NovaMind 版 `AgentMiddleware` 协议，钩子含：`before_agent` / `after_agent` / `before_model` / `after_model` / `wrap_tool_call`（同步 + async 双入口）。
  2. **挂载顺序固定为**（顺序即语义，禁止随意调换）：
     1. ContextGovernance（预算 init + 治理策略）
     2. SystemContext（系统提示词组装）
     3. SkillInjection（L1 技能注入）
     4. SkillMetrics（四计数器打点）
     5. SkillActivation（主动建议技能）
     6. Title（会话标题）
     7. RunJournal（运行日志）
     8. MCPAudit（MCP 审计，条件挂）
     9. Sandbox（沙箱生命周期 + 子 agent 恢复 ContextVar）
     10. MemoryRecall（L4 召回）
     11. MemoryConsolidation（L5 沉淀，条件挂）
     12. HelpRequest（求助）
     13. DanglingToolCall（悬空工具调用配对）
     14. ToolCall（工具路由）
     15. Orchestration（多 Agent，条件挂）
     16. Evidence（证据链）
     17. StallDetection（停滞检测）
     18. Todo（任务清单）
     19. Reflection（反思）
     20. Report（报告合成）
  3. **去重规则**：NovaMind 现有 `timing/logging/rate_limit` 中间件的职责并入 RunJournal（计时/审计）与 StallDetection（限流/停滞），**不再保留独立洋葱管道**；`MiddlewarePipeline` 类保留为内部工具，但不再作为主执行链。
- **验收**：中间件可插拔、顺序可配置；去掉任一中间件不影响核心循环；21 个钩子均有对应单测。

### 决策 4 — 记忆：五层长期记忆为主，摘要压缩 + 用户画像保留（可开关）

- **现状**：`novamind/core/context.py` 的回合裁剪 + LLM 摘要（词法+LLM 二次评估）+ `save_user_profile` 用户画像；SQLite 会话持久化。
- **要吸收**（Poirot 五层，参考文件见下）：
  - **L1 Schema**：`memory/schema.py` — `MemoryTrace`（frozen dataclass）+ `MemoryType`（episodic/semantic/procedural）+ `Association` + `OperationLog`（上限 20 条）；`with_strength` / `with_operation` 创建新实例替换，**工具里无 LLM**。
  - **L2 策略**：`memory/strategies/default/decay.py`（艾宾浩斯懒计算）+ `forget.py`（TTL+strength 阈值复合遗忘）+ 6 个硬编码决策；`memory/config.py` 的 `MemoryConfig`（frozen，仅 4 个 STARTUP_ONLY 字段）+ `set_memory_config()` 全局单例 runtime 切。
  - **L3 存储+检索**：`memory/strategies/default/store.py`（MarkdownFileStore 单文件 truth source）+ `retriever.py`（纯 BM25 + 检索强化写回 + forgotten 过滤 + 增量索引）。
  - **L4 注入**：`MemoryMiddleware.abefore_model` 注入 per-call `HumanMessage`（`hide_from_ui=True`，不碰 system prompt cache）；`set_turn_id` ContextVar。
  - **L5 沉淀**：`MemoryConsolidationMiddleware.aafter_model` 非阻塞 submit + `MemoryWorker`（daemon 线程 + Queue，LLM 抽取 episodic → encode → 总量够 10 条取最旧 10 条合并为 1 条 semantic，旧 trace 标 forgotten）。
- **落地**：
  1. 新建 `novamind/core/memory/` 完整移植五层结构（schema/strategies/store/retriever/config/worker/bootstrap）。
  2. **保留并融合 NovaMind 特色**：
     - **用户画像**：作为 `MemoryType.PROCEDURAL`（或独立 `persona` 类型）的一条高重要性 trace 保留，仍由 `save_user_profile` 工具写入；`build_system_prompt` 中的"画像是不信静态资料"安全边界保留。
     - **摘要压缩**：作为上下文治理（决策 8）的 P4 compaction 策略保留，`ContextManager.generate_summary` + 词法/LLM 二次评估逻辑迁入 `SummarizationMiddleware`。
  3. **开关控制**：`POIROT_MEMORY_USE` 等价开关 `NOVAMIND_MEMORY_USE`；默认关闭 L5 后台沉淀（`enable_extract=False`），业务需要时再开。
- **验收**：换会话（thread_id）后记忆可召回；`strength` 在 retrieve 时才计算（无后台扫表）；记忆注入不破坏 prompt cache 前缀；摘要/画像功能通过开关可独立启停。

### 决策 5 — 沙箱：三组件 + Local/Docker + warm pool + WSL2 + 跨进程锁

- **现状**：`novamind/core/tools/sandbox_tools.py` 的 office 工位（`list/read/write_office_file` + `execute_office_shell`），单本地目录 + 路径穿越拒绝 + shell 白名单。
- **要吸收**（Poirot 三组件，参考文件）：
  - `sandbox/sandbox.py` — `Sandbox` 编排类：**validate → translate → execute → mask**，切沙箱只换组件。
  - `sandbox/contracts/` — `PathTranslator`（translate_path / reverse_translate / translate_command / mask_output）、`SecurityGuard`（validate_path / validate_command）、`SandboxRuntime`（exec/read/write/list/glob/grep/download/update/close）。
  - `sandbox/translators/` — `IdentityTranslator` / `LocalPathTranslator` / `DockerPathTranslator`。
  - `sandbox/guards/` — `LocalSecurityGuard` / `DockerPathGuard`（强制 write_file 与 bash 重定向目标在 `/mnt/poirot/user_data/`）/ `PermissiveGuard` / `AuditGuard`。
  - `sandbox/docker/` — `DockerSandboxProvider` + warm pool + idle auto-destroy + `cross_process_lock.py` + `WslDockerExecutor`（`D:\foo` → `/mnt/d/foo`）。
- **落地**：
  1. 新建 `novamind/core/sandbox/`，按 contracts/translators/guards/runtimes/local/docker 分层移植。
  2. **保留 NovaMind 零信任底座**：`LocalSecurityGuard` 内部复用现有路径穿越拒绝 + shell 白名单 + HarnessPolicy 确认关键词；`AuditGuard` 复用 AuditLogger。
  3. **office 工位兼容**：Local provider 默认把沙箱根映射到 `workspace/office/`，旧 `*_office_*` 工具作为沙箱工具的别名保留，确保现有策略/测试不破。
  4. Docker 模式用 `docker-compose.yml` 挂载 `/mnt/novamind/user_data/`；WSL2 executor 处理 Windows 路径翻译。
- **验收**：Local/Docker 两 provider 可通过 env 切换；Docker 模式下 LLM 写的文件真实落盘到宿主机挂载区（`present_files` 能取到）；路径穿越/重定向逃逸被拦截；多实例并发不冲突。

### 决策 6 — 技能：三层自进化 + 36 内置

- **现状**：`novamind/core/plugin_loader.py` 扫描 `SKILL.md`/`README.md`，help→run 两阶段，无版本、无指标、无进化。
- **要吸收**（Poirot 三层，参考文件）：
  - **L1 基础**：`skill/store.py`（`SQLiteSkillStore`：WAL + version DAG + `is_active` 单指针 + 四计数器 selections/applied/completions/fallbacks）+ `skill/selector.py`（quality filter + LLM hybrid 选择）+ `skill/injector.py`（注入 middleware）。
  - **L2 进化**：`skill/evolution/`（`MetricMonitor` 阈值触发 → `IVEFocuser` 五问诊断 → `LLMMutator` 变异 → `ScoreDeltaGate` 门槛 → `GitRatchet` 棘轮回滚）。
  - **L3 评估**：`skill/eval/`（`SkillJudgmentAnalyzer` 执行判定 + `TaskQualityJudge` 四维加权：准确度 0.50/完整性 0.35/效率 0.05/深度 0.10 + `ResponseContractChecker` 契约检查 + `RuntimeTracker` 退化信号回灌 L2）。
  - **36（实际 37）内置技能**：`skill/builtin_skills/`（core/research/software-development/creative/productivity 五类，core 自动加载，其余 `/skill search`）。
- **落地**：
  1. 新建 `novamind/core/skill/`（store/selector/injector/parser/evolution/eval/hub）。
  2. **去重**：NovaMind 现有 `plugin_loader.load_dynamic_skills` 的"扫描 SKILL.md"职责并入 `SkillStore.discover`；旧动态技能入口保留为兼容别名。
  3. 内置技能从 Poirot `builtin_skills` 迁移，中文化描述；`/skill` 斜杠命令接入 CLI。
- **验收**：技能有版本 DAG、可回滚；四计数器打点零 LLM；`effective_rate` 跌破阈值触发变异且变异后分数更高才通过；退化可棘轮回滚。

### 决策 7 — 多 Agent：委派外部 coding agent + fork 自身副本 + 共享沙箱

- **现状**：无。
- **要吸收**（Poirot 参考文件）：
  - `multiagent/specialist.py` + `specialists/`（`pi_specialist.py` / `codex_specialist.py` / `claude_code_specialist.py`）— 通过 MCP `SpecialistMcpServer` 委派外部 CLI（pi/codex/claude），`--sandbox-url` 透传共享 Docker 容器。
  - `multiagent/subagent.py` + `subagent_specialist.py` — fork Poirot 自身副本做叶子任务，隔离 context、共享 thread sandbox。
  - `multiagent/sandbox_binder.py` — `SandboxBinder` 契约，复用父 `sandbox_id`。
  - `SandboxMiddleware.abefore_model` 从 `state["sandbox"]` 恢复 `ContextVar`，子 agent 跳过 acquire 直接复用父沙箱。
- **落地**：
  1. 新建 `novamind/core/multiagent/`（specialist/subagent/sandbox_binder/orchestration_middleware/eval/evolution）。
  2. **叶子角色防递归**：子 agent 的 `tool_groups` 不含 `multiagent`（看不到 `delegate_to_*` 工具），禁止无限 spawn（Poirot INV#4）。
  3. 工具 `delegate_to_specialist(goal, success_criteria)` / `delegate_to_subagent(goal)` 注入 lead agent 工具集。
  4. 共享沙箱：specialist 通过 MCP 命令透传 `--sandbox-url` 连 lead 的 Docker 容器；subagent 复用父 `sandbox_id`。
- **验收**：lead 能委派子任务且产物落在同一沙箱挂载区；子 agent 无法再 spawn 孙 agent；外部 CLI 未安装时优雅报错并回退。

### 决策 8 — 上下文治理：token 预算穿透真实窗口 + compaction/externalization 双策略 + P5 熔断

- **现状**：`ContextManager.trim_messages` 按**回合数**裁剪（trigger_turns=40），非 token 敏感。
- **要吸收**（Poirot 参考文件）：
  - `context_engineering/strategies/default/strategy.py` — `DefaultStrategy`，P0-P5 分段优先级舍弃：
    - P1 externalize 0.40（历史消息外化到文件，只留预览）
    - P2 thinking 0.50
    - P3 observations 0.60（top-N 截断）
    - P4 summarize 0.80（compaction 摘要）
    - P5 stop_toolcall 0.90（剥 tool_calls + 收尾提示 + jump model）
    - hard_stop 0.99（强制收尾）
  - `context_engineering/strategies/default/budget.py` — `BudgetTrackerExecutor`（累计 token 算 fraction 标 pending）。
  - `context_engineering/strategies/default/externalizer.py` + `summarizer.py` + `snapshot.py`。
  - `context_engineering/utilities.py` — `token_counter`（tiktoken 懒加载 + CJK 字符 fallback）+ `resolve_window_size`（**穿透 FallbackChatModel** 到内层真实模型，查 model_name 前缀映射表拿真实窗口，非硬编码）。
- **落地**：
  1. 新建 `novamind/core/context_engineering/`（strategies/default/{strategy,budget,externalizer,summarizer,snapshot} + utilities）。
  2. **保留 NovaMind 特色**：`ContextManager.generate_summary`（词法 + LLM 二次评估）作为 P4 的摘要实现；`Context Pack`（docs/ 结构化事实源）作为独立的 `before_model` 注入保留，与 token 治理并行不冲突。
  3. 阈值默认值与 Poirot 对齐，但通过 `.env` / 配置可调。
  4. **中文 token 估算**：`token_counter` 的 CJK fallback 必须保留（中文场景下 tiktoken 常不可用）。
- **验收**：长会话不溢出真实窗口；fraction 分母随 provider 切换自动取真实窗口；P5 触发时不再调用工具、基于已有信息收尾；externalize 的历史可从文件恢复（`/expand`）。

### 决策 9 — UI：GUI + CLI 双模式，保留 Rich monitor

- **现状**：CLI（Typer）+ Rich 实时监控面板（`entry/monitor.py`）。
- **落地**：
  1. **保留** Rich monitor（`novamind monitor --thread-id <id>`），作为"审计事件实时面板"继续存在。
  2. **新增 GUI**：引入 Textual（Poirot 已用 `textual>=0.40`），实现全屏 TUI：左滚动日志 + 底部输入框 + 状态栏实时 token 用量 + 右侧会话信息面板（宽屏）。
  3. **CLI 保留**：`prompt_toolkit` + `rich` 的滚动模式，斜杠命令补全 + 底部工具栏。
  4. 命令入口统一：`novamind`（默认 TUI）、`novamind cli`（滚动模式）、`novamind monitor`（审计面板）、`novamind doctor`（体检）。
- **验收**：三种入口共享同一套底层 `create_agent_app`；TUI 状态栏显示真实 token 用量与当前活跃 provider（用 `resolve_model_name` 取真实模型名，非 provider 链名）。

### 决策 10 — 测试：每阶段落地，不一次性堆

- **原则**：不做"先全改再补测试"，每个阶段（见第 6 节）结束时必须有对应测试通过才进入下一阶段。
- **测试分层**：
  1. 单元测试：每个新模块（provider/fallback/middleware/memory/sandbox/skill/multiagent/context）独立单测，覆盖关键不变量（如"工具里无 LLM""lazy decay 无后台任务""降级只切瞬时错误"）。
  2. 集成测试：`create_agent_app` 全链路（Poirot 用 FakeLLM + patch 走真实 graph）。
  3. 端到端：会话持久化跨重启、策略拦截、迭代上限、沙箱拒绝、多 agent 共享沙箱。
- **目标**：改造完成时测试量对齐 Poirot 的覆盖强度（2400+），但分阶段逐步累积；每阶段末跑 `pytest -q` 全绿。
- **保留**：现有 147 测试中仍有效者全部保留，改造涉及的模块同步更新断言。

### 决策 11 — 诊断：doctor 升级为"架构健康体检"

- **现状**：`novamind/core/doctor.py` 检查 docs 存在性、policy 合法性、工具覆盖、近期违规。
- **升级方案**（由执行者按此方向实现，具体检查项可增补）：
  1. **保留现有检查**：docs 存在性、policy JSON/白名单、工具覆盖、近期违规扫描。
  2. **新增分层自检**：
     - Provider：`.env` 关键 provider 是否可用、默认 provider 是否配置 key、路由链是否含兜底 deepseek。
     - Memory：storage 路径可写、Markdown truth source 可解析、BM25 索引与 store 是否一致。
     - Skill：SQLite schema 版本、版本 DAG 完整性、`is_active` 单指针唯一性、四计数器非负。
     - Sandbox：Local/Docker provider 组件是否齐备、挂载区是否存在、跨进程锁是否可用。
     - Middleware：装配顺序是否符合第 3 节固定顺序、有无重复挂载。
     - Context：窗口映射表是否覆盖当前 provider、token 估算器是否可用。
     - MultiAgent：外部 CLI 是否安装、沙箱绑定契约是否就绪。
  3. **输出结构化**：保持 `doctor --json` 返回 `{ok, counts, findings:[{level,code,message,suggestion}]}`，供 CI / 脚本消费；退出码非零表示存在 error。
  4. **退化信号接入**：`RuntimeTracker`（技能/多 Agent 的 applied-rate 趋势）产生的退化信号，作为 `warning` 级 finding 出现在 doctor 报告中。
- **验收**：启动前跑 `novamind doctor` 能发现配置漂移与模块不健康状态；新增模块后 doctor 能自动感知其健康度。

---

## 4. 模块边界与依赖规则

1. **`app → agents` 单向**：UI/CLI/bootstrap 只依赖 `agents` 层，`agents` 层不得反向 import `app`。
2. **跨层用 Protocol 破环**：memory worker 不反向依赖 app（LLM 构造注入）；skill L1 不 runtime import L2/L3（duck-type）；sandbox 组件间只依赖 contracts 协议。
3. **能力组件可独立替换**：记忆（MemoryProvider）、沙箱（SandboxProvider）、技能（SkillStore）都定义 Protocol，切换实现不改编排。
4. **术语统一**：改造后统一用 NovaMind 术语（`novamind`、`HarnessPolicy`、`doctor`、`office`），但关键架构概念（五层记忆、三组件沙箱、P0-P5、三层技能）沿用 Poirot 命名，便于对照源码。

---

## 5. 目标目录结构（改造后）

```text
NovaMind/
├── entry/                          # CLI / TUI 入口、monitor、doctor
├── novamind/
│   └── core/
│       ├── state_machine.py        # 自研状态机（保持薄）
│       ├── agent.py                # create_agent_app 工厂
│       ├── middlewares/            # 横切中间件（协议 + 20 个实现；复数名避开旧 middleware.py）
│       ├── llm/                    # provider_profile / provider_config / fallback_model / model_router
│       ├── memory/                 # 五层记忆（schema/strategies/store/retriever/config/worker/bootstrap）
│       ├── sandbox/                # 三组件（contracts/translators/guards/runtimes/local/docker）
│       ├── skill/                  # 三层技能（store/selector/injector/parser/evolution/eval/hub/builtin_skills）
│       ├── context_engineering/    # token 预算 + P0-P5 治理
│       ├── multiagent/             # 多 Agent（specialist/subagent/sandbox_binder/orchestration）
│       ├── policy.py               # HarnessPolicy（保留，作为 SecurityGuard 底座）
│       ├── doctor.py               # 架构健康体检（升级）
│       ├── logger.py               # AuditLogger（保留）
│       ├── token_tracker.py        # 保留，接入 budget
│       └── tools/                  # 内置工具 + 沙箱工具（保留）
├── docs/                           # 运行时事实源（保留，同步更新）
├── harness/policies.json           # 策略（保留，同步更新）
├── workspace/                      # 运行时数据（会话库/记忆/沙箱/技能库）
├── logs/                           # 审计日志（保留）
└── tests/                          # 分阶段累积的测试
```

---

## 6. 分阶段实施路线图

> 每阶段结束：相关单测通过 + `pytest -q` 全绿 + `novamind doctor` 通过，才进入下一阶段。**按依赖顺序推进，禁止跳跃。**

| 阶段 | 内容 | 关键产出 | 依赖 |
| --- | --- | --- | --- |
| **P0 地基** | 中间件协议定义 + 状态机瘦身 + ProviderProfile/FallbackChatModel/ModelRouter | 中间件钩子可用；LLM 链式降级 | 无 |
| **P1 沙箱** | 三组件协议 + Local/Docker provider + 路径翻译 + 守卫 + 跨进程锁 + WSL2 | 沙箱可切换、可落盘 | P0 |
| **P2 记忆** | 五层记忆完整移植 + 用户画像/摘要融合 | 跨会话召回、懒衰减 | P0 |
| **P3 上下文治理** | token 预算 + P0-P5 + 真实窗口解析 | 长会话不溢出、P5 熔断 | P0/P2 |
| **P4 技能** | 三层技能（SQLite+DAG+四计数器 → 变异 → 评估）+ 内置技能迁移 | 技能自进化闭环 | P0 |
| **P5 多 Agent** | 委派外部 agent + fork 子副本 + 共享沙箱 | 共享沙箱产物一致 | P1/P4 |
| **P6 UI + 诊断** | TUI/CLI 双模式 + doctor 升级 + RuntimeTracker 接入 | 双入口 + 健康体检 | 全部 |
| **P7 收尾** | 测试补齐、文档同步（docs/ + README）、性能验证 | 全量测试 + 文档 | 全部 |

**P0 是其他所有阶段的硬前置**：中间件协议与 LLM 路由定了，后续模块才有一个稳定的挂载点。

### 当前进度

- ✅ **P0 地基**（已完成）：`novamind/core/llm/`（ProviderProfile/ProviderConfig/FallbackChatModel/ModelRouter）+ `novamind/core/middlewares/`（BaseAgentMiddleware/MiddlewareManager）+ 状态机 before/after_agent 钩子接入。测试 27 个。
- ✅ **P1 沙箱**（已完成）：`novamind/core/sandbox/`（三组件 contracts + Sandbox 编排 + translators/guards/runtimes + LocalSandboxProvider 完整 + DockerSandboxProvider 骨架含 warm pool/idle destroy/跨进程锁/WSL2 executor）。测试 26 个。office 工位默认映射 `/mnt/novamind/user_data` → `OFFICE_DIR`，旧 office 工具保留（后续迁移）。
- ✅ **P2 记忆**（已完成）：`novamind/core/memory/`（五层：L1 schema / L2 衰减遗忘 / L3 MarkdownFileStore+HybridRetriever BM25 / L4 MemoryRecallMiddleware / L5 MemoryWorker 后台沉淀）+ `middlewares/memory_recall_middleware.py` + `memory_consolidation_middleware.py`。测试 18 个。用户画像/摘要融合留待 P3 上下文治理时衔接。
- ✅ **P3 上下文治理**（已完成）：`novamind/core/context_engineering/`（contract + utilities 真实窗口解析穿透 FallbackChatModel + budget/externalizer/summarizer/snapshot + DefaultStrategy P0-P5）+ `middlewares/context_governance_middleware.py`。测试 13 个。用户画像/摘要压缩已融合（画像 → procedural 记忆 `memory/persona.py`，摘要 prompt 只记进度不记偏好）。
- ✅ **P4 技能**（已完成）：`novamind/core/skill/`（L1 SQLiteSkillStore WAL+DAG+四计数器 + parser/injector/selector + L2 进化 IVEFocuser/LLMMutator/ScoreDeltaGate/GitRatchet/MetricMonitorTrigger + L3 评估 TaskQualityJudge/SkillJudgmentAnalyzer/ResponseContractChecker）+ 37 内置技能迁移到 `builtin_skills/`（全英文，discover 验证通过）。测试 12 个。
- ✅ **P5 多 Agent**（已完成）：`novamind/core/multiagent/`（types + sandbox_binder 共享沙箱 + subagent/specialist 协议 + tools 委派工具 + specialists/subagent_specialist + pi_specialist 外部 CLI 骨架）+ `middlewares/orchestration_middleware.py`。测试 6 个。共享沙箱：PerSubagentBinder 复用父 sandbox_id，PiSpecialist --sandbox-url 透传。
- ✅ **P6 UI + 诊断**（已完成）：doctor 已升级为架构健康体检（`_check_architecture_modules` 分层自检 7 个模块 + 内置技能）。TUI（Textual 全屏）已实现 `entry/tui.py` + `novamind tui` 命令（CLI/TUI 双模式）。RuntimeTracker 已实现（`skill/eval/runtime_tracker.py`，applied-rate 趋势判定 + 退化检测，信号供 GitRatchet 回滚）。
- ✅ **P7 收尾**（已完成）：
  - ✅ agent.py 接线：`create_agent_app` 接入 `model_router`（FallbackChatModel 链式降级）+ `middlewares`（before_model/after_model/wrap_tool_call 钩子分发）+ `NovaMindAgent` 传 middleware_manager。测试 3 个（test_agent_wiring.py）。
  - ✅ 画像迁移：`save_user_profile` 工具桥接 `memory/persona.py`（记忆 provider 启用时写 procedural 记忆，否则静默降级）。
  - ✅ TUI（Textual 全屏）：`entry/tui.py` + `novamind tui` 命令，全屏对话界面（用户输入/AI 回复/工具调用流式展示），Esc 中断、Ctrl+Q 退出。
  - ✅ Docker 沙箱补全：`DockerRuntime.download_file` 用 `docker cp` + 临时文件实现，异常归一为 `SandboxCommandError`；`DockerSandboxProvider` warm pool 真正预热（release 移入 warm pool 复用、容量淘汰最旧、idle 后台销毁线程）。
  - ✅ jieba 中文分词：`HybridRetriever._default_tokenize` 检测中文切 jieba 分词（缺失回退空格），解决 BM25 中文检索。
  - ✅ bootstrap 进化提醒：`skill/evolution/flag.py` 提供 `evolution_notice()`，`entry/main.py` 启动时提示 L2/L3 默认关闭及开启方式（`NOVAMIND_SKILL_EVOLUTION` / `NOVAMIND_MULTIAGENT_EVOLUTION`）。
  - ✅ pi_specialist 完整委派：`multiagent/runtimes/pi_runtime.py`（完整 prompt goal+context+success+三段输出格式 + 凭证 env 透传国内 provider 优先 + --sandbox-url 透传 + 错误归一化未安装/超时/非零退出码），`PiSpecialist` 组合 `PiRuntime`。

> 全部阶段 P0-P7 收官：六层能力（LLM 降级/横切中间件/五层记忆/三组件沙箱/上下文治理/三层技能/多 Agent）+ 接线贯通 + CLI/TUI 双入口 + RuntimeTracker 退化回灌 + Docker warm pool + pi 完整委派，全量 289 测试通过。改造目标达成，无阻断性遗留。

---

## 7. 保留 / 替换 / 新增 对照清单

| NovaMind 现有 | 处置 | 去向 / 说明 |
| --- | --- | --- |
| `state_machine.py`（NovaMindAgent） | **保留+瘦身** | 保留自研编排，横切逻辑抽走 |
| `provider.py`（get_provider） | **替换** | 换为 ProviderProfile + ModelRouter + FallbackChatModel；旧入口保留兼容 |
| `middleware.py`（洋葱管道） | **替换** | 换为横切中间件协议；timing/logging/rate_limit 职责并入 RunJournal/StallDetection |
| `context.py`（ContextManager） | **拆分** | 摘要/画像迁入记忆与上下文治理；Context Pack 保留为独立注入 |
| `plugin_loader.py`（动态技能） | **替换** | 并入 SkillStore.discover |
| `tools/sandbox_tools.py`（office 工位） | **替换** | 换为三组件沙箱；office 工位作为 Local provider 默认根，旧工具保留别名 |
| `policy.py`（HarnessPolicy） | **保留** | 作为 SecurityGuard 的零信任底座 |
| `logger.py`（AuditLogger） | **保留** | 审计事件体系不变，中间件统一写审计 |
| `token_tracker.py` | **保留+升级** | 接入上下文治理 budget |
| `doctor.py` | **保留+升级** | 升级为架构健康体检 |
| `mcp_adapter.py` | **保留+扩展** | 扩展 stdio/sse/http 三传输 + 等价回退链 |
| `entry/monitor.py`（Rich 面板） | **保留** | 继续作为审计实时面板 |
| `entry/cli.py` | **保留+扩展** | 新增 TUI 入口与 `/skill`、`/expand` 命令 |

---

## 8. 已敲定决策（2026-08-20）

1. **兜底 provider = 用户默认 provider**（不再硬编码 deepseek）：`route_chain_for` 保证 `is_default` 的 provider 恒在降级链尾；deepseek 保留为可选 profile，用户配了 key 即成为额外兜底候选。默认 provider 仍为 openai（保持 NovaMind 现状），用户可在 `.env` 改默认。
2. **数据落盘沿用 Poirot 风格**：统一使用项目根目录下的隐藏目录 `.novamind/`（对应 Poirot 的 `.poirot/`），其下分 `memory/`、`sandbox/`、`skill_store.sqlite3`、`logs/` 等；不再散落 `workspace/`。旧 `workspace/` 数据提供一次性迁移说明。
3. **技能进化（L2）与多 Agent 演化（L2/L3）默认关闭**，但启动（bootstrap）时通过显式提示提醒用户是否开启，避免静默消耗 LLM 成本。
4. **内置技能全英文**：正文与 description 均英文（agent 理解更稳定），不中文化。

## 9. 剩余待决策项（Open Questions）

> 仅剩一项，其余已在第 8 节敲定。执行者遇到时**先问再动**。

1. **编排是否最终迁移 LangGraph**（决策 1）：在 P0 状态机瘦身后评估；若手写检查点/流式/子图成本过高，则 P5 前迁移。

---

## 附：Poirot 关键参考文件索引（供执行者对照源码）

| 主题 | Poirot 文件 |
| --- | --- |
| LLM 声明/路由/降级 | `poirot/backend/agents/config/{provider_profile,provider_config,fallback_model,model_router}.py` |
| 中间件装配顺序 | `poirot/backend/agents/leader/factory.py` |
| 五层记忆 | `poirot/backend/agents/memory/{schema,config,worker}.py` + `memory/strategies/default/{store,retriever,decay,forget,manager,strategy}.py` |
| 沙箱三组件 | `poirot/backend/agents/sandbox/{sandbox.py,contracts/*,translators/*,guards/*,docker/*}` |
| 上下文治理 | `poirot/backend/agents/context_engineering/strategies/default/{strategy,budget,externalizer,summarizer}.py` + `utilities.py` |
| 三层技能 | `poirot/backend/agents/skill/{store,selector,injector,parser}.py` + `skill/{evolution,eval,builtin_skills}/*` |
| 多 Agent | `poirot/backend/agents/multiagent/{specialist,subagent,sandbox_binder}.py` + `specialists/*` |
