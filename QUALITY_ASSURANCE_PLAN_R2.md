# NovaMind 质量保障改进方案 · 第二轮（R2）

> 前置：第一轮方案（`QUALITY_ASSURANCE_PLAN.md`，P0–P2）已于 2026-08-26 全量落地并经第三方复核属实（459 测试全绿 / 总覆盖 75% / ruff-mypy-CI 就绪）。本文件是**下一轮**工作说明书，格式与执行原则同上轮：每任务独立提交（`test:` / `chore:` / `fix:` / `refactor:` 前缀），不顺手重构业务代码，新发现的问题记入文末「遗留问题登记」。
> 执行环境：Windows + Git Bash + uv。所有命令在仓库根目录执行。

---

## 一、起点快照（2026-08-26 复核实测，无需重新测量）

- 测试：**459 passed**，约 100 秒跑完，无外部服务依赖；`uv sync --extra dev` 后即可运行。
- 总覆盖率：**75%**（6458 语句 / 1609 未覆盖）；CI 底线 `--cov-fail-under=70`。
- 工具链：ruff 最小规则集已清零并强制；mypy advisory job 首批 6 文件零错误；CI 矩阵 Python 3.12/3.13（`--locked`）。

### 本轮要处理的剩余低覆盖文件（精确到未覆盖语句数）

| 文件 | 覆盖率 | 未覆盖 | 备注 |
|---|---|---|---|
| `novamind/core/plugin_loader.py` | 57% | 78 行 | 全库最大的未测文件 |
| `novamind/core/skill/store.py` | 62% | 65 行 | 技能 SQLite 数据层 |
| `novamind/core/tools/sandbox_tools.py` | 55% | 50 行 | 沙箱工具面 |
| `novamind/core/sandbox/docker/docker_sandbox_provider.py` | 59% | 75 行 | 安全关键 |
| `novamind/core/skill/selector.py` | 52% | 32 行 | 技能选择 |
| `novamind/core/sandbox/docker/cross_process_lock.py` | 50% | 12 行 | 并发正确性 |
| `novamind/core/skill/parser.py` | 70% | 15 行 | 技能文本解析 |
| `novamind/core/skill/eval/analyzers/checks.py` | 65% | 20 行 | 评估检查 |
| `novamind/core/skill/evolution/eval/programmatic_bridge.py` | 67% | 4 行 | |
| `novamind/webui/app.py` | **42%** | 28 行 | 上轮验收线 webui≥70% 字面未达 |

### 已知功能债（来源：test-log.md 2026-08-21 遗留事项 + QA 方案遗留登记 #5/#6）

- 记忆模块 `storage_path` 默认相对 CWD，不跟随 DATA_ROOT 约定，跨目录启动会散落。
- `save_user_profile` 备份时间戳精确到秒，同秒连续保存会合并备份文件。
- 定时任务读写经 `task_store` 模块级 `TASKS_FILE`，路径绑定分散在两处。
- `delegate_to_subagent` 未接线（`SubagentProvider` 只有 Protocol 无默认实现）——**属新功能开发，本轮不立项**，仅登记。

---

## 二、任务清单

### P0-A：收尾性小任务（合计半天内）

1. **branch protection 落实确认**（管理员在 GitHub 后台操作，非代码改动）：main/master 要求 CI 三个 job（tests/lint/typecheck 中 typecheck 为 advisory 不计入必需）全部通过方可合并。若已开启，截图留档即可。
2. **覆盖率底线只升不降的第一步**：实测已是 75%，将 CI `--cov-fail-under=70` 提至 **74**（留 1 点波动余量）。后续每轮按此规则递推。
3. **webui/app.py 补齐至上轮验收线**：当前 42%，目标 ≥70%。用 fastapi `TestClient` 覆盖 `app.py` 中启动辅助、路由注册、静态资源回退等分支（33–78 行为缺失区）。
4. **pre-commit hooks 引入**：`.pre-commit-config.yaml` 挂 ruff（`ruff check` + `ruff format --check` 若团队接受格式化；不接受则仅 check）+ `pytest tests/unit -q -x` 快速冒烟。README「开发与测试」补一行 `pre-commit install` 说明。

### P1：覆盖深水区（核心工作量，按序逐模块提交）

约定沿用上轮：LLM 交互一律用 `_fakes.FakeLLM` mock；每个模块先写拒绝/异常分支再写正常路径；每模块独立提交且全量保持常绿。

**B1 —— 技能数据层（优先级最高：这是技能系统的 truth source）**

- `core/skill/store.py`（65 行未覆盖）：SQLite 错误分支（磁盘满/损坏库/并发写）、版本 DAG 环检测、四计数器更新的事务性、迁移/初始化幂等。
- `core/skill/selector.py`（32 行未覆盖）：quality filter 过滤边界（刚好压线分数）、LLM hybrid 选择降级到纯规则排序的分支、空技能库行为。
- `core/skill/parser.py` + `eval/analyzers/checks.py`：畸形技能文本容错、契约规则提取的非法输入。

**B2 —— 沙箱剩余分支（安全关键，正反用例成对）**

- `docker_sandbox_provider.py`（75 行未覆盖）：容器创建失败重试、挂载区越界拒绝、warm pool 取用时实例已被销毁的竞态。
- `cross_process_lock.py`：锁竞争（双进程抢锁）、持锁进程崩溃后锁释放、超时获取。
- `tools/sandbox_tools.py`（50 行未覆盖）：命令拼装注入尝试、超时传播、stderr 回传。
- **建议引入 hypothesis 属性测试**（dev extras 加 `hypothesis`）专用于 PathTranslator 家族：不变量为「translate 后的路径永不逃逸出工位根」「同一路径重复 translate 幂等」。随机路径串比手写用例更能扫出穿越漏洞。

**B3 —— plugin_loader.py（78 行未覆盖，全库最大盲区）**

- 合法插件加载、重复 id 冲突、入口点缺失、加载期抛异常的隔离（坏插件不能拖垮主进程）、热卸载语义（若有）。当前 112–334 行大片缺失说明异常路径几乎没测过。

**B4 —— 杂项收尾**

- `programmatic_bridge.py`、`permissive_guard.py`、`identity_translator.py` 等零散文件补到 ≥80%；顺手核对 `skill/store.py` 补测后 `selector.py` 是否连带上升。

**B1–B4 完成后的全局动作**：总覆盖预计达 80%+，将 CI 底线提至 78，并在 README 记录新基线。

### P2：工具链强化

1. **mypy advisory → 强制 + 扩范围**：
   - 第二批目标文件：`novamind/core/middleware.py`、`policy.py` 已在首批则跳过，改列 `core/memory/*` 入口、`core/context_engineering/__init__.py`、`core/multiagent/types.py`、`core/logger.py`。
   - 第二批零错误后，把 CI 中 typecheck job 的 `continue-on-error: true` 移除并入必需检查；此后新增文件自动纳入约束（靠扩 files 列表推进，暂不上全量 strict）。
2. **ruff 规则集只增不减的第一步**：在现有 `["E4","E7","E9","F"]` 基础上加 `"B"`（bugbear，抓真实可疑模式）。存量预计少量，单独 `style:` 提交清零后再进 CI。不加 UP/N 等风格类，避免噪音。
3. **变异测试试点（可选，时间允许再做）**：dev extras 加 `mutmut`，仅对三个纯逻辑模块试跑——`core/policy.py`、`core/token_tracker.py`、记忆衰减公式所在模块。目标是回答「459 个测试的断言质量如何」，产出一份存活变异体清单供人工研判；**不设门槛、不进 CI**。

### P3：功能债清理（小而确定的三件）

1. **记忆 storage_path 收敛到 DATA_ROOT**：默认值改为跟随项目统一的数据目录约定，保留显式配置覆盖；补一个「跨 CWD 启动落到同一位置」的回归测试。改动前先 grep 所有读取该配置的调用点。
2. **save_user_profile 备份粒度**：文件名加入毫秒或单调序号，消除同秒合并；用假时钟写「同秒连存两次产生两个备份」的用例（上轮遗留 #5）。
3. **task_store 路径单一来源**：`TASKS_FILE` 收敛为从单一 config 来源派生，消除测试需 patch 两处的别扭（上轮遗留 #6）；重构后现有测试应只需 patch 一处。

---

## 三、长效规范（延续 + 新增一条）

延续上轮全部规范。新增：

6. **覆盖率底线递推规则制度化**：每轮结束时实测一次总覆盖，新底线 = 实测值 − 1，只升不降，写入 CI 与 README。
7. **属性测试定位**：凡「输入空间大、行为是不变量」的模块（路径翻译、分词、裁剪、序列化往返），新测试优先用 hypothesis 属性测试而非枚举用例。

## 四、执行顺序与验收总清单

建议顺序：P0-A（半天）→ P1-B1 → P1-B2 → P2 工具链（可与 B3/B4 并行）→ P1-B3/B4 → P3。

- [ ] P0-A：branch protection 确认；底线提至 74；app.py ≥70%；pre-commit 可用
- [ ] P1-B1：store/selector/parser/checks 达标（store ≥85%、selector ≥85%、parser/checks ≥85%）
- [ ] P1-B2：docker provider ≥80%、lock ≥90%、sandbox_tools ≥80%；hypothesis 路径翻译不变量上线
- [ ] P1-B3：plugin_loader ≥80%
- [ ] P1-B4：零散文件 ≥80%，总覆盖 ≥80%，CI 底线提至 78
- [ ] P2：mypy 转强制 + 第二批文件零错误；ruff +B 清零；变异测试报告产出（可选）
- [ ] P3：三项功能债各自带回归测试合入

## 五、遗留问题登记（本轮执行中发现的新问题记在这里）

- **#1 register() 静默丢弃构造器四计数器（设计使然，留档）**：SQLiteSkillStore.register 只写身份不落计数器，计数器仅经 record_outcome 累积。行为符合设计，但构造器传入的 sel/app/comp/fb 被静默忽略，测试需直连 UPDATE 造数。已按真实语义写测试，不修。
- **#2 plugin_loader.reload_all 真 bug（已修复，ad05e8e）**：`self._load_content.cache_clear()` 中 lru_cache 包装器的 cache_clear 挂在函数上，实例访问得到绑定方法（无该属性），reload_skills() 一调即 AttributeError。改类级访问。
- **#3 固定 thread_id 累积致裁剪误判（已修复，006084a）**：policy_test 等固定 id 在 workspace SQLite 跨运行累积超 40 回合，触发裁剪裁掉用户消息 → 策略判定失效。相关测试改唯一 id + 清理。测试卫生待统一（历史遗留固定 id 测试）。
- **#4 langchain 消息类重载代际分裂（已根治，006084a）**：上轮修 agent/context 后，路由与 tool_executor 及 context_engineering/middlewares 仍残留 15 处 isinstance；本轮全库鸭子化 .type。带 --cov 全量连跑稳定。
- **#5 mutmut 变异测试试点暂缓（未执行，可选）**：mutmut 不支持原生 Windows（需 WSL）。本轮未在 WSL 搭建变异环境。执行命令（WSL 内）：`cd /mnt/d/claudecode/NovaMind && uv sync --extra dev && uv run python -m mutmut run --paths-to-mutate novamind/core/policy.py novamind/core/token_tracker.py novamind/core/memory/strategies/default/decay.py --runner "uv run --no-sync python -m pytest tests/unit/test_token_tracker.py tests/unit/test_agent.py::TestHarnessPolicy tests/unit/test_memory.py::TestDecay tests/unit/test_memory.py::TestDecayMathInvariants -q" --use-coverage`。不设门槛、不进 CI。
- **#6 测试常写真实 DB_PATH**：大量历史测试用固定/真实 workspace SQLite，CI 全新环境不受影响，但本地反复跑会累积状态。建议后续引入 `tmp_path` + 全局 DB_PATH 夹具统一隔离。

