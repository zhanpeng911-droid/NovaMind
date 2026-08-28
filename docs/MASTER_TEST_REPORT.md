# NovaMind 总测试报告（Master）

> 一份看懂全部测试与质量状态。本文件是**唯一入口**，把三轮质量工程 + 全功能验收整合在一起；粒度细节在各轮子文档（见附录索引）。
> 最新更新：2026-08-26。

---

## 一、一句话快照

**563 个常规自动化测试（unit + integration）全绿，覆盖 81%、CI 底线 78；34 个真实/环境功能验收用例（Part A/B/C）31 过、1 环境失败（docker daemon 未启动）、2 跳过；真实 LLM（DeepSeek）19/19 全过。README 宣称的每一项能力都已实际验过或明确标记状态。**

一次跑完全部：
```bash
uv run --no-sync pytest tests -q --ignore=tests/functional   # 常规回归（~25s）
uv run --no-sync pytest tests/functional -q                 # 功能验收（真实 LLM + 环境）
```

---

## 二、测试体系全景

```
tests/
├── unit/          # 单模块单元测试（24 文件，≈510 项）
├── integration/   # 组件组装/端到端（5 文件，≈53 项）
├── functional/    # 全功能验收（Part A 无依赖 / Part B 真实 LLM / Part C 特殊环境）
│   ├── conftest.py          # real_api 标记 + 缺 key 自动跳过
│   ├── test_part_a_functional.py   # A1-A16：CLI/编排/中间件/记忆机制/心跳等
│   ├── test_part_b_real.py         # B1-B16：真实 LLM 全功能面 19 用例
│   └── test_part_c_env.py          # C1-C4：docker/WSL/GUI/MCP
├── _fakes.py     # 共享可编程 FakeLLM/FakeAudit（消除重复）
└── conftest.py   # 共享导入
```

**工具链与 CI（全部生效）**：

| 项 | 状态 |
|---|---|
| GitHub Actions 矩阵 | Python 3.12/3.13，`uv sync --locked`（依赖不漂移） |
| CI 覆盖底线 | `--cov-fail-under=78`，只升不降 |
| ruff | `E4/E7/E9/F + B`（bugbear）全清零，CI 强制 |
| mypy | 两批 15 文件零错误，typecheck job 已转必需 |
| pre-commit | ruff 提交前 + unit 冒烟推送前 |
| branch protection | 必需 Pytest(3.12/3.13)+Ruff，管理员直推保留 |
| 依赖清单 | 唯一事实源 `pyproject.toml`；`requirements.txt` 由 `uv export` 再生 |

---

## 三、测试规模演进史

| 阶段 | 常规测试 | 说明 |
|---|---|---|
| 项目早期 | 325 | 平铺 tests/，README 声称 325 |
| P1-P3 测试基建 | **337** | 拆 unit/integration、共享 fake、decay 数学不变量、真实 JSONL 落盘 |
| R1 质量工程（P0-P2） | **459** | 依赖统一 + Python 锁 3.12-3.13 + CI + 覆盖率盲区 + ruff/mypy |
| R2（覆盖深水区 + 功能债） | **561** | B1-B4 覆盖（store/沙箱/plugin_loader/零散）+ 三件功能债 |
| R3（全功能验收 + 缺陷修复） | **563** | 新增 functional 34（真实 LLM 19）；修复 L4 索引 / 默认接线两缺陷 |
| **当前** | **563 + 34** | 常规 563 passed；functional 31 passed / 1 env-fail / 2 skip |

覆盖：66% → 75% → **81%**；CI 底线 70 → 74 → **78**。

---

## 四、三轮质量工作摘要

### R1（`docs/test-report.md` 一~七章）
- **P0**：依赖漂移修复（pyproject 补 fastapi/uvicorn/pywebview/jieba）、`.python-version=3.12`、`requires-python>=3.12,<3.14`、requirements 改 uv export；GitHub Actions CI 上线。
- **P1**：五模块覆盖盲区补齐（sandbox 80%、evolution 91/83/80%、analyzers、builtins 88%、webui server 70%）。
- **P2**：ruff 接入清零、mypy 首批零错误、版本号对齐 3.0.0。

### R2（`docs/test-report.md` 八章）
- **P0-A**：branch protection 配置、app.py 编排测试 42%→100%、pre-commit。
- **P1**：store/selector/parser/checks → 97/98/98/98%；docker provider/lock/sandbox_tools → 95/100/97%；plugin_loader → 95%（含修真 bug）；hypothesis 路径翻译不变量。
- **P2**：mypy 第二批 9 文件零错误转强制；ruff +B 清零。
- **P3**：记忆 storage_path 收敛统一数据根 + 迁移、备份毫秒、TASKS_FILE 单源化。

### R3（`docs/functional-report.md` + 修复）
- 全功能验收：把 README 每条能力真实执行（Part A/B/C），产出 34 用例 + 缺陷登记 5 项。
- 修复两个产品缺陷：**L4 检索索引接线**（B10 转绿）、**默认运行时记忆/治理中间件接线**。

---

## 五、功能验收结论总表（最新）

| # | 功能 | 结果 | 说明 |
|---|---|---|---|
| A1 | CLI 四命令 | ✅ | help/doctor --json/monitor 全通 |
| A2 | 自研编排（10 轮收敛 + 迭代上限） | ✅ | 死循环被上限终止并审计 |
| A3 | 横切中间件五钩子 | ✅ | 按序触发、可扩展 |
| A4/A9 | 模型路由/降级 + 技能自进化 | ✅ | 真实 LLM 链路跑通 |
| A5 | 五层记忆 L1-L5 | ✅ | 两缺陷已修复，B10 转绿 |
| A6 | 三组件沙箱 Local | ✅ | 零信任拦截 + 属性测试 |
| A7 | 上下文治理 P0-P5 | ✅ | 真实长对话收尾 |
| A8 | 技能系统 37 内置 | ✅ | 全部加载 |
| A10 | 内置工具面 | ✅ | 真实组合全通 |
| A11 | 多 Agent 委派（pi） | ✅ | 真实委派结构化回传 |
| A12 | 审计 JSONL/脱敏/序列 | ✅ | 可重放、无密钥泄漏 |
| A13 | 心跳调度 | ✅ | 到期触发 + repeat 递减 |
| A14 | MCP Adapter | ⏭️ 跳过 | mcp 依赖未装（环境） |
| A15 | WebUI server + 面板 | ✅ | TestClient 守卫 + webview 可导入 |
| A16 | 配置 .env/provider | ✅ | 缺省/覆盖/非法报错 |
| A17 | Docker 挂载区强制 | ❌（环境） | docker daemon 未启动；单测 95%+ 守卫 |
| A18 | WSL2 路径翻译 | ✅ | 需 `NOVAMIND_SANDBOX_EXECUTOR=wsl` 显式开启 |
| A19 | GUI 桌面窗口 | ⏭️ 跳过 | 无头会话；webview 可导入，交互留手工 |

**真实 LLM（DeepSeek）Part B：19/19 全过**（任务链 3、降级 2、进化、L5 沉淀、治理、质量评估、pi 委派、审计重放、路由、召回注入、摘要评估、上下文包、画像、工具面、token 用量、长循环）。

---

## 六、缺陷登记（整合，含状态）

| # | 级别 | 功能 | 现象 | 状态 |
|---|---|---|---|---|
| 1 | 高 | L4 记忆召回 | encode 后记忆不进检索索引，retrieve 恒 0 命中 | ✅ **已修复**（`_wrap_store` 接线，B10 转绿） |
| 2 | 中 | L4/L5 + 治理默认激活 | 默认 CLI/GUI 运行时未挂载记忆/治理中间件 | ✅ **已修复**（`default_stack` 接入入口） |
| 3 | 低 | C1 Docker | docker CLI 在、daemon 未启动 | ⏳ 环境项，daemon 启动后复跑 |
| 4 | 信息 | WSL2 翻译 | executor 默认关闭，需显式开启 | 📄 配置依赖，文档已注明 |
| 5 | 信息 | L5 抽取类型 | 真实 LLM 自判记忆类型（可归 semantic） | 👀 设计留观 |
| 6 | 低 | 测试卫生 | 部分历史测试用固定 thread_id 写真实 DB | ✅ 已处理高危项，全量隔离待后续 |
| 7 | 中 | plugin_loader.reload | `cache_clear` 绑定方法 AttributeError | ✅ 已修复 |
| 8 | 高 | 环境清空反模式 | 测试清空 os.environ 剥掉 SystemRoot 致 SSL 崩 | ✅ 已修复（`env_without`） |

---

## 七、工具链与工程指标

| 指标 | 值 |
|---|---|
| 常规自动化测试 | **563** passed（unit≈510 / integration≈53） |
| 功能验收用例 | **34**（31 pass / 1 env-fail / 2 skip） |
| 真实 LLM 用例 | **19/19** 全过（DeepSeek，每轮 ~2 分钟） |
| 总覆盖率 | **81%**，CI 底线 **78** |
| ruff | 0 错误（含 bugbear B 类） |
| mypy | 15 文件零错误，typecheck 强制 |
| 运行时缺陷存量 | 2 已修复 / 3 环境或留观 |

---

## 八、跑一遍的命令清单

```bash
# 常规回归（unit + integration，不含 functional）
uv run --no-sync pytest tests -q --ignore=tests/functional
uv run --no-sync pytest tests -q --cov=novamind --cov-report=term --cov-fail-under=78

# 全功能验收（真实 LLM + 特殊环境；缺 key 自动跳过）
uv run --no-sync pytest tests/functional -q

# 工具链
uv run --no-sync ruff check novamind entry tests
uv run --no-sync mypy novamind/core/policy.py novamind/core/token_tracker.py \
    novamind/core/event_bus.py novamind/core/task_store.py \
    novamind/core/skill/types.py novamind/core/sandbox/types.py \
    novamind/core/middleware.py novamind/core/logger.py \
    novamind/core/context_engineering/__init__.py novamind/core/context_engineering/contract.py \
    novamind/core/multiagent/types.py novamind/core/memory/config.py \
    novamind/core/memory/schema.py novamind/core/memory/types.py novamind/core/memory/bootstrap.py

# 发布前门禁：functional 全绿（除已登记环境项）
```

---

## 九、附录索引（粒度细节在各子文档）

| 文档 | 内容 |
|---|---|
| `docs/test-report.md` | R1/R2 质量工程详情（真实任务链、压力测试、P1-P3 基建、P0-P2 执行报告） |
| `docs/functional-report.md` | R3 全功能验收 Part A/B/C 逐项证据 + 缺陷登记 + 修复记录 |
| `QUALITY_ASSURANCE_PLAN.md` | R1 工作说明书 + 遗留登记 |
| `QUALITY_ASSURANCE_PLAN_R2.md` | R2 工作说明书 + 遗留登记 |
| `QUALITY_ASSURANCE_PLAN_R3.md` | R3 验收框架 + 缺陷登记 |
