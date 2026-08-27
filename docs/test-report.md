# NovaMind 真实环境测试报告

> 测试环境：Windows 11 + Python 3.13 + DeepSeek (deepseek-v4-flash) 真实 API
> 测试日期：2026-08-08

---

## 一、复杂任务链验证（真实 LLM）

### 测试设计

3 个真实复杂任务，要求 Agent 自主规划并连续调用多种工具完成：

| 任务 | 要求步骤 | 工具组合 |
| --- | --- | --- |
| project_report | 12 步 | 时间 + 计算 + 建目录 + 写文件 + 列目录 + 读文件 + 改文件 + 再读 + 画像 + 定时任务 + 查任务 + 模型信息 |
| calc_dashboard | 6 步 | 三连计算 + 建目录 + 写文件 + 读文件 + 列目录 |
| file_organizer | 7 步 | 列目录 + 读文件 + 建目录 + 写文件 + 再列 + 再读 |

### 执行结果

| 任务 | 状态 | 工具调用 | 耗时 | 策略违规 |
| --- | --- | --- | --- | --- |
| project_report | ✅ | 13 次 | 28.6s | 0 |
| calc_dashboard | ✅ | 7 次 | 6.5s | 0 |
| file_organizer | ✅ | 8 次 | 13.7s | 0 |

**project_report 完整工具链**（13 步连续调用，全程零策略违规）：

```
get_current_time → calculator → calculator → execute_office_shell
→ get_system_model_info → write_office_file → list_office_files
→ read_office_file → write_office_file → read_office_file
→ save_user_profile → schedule_task → list_scheduled_tasks
```

### 指标

| 指标 | 数值 |
| --- | --- |
| **任务完成率** | **100%** (3/3) |
| **平均耗时** | **16.3s / 任务**（最长复杂链 28.6s） |
| **平均工具调用** | **9.3 次 / 任务**（最长 13 次） |
| **策略违规** | 0 次（沙盒 + 白名单全程拦截有效） |

---

## 二、并发压力测试（10 Agent 并发）

### 测试设计

10 个不同 `thread_id` 的 agent 同时执行 10 个不同任务（计算 / 时间 / 建目录 / 写读文件 / 定时任务 / 文件总结等），并发监控 EventBus 队列深度与 emit 延迟，验证状态机会话隔离。

### 执行结果

| Agent | 任务 | 状态 | 工具调用 | 耗时 |
| --- | --- | --- | --- | --- |
| agent_a | 计算 123*456 | ✅ | 1 | 3.8s |
| agent_b | 查询当前时间 | ✅ | 1 | 1.8s |
| agent_c | 建目录 + 列文件 | ✅ | 2 | 2.8s |
| agent_d | 计算 + 模型信息 | ✅ | 2 | 2.3s |
| agent_e | 写文件 + 读文件 | ✅ | 2 | 3.7s |
| agent_f | 双计算 | ✅ | 3 | 3.3s |
| agent_g | 列目录 + 计数 | ✅ | 1 | 3.6s |
| agent_h | 计算 + 定时任务 | ✅ | 3 | 4.4s |
| agent_i | 读文件 + 总结 | ✅ | 2 | 5.9s |
| agent_j | 计算 + 写文件 | ✅ | 2 | 4.7s |

### 指标

| 指标 | 数值 |
| --- | --- |
| **并发完成率** | **100%** (10/10) |
| **并发总耗时** | **6.9s**（串行估算约 69s，**10 倍加速**） |
| **单任务平均耗时** | **3.5s** |
| **EventBus 最大队列深度** | 24 / 100（容量充足） |
| **EventBus 最大 emit 延迟** | 0.06ms |
| **EventBus 阻塞超时** | 0 次 |
| **状态串扰** | 0 处（10 个 thread_id 完全隔离） |

---

## 三、结论

1. **EventBus 无阻塞**：10 agent 并发 emit，队列峰值仅 24/100，emit 延迟 0.06ms，零阻塞超时。asyncio.Queue 背压设计在 10 并发下完全无压力。
2. **状态机零串扰**：10 个 thread_id 的消息内容完全隔离，无跨会话污染。
3. **并发扩展性良好**：瓶颈在 LLM API 网络延迟而非框架本身，5 并发 → 10 并发总耗时仅 +1.4s。
4. **安全策略全程有效**：复杂任务链与并发测试中均零策略违规，沙箱与工具白名单拦截正常。
5. **全量回归**：325 个自动化测试全部通过。

---

## 四、测试基线建设（P1：真实 API 跳过 + 共享 Fake）

> 基线对齐 Poirot 的测试组织（`tests/v1/_fake_model.py` 共享 fake + 真实 API 集成测优雅跳过）。

### 改动一：真实 API 测试优雅跳过（`tests/test_context.py`）

`TestSummaryLLMEvaluation`（真实 DeepSeek 二次评估摘要）在无 key / 无模型配置时不再报错，而是优雅跳过：

- 缺少 `OPENAI_API_KEY` 或 `DEFAULT_MODEL` → `unittest.SkipTest` 跳过
- 缺少 `langchain-openai` 依赖（ImportError）→ 兜底跳过（对齐 Poirot 的 `pytest.importorskip` 思路）

**验证**：清空 key 后运行 → `15 passed, 3 skipped`（3 个 LLM 评估测试正确跳过，而非崩溃）；配置 key 时 3 个测试走真实 API 全部通过。

### 改动二：共享 Fake 抽取（新增 `tests/_fakes.py`）

消除 5 个测试文件里重复的 FakeLLM / FakeAuditLogger / FakeAudit 定义，集中到一个共享模块：

| 共享类 | 能力 | 替换的重复定义 |
| --- | --- | --- |
| `FakeLLM` | 预设响应序列 + `call_history` + `call_count` + `bound_tools` 记录 | test_e2e / test_agent / test_agent_wiring / test_multiagent_wiring / test_agent_eval |
| `FakeAuditLogger` | dict 事件列表 + `get_events` 过滤 | test_e2e / test_agent（内联 2 处） |
| `FakeAudit` | (event, kwargs) 元组事件列表 | test_agent_wiring / test_multiagent_wiring |

**验证**：重构后 6 个受影响文件 `74 passed`，全量 `325 passed`，零回归。行为完全等价（仅 test_agent_wiring 的默认兜底回复从隐式 `"done"` 改为显式传入，保持断言不变）。

### 净效果

| 指标 | 改动前 | 改动后 |
| --- | --- | --- |
| 重复 Fake 定义 | 5 个文件各写一份 | 集中 1 份，按需导入 |
| 真实 API 测试缺 key | 报错 / 400 | 优雅跳过 |
| 全量测试 | 325 | 325（零回归） |

---

## 五、测试基线建设（P2：decay 数学不变量 + 真实 JSONL 落盘）

### 改动一：Ebbinghaus 衰减数学不变量测试（`tests/test_memory.py` → `TestDecayMathInvariants`，新增 10 个）

对齐 Poirot `test_decay.py` 的数学不变量思路，把衰减公式当纯数学锁死：

| 不变量 | 断言 |
| --- | --- |
| 精确公式（time_hours=0） | `strength = base + 0 + importance×0.05` 精确到 6 位小数 |
| 时间单调性 | time_hours 增大 → strength 严格递减（s0 > s1 > s2） |
| 类型排序 | 24h 后 procedural > semantic > episodic（衰减率 0.005<0.02<0.1） |
| access boost 精确公式 | 强化项 = `log(1+access_count) × 0.1` |
| access_count / importance 单调 | 越大 → strength 越大 |
| 钳制 | 极端输入下 strength 恒 ∈ [0,1] |
| 纯函数 | 多次调用结果一致 + trace 字段不被修改 |
| last_accessed<=0 | 用 created_at 算 time_hours |
| config 覆盖隔离 | `set_memory_config` 覆盖生效 + `tearDown` 恢复全局 config（防污染） |

### 改动二：真实 AuditLogger 写 JSONL 落盘端到端（`tests/test_e2e.py` → `TestAuditLoggerJsonlPersistence`，新增 2 个）

不用内存 FakeAuditLogger，改用**真实 AuditLogger 指向临时目录**跑完整 agent 循环：

- **单轮对话**：断言 `{thread_id}.jsonl` 真实落盘、事件字段完整（thread_id/ts/event）、顺序 `context_pack_loaded → llm_input → ai_message`、ai_message 内容写入
- **工具轮**：断言完整序列 `tool_call → policy_check → tool_result → ai_message` 顺序落盘、工具名写入

单例隔离沿用 `test_logger.py` 套路（`AuditLogger._instance = None` 重置 + shutdown 冲刷队列），不污染全局审计器。

### 净效果

| 指标 | P1 后 | P2 后 |
| --- | --- | --- |
| 全量测试 | 325 | **337**（+12，零回归） |
| decay 数学覆盖 | 3 个基础 | 12 个（精确公式/单调/类型排序/access boost/config 隔离） |
| 审计落盘验证 | 仅内存 Fake | 真实 JSONL 落盘 + 事件序列 |

---

## 六、测试基线建设（P3：目录拆分 unit/ + integration/）

对齐 Poirot 的测试组织（`v1/unit/<模块>/` + `v1/integration/`），把平铺的 `tests/` 拆成两层：

```
tests/
├── conftest.py      # 让 tests/ 可导入（_fakes 跨子目录可用）
├── _fakes.py        # 共享 fake（P1）
├── unit/            # 24 个文件：单模块单元测试
└── integration/     # 5 个文件：组件组装 / 全链路
```

- **integration/**：`test_e2e.py`（完整 create_agent_app 全链路 + 真实 JSONL 落盘）、`test_integration.py`（CLI/端到端）、`test_webui.py`（GUI 后端）、`test_agent_wiring.py` / `test_multiagent_wiring.py`（接线端到端）
- **unit/**：其余 24 个（状态机 / 记忆 / 技能 / 沙箱 / 上下文治理 / logger / provider 等单模块测试）

**关键点**：新增 `tests/conftest.py` 把 `tests/` 插入 sys.path，保证 `from _fakes import ...` 在 unit/ 与 integration/ 两个子目录下都能解析（测试文件无需改 import）。`pyproject.toml` 的 `testpaths=["tests"]` 递归生效，无需改动。

**验证**：全量 **337 passed**，零回归；`--collect-only` 确认从新路径正常收集。

---

## 附：自动化测试体系（337 个，29 个文件，unit + integration）

### integration/（5 个文件，67 个测试）

| 测试文件 | 数量 | 覆盖 |
| --- | --- | --- |
| test_webui.py | 22 | 桌面 GUI 后端接口、会话/技能/监控面板 |
| test_multiagent_wiring.py | 19 | 多 Agent 委派接线、pi 检测、Orchestration 中间件 |
| test_integration.py | 15 | CLI、日志、SQLite 隔离、端到端循环 |
| test_e2e.py | 8 | 端到端全链路（工具循环、持久化、策略、迭代上限、真实 JSONL 落盘） |
| test_agent_wiring.py | 3 | before/after_model、wrap_tool_call 触发（共享 Fake） |

### unit/（24 个文件，270 个测试）

| 测试文件 | 数量 | 覆盖 |
| --- | --- | --- |
| test_sandbox.py | 32 | 三组件沙箱：路径穿越/元字符拦截/白名单/Docker 挂载区/warm pool |
| test_memory.py | 31 | 五层记忆：Schema/衰减（含数学不变量）/遗忘/检索强化/后台沉淀 |
| test_state_machine.py | 22 | 状态容器、边路由、SQLite 持久化、迭代上限 |
| test_context.py | 18 | 裁剪、摘要词法评估、LLM 二次评估（真实 API，缺 key 跳过） |
| test_multiagent.py | 15 | 多 Agent：委派工具生成、prompt/错误归一化、UTF-8 解码 |
| test_agent.py | 14 | Token 提取、策略拦截、中间件管道 |
| test_agent_eval.py | 14 | 多轮对话、工具收敛、会话隔离 |
| test_context_engineering.py | 13 | 上下文治理：token 预算、外化/摘要/熔断 |
| test_monitor.py | 13 | Rich 审计面板、事件流 |
| test_skill.py | 12 | 三层技能：DAG/选择器/进化/契约检查 |
| test_fallback_model.py | 9 | 链式降级：瞬时错误切换、is_default 恒在链尾 |
| test_session.py | 9 | 会话持久化与恢复 |
| test_runtime_tracker.py | 8 | applied-rate 趋势判定 + 退化检测 |
| test_token_tracker.py | 8 | 成本估算与会话统计 |
| test_agent_middleware.py | 7 | 横切中间件钩子 |
| test_logger.py | 7 | 脱敏、有界队列背压、优先级驱逐 |
| test_model_router.py | 7 | 多 provider 路由、真实窗口解析 |
| test_provider.py | 7 | 提供商工厂、缺 key/缺依赖报错 |
| test_evolution_flag.py | 6 | 技能/多 Agent 进化 L2/L3 开关提醒 |
| test_middleware.py | 6 | 中间件管道合并、默认行为 |
| test_doctor.py | 4 | 架构健康体检 |
| test_state_machine_hooks.py | 4 | 状态机钩子接线 |
| test_sandbox_tools.py | 3 | 沙箱工具面 |
| test_plugin_loader.py | 1 | 动态插件加载 |

---

## 七、质量保障专项执行报告（QUALITY_ASSURANCE_PLAN P0-P2 全量落地）

> 执行日期：2026-08-26。每个任务独立提交；过程中发现的 6 项新问题已登记至方案「遗留问题登记」。

### 环境与依赖（P0-A）

| 项 | 结果 |
| --- | --- |
| pyproject 补齐 fastapi/uvicorn/pywebview/jieba | ✅ 漂移消除 |
| requires-python `>=3.12,<3.14` + `.python-version=3.12` | ✅ 新环境不再误选 3.14 |
| requirements.txt 改为 uv export 锁定导出 | ✅ 唯一事实源 pyproject |
| 删 `.venv` 后 `uv sync --extra dev && pytest` | ✅ 337→459 全绿 |

### CI（P0-B）

GitHub Actions：pytest 矩阵 3.12/3.13（`--locked`，覆盖率产物上传）+ ruff 强制 job + mypy advisory job。branch protection 需仓库管理员在 GitHub 后台开启。

### 覆盖率盲区（P1）

| 模块 | 前 | 后 |
| --- | --- | --- |
| sandbox 整体 | 63% | **80%**（local_runtime 39→83、docker_runtime 42→97、audit_guard 41→100） |
| ive_focuser / llm_mutator / evolution manager | 15/24/19% | **91 / 83 / 80%** |
| metric_monitor / score_delta_gate / git_ratchet | — | **96 / 96 / 覆盖** |
| injector / task_quality_judge / skill_judgment_analyzer | 19/31/30% | **100 / 96 / 91%** |
| builtins 工具面 | 18% | **88%** |
| webui server.py / app.py | 61 / 23% | **70 / 42%** |
| **全量** | 66% | **75%**，CI 底线 `--cov-fail-under=70` 只升不降 |

### 工具链（P2）

- **ruff**（E4/E7/E9/F 最小集）：存量 94 → 清零；CI 强制 job
- **mypy**（首批 6 文件，follow_imports=silent）：零错误；CI advisory job

### 测试规模演进

147（v2 报告） → 337（P1-P3 基建） → **459**（本专项），unit/integration 两层结构。

### 附带修复的真实 bug

1. ConversationStore SQLite 连接泄漏（Windows 文件锁）
2. langchain_core.messages 进程内重载导致的 isinstance 代际分裂（改 `.type` duck-typing）
3. 测试清空整个 os.environ 剥掉 SystemRoot 致 uv 版 OpenSSL 崩溃（4 处）


---

## 八、质量保障专项执行报告 R2（QUALITY_ASSURANCE_PLAN_R2 全量落地）

> 执行日期：2026-08-26。逐任务独立提交；起点快照数字全部实测核对一致。

### P0-A（e2f117c）
- branch protection 已通过 GitHub API 配置：必需 Pytest(py3.12/3.13)+Ruff，typecheck 不计入，enforce_admins=false 保留管理员直推
- CI 覆盖底线 70→74；app.py 编排测试 42%→**100%**（修正方案误设的 TestClient 手段，改 mock uvicorn/webview）；pre-commit 接入（ruff + unit 冒烟）

### P1 覆盖（B1-B4）
| 模块 | 前 → 后 |
|---|---|
| store/selector/parser/checks | 62/52/70/65 → **97/98/98/98%** |
| docker provider / cross-process lock / sandbox_tools | 59/50/55 → **95/100/97%** |
| plugin_loader | 57 → **95%** |
| programmatic_bridge / permissive_guard / identity_translator | 67/67/62 → **100%** |
| 总覆盖 | 75 → **81%**，CI 底线 74→**78** |

新增 hypothesis 属性测试（translators）：Local「翻译结果永不逃逸工位根」+「mask 为 translate 逆操作」、Docker「reverse 必落 host 根 + 前缀外必拒」，各 150 例随机。

### P2 工具链
- mypy 第二批 9 文件零错误，typecheck job 转强制
- ruff 规则集 +B（bugbear）并清零（8×B904 from None、2×B027 abstract、1×B019 lru_cache 泄漏）
- mutmut 变异试点：**暂缓**（不支持原生 Windows，需 WSL），命令与说明记入遗留 #5

### P3 功能债（三件全落地）
- 记忆 storage_path 收敛统一数据根（跨 CWD 同位置）+ 旧数据一次性迁移
- save_user_profile 备份加毫秒（同秒不合并）
- TASKS_FILE 单一来源（测试只需 patch 一处）

### 附带修复的真 bug
- plugin_loader.reload_all cache_clear 绑定方法 AttributeError（ad05e8e）
- 固定 thread_id 跨运行累积致裁剪误判（006084a）
- langchain 消息类重载代际分裂 15 处 isinstance 全库根治（006084a）

### 测试规模演进
459 → **561**（unit/integration 两层；全程 `--cov --cov-fail-under=78` 稳定全绿）
