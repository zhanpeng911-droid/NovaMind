# NovaMind 全功能验收方案（R3）

> 性质说明：本轮**不是**补测试覆盖率的轮次，而是**全功能验收**——把 README 宣称的每个产品能力真实执行一遍，逐项判定「能用 / 部分可用 / 不可用 / 未接线」，产出可追溯的功能验收报告。与代码质量测试（R1/R2 已完成，561 测试 / 81% 覆盖）互补，本轮回答的是「产品真的能干活吗」。
> 执行原则：逐功能验收、如实记录 PASS/FAIL/部分/跳过；发现的缺陷记入文末「缺陷登记」，不顺手修（除非是阻塞验收本身的阻塞性 bug）。
> 执行环境：Windows + Git Bash。**依赖清单**：Part A 无需外部服务；Part B 需 LLM API key（OpenAI/Anthropic/DashScope 任一，或本地 Ollama）与 `.env`；Part C 需 Docker / WSL2 / 桌面 GUI 环境，缺则该项标记「跳过」。

---

## 一、验收框架与方法

每个功能按统一模板验收，最终汇总成 `docs/functional-report.md`：

```text
功能名 | 验收方式 | 前置条件 | 通过标准 | 结果(PASS/FAIL/部分/跳过) | 备注/缺陷号
```

通用方法：

- **Part A**（无外部依赖）：优先复用现有测试套件做「全功能冒烟」，再对无测试守卫的功能补最小验收用例；能自动化的写成 pytest，不能自动化的给出可复制的手工命令与预期输出。
- **Part B**（真实 LLM）：新建 `tests/functional/`，统一 `@pytest.mark.real_api` + conftest 缺 key 自动 `SkipTest`；每次验收前手动跑一次全量 Part B（记录耗时与花费），可复刻的用例进 CI 之外的「发布前必跑清单」。
- **Part C**（特殊环境）：逐项确认环境存在则验、缺失则显式「跳过」，不静默略过。

---

## 二、功能验收总表（以 README「核心能力」矩阵为骨架）

| # | 功能 | 验收方式 | 外部依赖 | 现状参考 |
|---|---|---|---|---|
| A1 | CLI 入口（run/monitor/doctor/gui 四命令） | 手工 + 命令断言 | 无 | 部分未验 |
| A2 | 自研编排：状态机 agent→tools→agent + 迭代上限 + 持久化 | 自动 | 无 | 单测 94% |
| A3 | 横切中间件五钩子顺序与数据流 | 自动 + 集成 | 无 | middleware 单测有 |
| A4 | 模型路由与链式降级（is_default 兜底） | 自动 + B 增强 | 无/B | 单测 82% |
| A5 | 五层记忆：L1 Schema → L4 注入 → L5 沉淀 | 自动 + B | 无/B | **worker 0%、middleware 0%** |
| A6 | 三组件沙箱 Local：零信任拦截 + 路径穿越 | 自动（已有+属性测试） | 无 | 覆盖高 |
| A7 | 上下文治理：token 预算 P0-P5 分段舍弃 | 自动 + B | 无/B | snapshot 34%、summarizer 49% |
| A8 | 技能系统：37 内置 + 选择/注入/四计数器 | 自动 | 无 | 覆盖高 |
| A9 | 技能自进化 L2/L3（变异→门槛→棘轮回滚） | 自动(mock) + B | 无/B | 覆盖高，真 LLM 链路未验 |
| A10 | 内置工具面：时间/计算/画像/定时任务 | 自动 + 手工 | 无 | builtins 90% |
| A11 | 多 Agent 委派 delegate_to_pi | B | pi CLI | 08-21 手工验证过，未脚本化 |
| A12 | 审计：JSONL 落盘 / 事件序列 / 脱敏 | 自动 | 无 | 已有 |
| A13 | 心跳调度 heartbeat | 自动 | 无 | **11%** |
| A14 | MCP Adapter | B | 外部 MCP server | **22%** |
| A15 | WebUI server + 静态面板 | 自动(TestClient) + GUI | 无/GUI | server 70%、app 100% |
| A16 | 配置：.env / provider / 运行时热生效 | 自动 | 无 | 部分 |
| A17 | 沙箱 Docker 挂载区强制 + 越界拒绝 | C | Docker | 单测 95%+ |
| A18 | WSL2 executor 路径翻译 | C | WSL2 | 单测有 |
| A19 | GUI 桌面窗口（pywebview） | C | 桌面环境 | 未功能验收 |

---

## 三、Part A：本地确定性功能验收（无外部依赖，目标全 PASS）

### A1 CLI 四命令
- `novamind --help` 显示全部子命令；`novamind run` 在无模型配置时给可读报错而非裸 traceback；`novamind doctor` 输出架构健康体检 JSON/表格且不崩；`novamind monitor` 在无审计数据时正常启动空面板。
- 验收：逐条执行，记录退出码与关键输出。

### A2 编排状态机
- 用 `_fakes.FakeLLM` 走完整 `create_agent_app`：预设「先工具后回答」响应序列 → 断言工具真实执行、结果回填、最终回答；迭代上限触发终止；`thread_id` 持久化后恢复上下文。
- 验收：`uv run --no-sync pytest tests/unit/test_state_machine.py tests/integration/test_e2e.py -q` 全绿即视为通过；另加一条「10 轮连续工具调用后正确收敛」的集成用例（若缺）。

### A3 中间件五钩子
- 注册记录型中间件，断言 `before_agent/after_agent/before_model/after_model/wrap_tool_call` 实际触发顺序与参数透传；再验证「加一个中间件不改核心循环」的扩展性声明（新增中间件后原行为不变）。
- 现状：`middlewares/*` 三个记忆中间件 0% ——补最小集成用例：注册后真实跑一轮 agent，断言调用链。

### A5 记忆五层（重点：L4/L5 目前无守卫）
- L1/L2/L3：真实写中文笔记到 Markdown 存储 → BM25 命中 → `store.update` 强化写回后 strength 提升 → 再次检索排序前置。
- L4：跑一轮带记忆注入的 agent，断言 UserMessage 前确实注入了召回的记忆且不碰 system prompt。
- L5 worker（0%）：手动启动 MemoryWorker → 喂入一段对话 → 等待后台沉淀 → 断言新 episodic trace 落库；再验证「够量合并为 semantic」与「旧 trace 标 forgotten 被检索过滤」。
- 验收：每条对应一个可复跑脚本或 pytest（写入 `tests/functional/`）。

### A6 沙箱 Local 零信任
- 真实 shell 执行：白名单内命令成功；元字符（`;` `&&` `$()` 等）、环境变量注入、白名单外命令被拒绝；`D:\foo`→`/mnt/d/foo` 翻译正确；warm pool 取用/归还、idle destroy 触发、跨进程锁互斥。
- 现状：单测+属性测试已厚，功能验收主要是「真实命令层面再确认一次」。

### A7 上下文治理
- 构造超预算对话真实走一遍：externalize → summarize → snapshot → 熔断 P5 的取舍路径，断言被舍弃内容确实不再进入后续模型上下文；budget fraction 边界（0.45 阈值）。
- 现状：snapshot/summarizer/externalizer 34–58%，补功能用例。

### A8 技能系统
- 37 个内置技能全部加载成功（无 error）；选择器按 quality 选出正确技能；一次真实任务中技能被注入并产生可审计的应用记录。

### A10 内置工具
- 时间/计算/画像（含同秒备份不合并）/定时任务（创建→列出→执行→清理）逐一真实调用；`save_user_profile` 保留最近 10 份。

### A12 审计
- 完整 agent run 后审计 JSONL 存在、事件顺序正确（含 tool_call→policy_check→tool_result→ai_message）、脱敏生效（无明文 key）。

### A13 心跳
- 手动启动带 heartbeat 的入口，断言定时 tick 事件产生（当前 11%，补最小功能用例）。

### A16 配置
- `.env` 缺省值生效、显式覆盖生效、非法 provider 报错可读；`NOVAMIND_DATA_ROOT` 跨目录启动数据落同一位置（P3 已修，功能确认）。

---

## 四、Part B：真实 LLM 功能验收（需 key，缺 key 显式跳过）

统一前置：`.env` 配置真实 provider；新建 `tests/functional/` 并用 `@pytest.mark.real_api` 标记，conftest 统一「无 key 或模型不可达 → SkipTest 并写明原因」。

### B1 端到端复杂任务链（复刻 08-08 三用例）
- project_report（13 工具步）/ calc_dashboard / file_organizer 三个真实任务：断言完成、零策略违规、产物落盘正确；记录耗时。
- 通过标准：3/3 完成，策略违规 0。

### B2 链式降级真实生效
- 主 provider 故意配错 key 或指向不可达端点 → 断言自动回退到备用 provider 且 `is_default` 兜底成功；真实窗口大小解析正确。

### B3 技能自进化真实链路
- 真实模型跑一轮：分数跌破阈值 → IVEFocuser 诊断 → LLMMutator 变异 → ScoreDeltaGate 判门槛 →（若退化）GitRatchet 回滚；断言最终技能版本有效且变更可追溯。

### B4 L5 记忆沉淀真实链路
- 真实多轮对话 → worker 调真实 LLM 抽取 episodic → 够量合并 semantic；跨会话重启后检索命中沉淀记忆。

### B5 上下文治理真实
- 超长对话真实裁剪后模型仍能完成收尾回答，且被舍弃内容不可见于输出上下文（通过审计/注入层断言）。

### B6 质量评估与契约检查
- 对 B1–B5 的真实输出跑 TaskQualityJudge（四维加权）与 ResponseContractChecker（从技能文本提取契约），断言评分落在合理区间、契约违规被检出。

### B7 多 Agent 委派（需 pi CLI）
- 复刻 08-21 已验证链路：真实中文多步任务委托给 pi → 三段式结构化回传（What You Did / Success / Gaps）；异常（pi 缺失/超时）走可读报错。
- 通过标准：端到端完成且回传结构完整。

### B8 审计完整性（真实 run）
- 一次真实 agent run 的审计事件可完整重放，能回答「每一步做了什么、调用链是什么、最终回答引用了哪些证据」。

---

## 五、Part C：特殊环境功能验收（环境缺失显式「跳过」）

- **C1 Docker 沙箱**：容器创建 → 写入/重定向强制落在 `/mnt/novamind/user_data/` → 越界拒绝 → 空闲销毁；跨实例并发锁（需 Docker）。
- **C2 WSL2 executor**：`D:\...` 路径翻译到 `/mnt/d/...` 后真实执行一条命令（需 WSL2）。
- **C3 GUI 桌面窗口**：`novamind gui` 启动 → 会话/技能/监控面板可交互 → 后端接口在 TestClient 层已有 70% 守卫，桌面层按手工 checklist 验一遍（需桌面环境）。
- **C4 MCP Adapter**：连接一个真实 MCP server（可用本地 mock MCP 起一个真实进程），断言工具发现与调用往返（需自备 MCP server）。

---

## 六、收尾与报告

1. 汇总 `docs/functional-report.md`：总表 + 每项详情 + 耗时/花费记录（Part B）+ 缺陷清单。
2. 对验收中 FAIL/部分的项：区分「产品缺陷（记缺陷号，进缺陷登记）」与「未接线/设计未完成（如 delegate_to_subagent，明确标注留待 roadmap）」。
3. 可复刻的真实用例（B1 三任务、B7）固化进 `tests/functional/`，列入 README「发布前必跑清单」；条件允许时再评估录回放（L1）把关键路径接进 CI。
4. 验收结论只写「已验证可用 / 部分可用（附清单）」，不写「全部通过」除非真的全过。

## 七、缺陷登记（验收中发现的真实问题记在这里）

| # | 功能 | 现象 | 严重级 | 复现方式 | 归属（框架/环境/未接线） |
|---|---|---|---|---|---|
|  |  |  |  |  |  |

## 八、验收总清单

- [ ] Part A（A1–A16）逐项有结论，全部有证据（命令输出/测试结果/截图）
- [ ] Part B（B1–B8）跑完一轮真 API，耗时花费有记录，缺依赖项显式「跳过」并注明
- [ ] Part C（C1–C4）环境存在则验、缺失则「跳过」
- [ ] `docs/functional-report.md` 产出，结论如实（含 PASS/FAIL/部分/跳过计数）
- [ ] 缺陷登记完整，每个 FAIL 都定位了归属
- [ ] B1/B7 固化进 `tests/functional/` 与发布前必跑清单
