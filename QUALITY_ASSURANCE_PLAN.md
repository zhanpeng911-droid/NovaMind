# NovaMind 质量保障改进方案

> 本文档是给执行方的完整工作说明书。所有现状描述均已在真实环境验证过（包括一次全新环境的全量测试执行），可直接按任务清单动手。
> 执行原则：**每个任务独立提交**（commit 前缀 `fix:` / `ci:` / `test:` / `chore:`），不顺手重构业务代码；过程中发现新 bug 记录到文档末尾的「遗留问题登记」而不是混入本方案提交。

---

## 一、现状盘点（已核实）

### 测试资产本身质量高

- **337 个测试函数**（约 1.4 万行源码，140 个 .py 文件），分层清晰：`tests/unit/`（26 个文件）+ `tests/integration/`（5 个文件），共享 `conftest.py` 与 `_fakes.py`。
- 测试基建好：共享可编程 `FakeLLM`（预设响应序列 / 调用历史 / bind_tools 记录）+ `FakeAuditLogger`，全套件约 **68 秒**跑完，不依赖真实 LLM 与外部服务——这是接 CI 的理想条件。
- `pytest-cov>=4.0` 已在 dev 依赖中声明。
- `test-log.md` 的排障文化（现象→根因→修复→验证）是重要资产，必须延续。

### 但全新环境下测试跑不出全绿（2026-08-26 实测）

用项目自带 uv 从零建环境执行 `uv run --extra dev pytest tests`：

| 现象 | 已查实的根因 |
|---|---|
| 收集阶段直接中断：`tests/integration/test_webui.py` 导入失败，`No module named 'fastapi'` | **依赖清单漂移**：`requirements.txt` 声明了 fastapi / uvicorn / pywebview / jieba，`pyproject.toml` 里一个都没有 |
| 排除 webui 后：**313 passed / 2 failed**，总覆盖率 **64%** | 见下两行 |
| `tests/unit/test_memory.py::TestHybridRetriever::test_chinese_tokenize_retrieves` 失败 | jieba 未安装（同上依赖漂移），中文检索退化为空格分词，断言不通过 |
| `tests/unit/test_agent_eval.py::…::test_openai_missing_key_raises_valueerror` 失败，`ssl.SSLError … certifi.where()` | uv 按 `requires-python >=3.10` 选了 **CPython 3.14**，langchain-openai 在该版本的导入期 SSL 初始化在本机崩溃。Python 版本未锁定 |

结论：`test-log.md` 中"全量 325 passed"只在作者本机旧 venv 成立。**没有 CI，"新环境是坏的"这件事没人能拦住**——这就是本方案 P0 的由来。

### 覆盖率盲区（总量 64%，但盲区集中在核心卖点模块）

| 模块 | 行覆盖 | 说明 |
|---|---|---|
| `novamind/webui/server.py` + `app.py` | **0%** | 218 行服务端逻辑完全无测试 |
| `core/skill/evolution/focus/ive_focuser.py` | 15% | 技能自进化诊断层 |
| `core/skill/evolution/manager.py` | 19% | 自进化编排 |
| `core/skill/evolution/mutators/llm_mutator.py` | 24% | 变异器 |
| `core/skill/injector.py` | 19% | 技能注入中间件 |
| `core/tools/builtins.py` | 18% | 内置工具面 |
| `core/skill/eval/analyzers/task_quality_judge.py` | 31% | 四维质量打分 |
| `core/skill/eval/analyzers/skill_judgment_analyzer.py` | 30% | 应用判定 |
| `core/task_store.py` | 41% | 定时任务存储 |
| `core/sandbox/runtimes/local_runtime.py` / `docker_runtime.py` | 39% / 42% | 沙箱执行体（安全关键） |

这些恰是 README 主打的自进化与零信任安全路径。

### 其他已核实事实

- 无任何 CI 配置（无 `.github/` 目录），但有 GitHub remote：`github.com/zhanpeng911-droid/NovaMind`。
- 无 ruff / black / mypy / pre-commit 配置。
- 版本不一致：`pyproject.toml` 为 `version = "2.0.0"`，README 自述 3.0.0。
- 根目录存在旧的手工 `venv/` 目录（uv 用的是新建 `.venv`）；`uv.lock` 存在且应被 CI 使用。

---

## 二、任务清单

### P0-A：统一依赖清单 + 锁定 Python 版本

**目标**：让 `uv sync --extra dev` 后的全量套件（含 webui 测试）可收集、可运行。

1. 把 fastapi、uvicorn、pywebview、jieba 四个依赖补进 `pyproject.toml` 的 `[project] dependencies`（版本下限照抄 requirements.txt：fastapi>=0.100.0、uvicorn>=0.20.0、pywebview>=5.0、jieba>=0.42.0）。理由：`novamind gui` 是 `[project.scripts]` 入口的一部分，按主依赖安装是合理默认。
2. 依赖唯一事实源定为 **pyproject.toml**。将根目录 `requirements.txt` 替换为由 `uv export --format requirements-txt > requirements.txt` 生成的锁定导出（或直接删除并在 README 注明用 uv 安装）。此后禁止手改 requirements.txt。
3. 新建 `.python-version` 写入 `3.12`。同时在 `pyproject.toml` 给 Python 上限留策略：`requires-python = ">=3.10"` 暂不改，但在 README「开发与测试」注明当前支持/测试过的版本为 3.10–3.13，3.14 因 langchain-openai 导入期 SSL 崩溃暂不支持（待上游修复后复核）。
4. 本地验证（模拟新环境）：删除 `.venv` 后执行 `uv sync --extra dev && uv run pytest tests -q`，预期收集无 ImportError，且原两个失败用例（jieba 分词、provider key 校验）转绿。

**验收标准**：全新 clone + sync 环境下，`pytest tests` 全量收集成功、全部通过；requirements.txt 与 pyproject.toml 不再可能漂移。

---

### P0-B：接入 GitHub Actions CI

**目标**：每次 push / PR 自动跑全量测试。套件仅约 70 秒且无需外部服务，成本极低。

新建 `.github/workflows/ci.yml`：

```yaml
name: CI

on:
  push:
    branches: [main, master]
  pull_request:

jobs:
  tests:
    name: Pytest (py${{ matrix.python-version }})
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.10", "3.11", "3.12", "3.13"]  # 3.14 暂排除，见任务 P0-A 第 3 条
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install uv
        run: pip install uv
      - name: Sync deps
        run: uv sync --extra dev --locked
      - name: Pytest with coverage
        run: uv run --no-sync pytest tests -q --tb=short --cov=novamind --cov-report=term-missing
      - name: Upload coverage report
        if: failure() || matrix.python-version == '3.12'
        uses: actions/upload-artifact@v4
        with:
          name: coverage-${{ matrix.python-version }}
          path: coverage.xml
```

注意：

- `--locked` 保证严格按 uv.lock 安装，防依赖漂移复发。
- 先跑通 3.12 单版本再加矩阵亦可；若某老版本出现非本次改动导致的偶发失败，单独建 issue 处理，不要静默从矩阵剔除。
- 提醒仓库管理员（非文件改动）：对 main/master 开启 branch protection，要求该 workflow 通过才允许合并。没有这一步，CI 只是装饰。

**验收标准**：PR 页面出现绿色 checks；故意提交一个会让测试失败的改动，CI 变红。

---

### P1：补齐覆盖率盲区（按风险排序，逐模块独立提交）

**目标**：把测试资源投向 README 主打的安全与自进化路径。沿用现有 `_fakes.FakeLLM` 模式，LLM 交互一律 mock。

优先级与验收行为：

1. **`novamind/webui/`（当前 0%）**：用 fastapi `TestClient` 直测 `server.py` 各端点——鉴权/参数校验失败分支、SSE 流式响应的事件序列、静态文件挂载。至少覆盖每个端点的 1 条正常路径 + 1 条错误路径。
2. **`core/sandbox/` SecurityGuard 与两个 runtime（39%/42%）**：这是零信任边界，优先测拒绝路径——shell 元字符拦截、环境变量拦截、白名单外命令拒绝、路径穿越拒绝（Local）、Docker 模式写入目标越出 `/mnt/novamind/user_data/` 的强制。每条安全策略必须有正反两个用例。
3. **`core/skill/evolution/` 链路（15%–24%）**：`manager.py` 状态流转、`metric_monitor` 触发阈值边界（effective_rate 刚跌破/刚高于阈值）、`score_delta_gate` 与 `git_ratchet` 的回滚判定。LLM 相关的 `llm_mutator` / `ive_focuser` mock 掉 LLM 调用后测编排与门禁逻辑。
4. **`core/skill/injector.py`（19%）与 eval analyzers（30%/31%）**：注入命中/未命中分支；`task_quality_judge` 四维加权打分的边界（权重和、极端分数）；`response_contract_checker` 契约提取与违规判定。
5. **`core/tools/builtins.py`（18%）**：各内置工具的正常路径与非法入参。

约定：

- 每完成一个模块跑 `uv run --no-sync pytest tests -q` 保持常绿，独立提交（`test: cover xxx`）。
- 完成后在 CI 中给覆盖率设底线：先实测基线，再 `--cov-fail-under=<基线-5>`，随后只升不降。全局目标不必超过 75%，重点是上表模块不再有 <50% 的。

**验收标准**：上表所列模块行覆盖率全部 ≥60%（webui ≥70%）；CI 覆盖率底线生效。

---

### P2-A：Ruff 接入

1. `dev` extras 加 `"ruff>=0.6"`；`pyproject.toml` 加：

```toml
[tool.ruff]
line-length = 100
target-version = "py310"
# 初期保持默认规则集（E4/E7/E9/F）
```

2. 本地 `uv run --no-sync ruff check novamind entry tests` 看存量：少则 `--fix` + 手动清零（单独 `style:` 提交）；多则先只对新增代码生效（CI 改为 diff 检查），分批消化存量，不为过 lint 大改逻辑。
3. 清零后加入 CI tests job：`uv run --no-sync ruff check novamind entry tests`。

**验收标准**：CI 中 lint 步骤生效，主干全绿。

### P2-B：Mypy 渐进接入（advisory → 强制）

1. `dev` extras 加 `"mypy>=1.10"`；`[tool.mypy]` 配 `python_version = "3.12"`、`ignore_missing_imports = true`、`check_untyped_defs = true`。
2. 第一批只查相对独立的包：`novamind/core/policy.py`、`token_tracker.py`、`event_bus.py`、`task_store.py`、`skill/types.py`、`sandbox/types.py`。
3. CI 先以 advisory job 运行（`continue-on-error: true`），第一批包零错误后再并入主 job 强制，后续分批扩大范围。

**验收标准**：advisory job 上线并产出报告；第一批文件零类型错误。

### P2-C：一致性修缮（半小时内的小事，顺手做掉）

- `pyproject.toml` 版本号与 README 对齐（确认当前真实版本后统一，疑似应为 3.x）。
- 评估是否删除根目录旧 `venv/`（uv 已用 `.venv`）；若保留，确认其在 `.gitignore` 中。
- 在 README「开发与测试」章节补充：本地标准命令（sync / pytest / ruff）、支持的 Python 版本、覆盖率基线数字。

---

## 三、长效规范（写入 README「开发与测试」或 CONTRIBUTING）

1. **修 bug 必须附带能复现该 bug 的回归测试**，随修复同一 PR 提交——本项目 test-log 的既有习惯，制度化。
2. 依赖变更只改 `pyproject.toml` 并提交重新生成的 `uv.lock`；requirements.txt 只允许由 `uv export` 再生。
3. `skip` 测试必须注明原因与跟踪链接。
4. 覆盖率底线只升不降；新模块合入时须自带测试。
5. 每次真实环境排障继续记入 `test-log.md`（现象→根因→修复→验证），这份日志本身就是回归知识库。

## 四、执行顺序与验收总清单

建议顺序：P0-A → P0-B（合计约半天）→ P1 按序逐模块（持续）→ P2 三项可并行。

- [ ] P0-A：全新 sync 环境 `pytest tests` 全量收集并全绿（含 webui 与原两个失败用例）
- [ ] P0-B：GitHub Actions 多版本矩阵上线，branch protection 开启
- [ ] P1：盲区模块覆盖率达标，`--cov-fail-under` 底线在 CI 生效
- [ ] P2-A：ruff 进 CI 且主干全绿
- [ ] P2-B：mypy advisory job 上线，首批文件零错误
- [ ] P2-C：版本号一致，README 开发章节更新

## 五、遗留问题登记（执行中发现的新问题记在这里）

- （执行方填写）
