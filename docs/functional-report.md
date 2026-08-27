# NovaMind 全功能验收报告（R3）

> 验收日期：2026-08-26。环境：Windows + Git Bash + uv；真实 LLM = DeepSeek（`deepseek-v4-flash`，key 探活 HTTP 200）。
> 性质：全功能验收——把 README 宣称的产品能力真实执行一遍，逐项判定可用性。与代码质量测试（561 测试 / 81% 覆盖）互补。
> 配套套件：`tests/functional/`（`real_api` 标记，缺 key 自动跳过，CI 已 `--ignore` 排除，作为「发布前必跑清单」手动执行）。

## 一、结论总表

| # | 功能 | 验收方式 | 结果 | 备注/缺陷号 |
|---|---|---|---|---|
| A1 | CLI 四命令 | 子进程断言 | ✅ PASS | help/doctor --json/monitor --list 全部正常 |
| A2 | 自研编排（10 轮工具收敛 + 迭代上限） | 自动 | ✅ PASS | 10 轮工具调用自然收敛；死循环被上限终止并审计 system_action |
| A3 | 横切中间件五钩子 | 自动 | ✅ PASS | before/after_agent、before/after_model 按序触发；新增中间件不改默认行为 |
| A4 | 模型路由 + 链式降级 | 自动+B | ✅ PASS | 见 B2/B9 |
| A5 | 五层记忆 L1-L5 | 自动(mock)+B | ✅ PASS | 缺陷#1/#2 已修复：L4 检索索引接线、默认运行时已挂载记忆/治理中间件，B10 转绿（见修复记录） |
| A6 | 三组件沙箱 Local | 自动+属性测试 | ✅ PASS | 零信任拦截/路径穿越/白名单/翻译全通 |
| A7 | 上下文治理 P0-P5 | 自动+B | ✅ PASS | governance 中间件真实长对话跑通（B5） |
| A8 | 技能系统 37 内置 | 自动 | ✅ PASS | 37 个 SKILL.md 全加载 |
| A9 | 技能自进化 L2/L3 | mock+B | ✅ PASS | 真实 LLM 一轮进化 35s 跑通（B3），记录落库 |
| A10 | 内置工具面 | 自动+B | ✅ PASS | 时间/计算/画像/定时任务/文件真实组合全通（B14） |
| A11 | 多 Agent 委派 delegate_to_pi | B | ✅ PASS | pi 0.84.2 真实委派返回结构化结果（B7） |
| A12 | 审计 JSONL/脱敏/序列 | 自动+B | ✅ PASS | 真实 run 审计可重放、无密钥泄漏（B8） |
| A13 | 心跳调度 | 自动 | ✅ PASS | 到期任务触发、repeat 计数递减 |
| A14 | MCP Adapter | C | ⏭️ 跳过 | mcp 依赖未安装（环境缺失，缺陷见下） |
| A15 | WebUI server + 面板 | TestClient+GUI | ✅ PASS | server 70% 守卫；桌面层 webview 可导入，交互留手工 |
| A16 | 配置 .env/provider | 自动 | ✅ PASS | 缺省/覆盖/非法 provider 报错可读 |
| A17 | 沙箱 Docker 挂载区强制 | C | ❌ FAIL(环境) | docker CLI 在但 **daemon 未启动**（缺陷#3）；单测层 95%+ 已守卫 |
| A18 | WSL2 executor 路径翻译 | C | ✅ PASS | `D:\foo`→`/mnt/d/foo` 纯函数恒生效；executor 需显式开启（缺陷#4 配置依赖） |
| A19 | GUI 桌面窗口 | C | ⏭️ 跳过 | 无头会话无法交互；webview 导入正常，留手工 checklist |

**计数：PASS 16 / FAIL 1（A17 环境）/ 跳过 2（A14、A19）**

## 二、Part A 细节（无外部依赖，10 项 functional 用例 + 复用既有套件）

- A1：`novamind --help` 含 run/monitor/doctor/gui/config；`doctor --json` 输出 `ok/counts/findings` 退出码 0；`monitor --list` 正常。
- A2：FakeLLM 脚本 10 轮 `agent→tools→agent` 后收敛；死循环在 max_iterations 终止并写 `system_action` 审计。
- A3：录制中间件五钩子均触发且先 before 后 after；加中间件后默认行为不变。
- A5（mock 机制层）：MemoryWorker 抽取→落库→阈值合并 semantic→源标 forgotten→retriever 过滤 全通。
  **[WIRING] 事实核查输出**：默认 `create_agent_app` 不含 MemoryRecall/MemoryConsolidation/ContextGovernance 中间件 → 默认 CLI/GUI 运行时 L4/L5 未接线（缺陷#2）。
- A13：到期任务注入事件总线；repeat_count 递减。

## 三、Part B 细节（真实 LLM，DeepSeek；18 passed + 1 xfailed，总耗时 ~2 分钟/轮，花费极低）

| 用例 | 结果 | 关键证据 |
|---|---|---|
| B1 复杂任务链 ×3 | ✅ | 3/3 完成，0 策略违规，工位产物落盘 |
| B2 降级（瞬时→真；401 不降级） | ✅ | 死端点超时切到真 DeepSeek；401 直接暴露不降级（符合设计） |
| B3 技能自进化真链路 | ✅ | IVE→Mutator→Gate 真实跑通，候选版本+记录落库（35s） |
| B4 L5 真实沉淀 | ✅ | 真实 2 轮对话经 worker 抽取记忆落库（LLM 自判类型） |
| B5 上下文治理真长对话 | ✅ | 治理中间件下长对话完整收尾 |
| B6 TaskQualityJudge 真实 | ✅ | 四维评分落 [0,1] |
| B7 delegate_to_pi 真实 | ✅ | pi 委派返回结构化结果 |
| B8 审计完整重放 | ✅ | 事件序列完整、无密钥泄漏 |
| B9 模型路由真降级+兜底 | ✅ | is_default 恒在链尾，死端点切真 |
| **B10 真实召回注入** | ✅ PASS | 缺陷#1 修复后：encode → 检索命中 → 召回注入生效 |
| B11 摘要 LLM 二次评估 | ✅ | llm_verdict 产出 |
| B12 上下文包影响回答 | ✅ | 回答体现 docs 内容 |
| B13 用户画像进入回答 | ✅ | 画像内容被模型引用 |
| B14 内置工具真实组合 | ✅ | 计算/时间/定时任务/文件全通 |
| B15 token 用量审计 | ✅ | token_usage 落审计且 cost>0、模型名正确 |
| B16 长工具循环真收敛 | ✅ | ≥2 次工具调用后自然收敛，未被上限截断 |

## 四、Part C 细节（特殊环境）

- C1 Docker：**daemon 未启动**（`docker ps` 连不上 npipe）→ 真实容器验收 FAIL（缺陷#3，环境）。镜像/挂载区单测已厚。
- C2 WSL2：`wsl echo` 执行正常；`translate_to_wsl(D:\foo\bar.txt) == /mnt/d/foo/bar.txt` ✅；executor 默认关闭需显式开启（缺陷#4）。
- C3 GUI：webview 可导入；后端接口 TestClient 层已守卫；桌面交互留手工 checklist。
- C4 MCP：mcp 依赖未装 → 显式跳过（缺陷登记）。

## 五、缺陷登记

| # | 严重级 | 功能 | 现象 | 归属 | 复现/证据 |
|---|---|---|---|---|---|
| 1 | 高 | L4 记忆召回 | 启动后 encode 的记忆**永远检索不到**（连精确词 0 命中） | 框架缺陷 | `build_default_provider` 未对 store 做 `_wrap_store` 接线（bootstrap 有、strategy 缺）；B10 xfail 佐证 |
| 2 | 中 | L4/L5 + 上下文治理默认激活 | 默认 CLI/GUI 运行时**未接线**这些中间件（README 宣称的“五层记忆/上下文治理”默认不生效） | 未接线 | create_agent_app middlewares 默认空；main/server 未传；[WIRING] 输出 |
| 3 | 低 | C1 Docker 验收 | docker CLI 在、**daemon 未启动** | 环境 | `docker ps` npipe 连接失败 |
| 4 | 信息 | WSL2 路径翻译 | executor 默认关闭，需 `NOVAMIND_SANDBOX_EXECUTOR=wsl` 显式开启 | 配置依赖/文档 | 纯函数恒生效，包装器默认直传 |
| 5 | 信息 | L5 抽取类型 | 真实 LLM 自判类型（DeepSeek 把偏好归 semantic 非 episodic） | 设计留观 | B4 实测 |

## 六、发布前必跑清单（新增，真实 LLM）

```bash
uv run --no-sync pytest tests/functional -q          # 全功能验收（Part A/B/C）
# 已知项：B10 xfail（缺陷#1）、C1 FAIL（需 docker daemon 启动后复跑）、C4 需装 mcp
```

CI 已 `--ignore=tests/functional`：功能验收属发布前手工门禁，不占日常回归。


---

## 七、修复记录（缺陷 #1 / #2 已修复）

- **缺陷#1（高，L4 检索索引断裂）已修复**：`build_default_provider` 引入幂等的 `_wrap_store`（按 retriever 记 `_wired_retrievers` 集合防重复包装），与 bootstrap 收敛为单一实现；默认路径 encode 后记忆即进索引。回归单测 `TestRecallIndexWiring`（encode→retrieve 命中 / 重复 build 幂等）。B10 由 xfail 转 **PASS**，Part B 19/19 全绿。
- **缺陷#2（中，默认运行时未接线）已修复**：新增 `middlewares/default_stack.py#build_default_middlewares(llm)`（记忆召回 + 沉淀 worker + 上下文治理），接入 `entry/main.py` 与 `webui/server.py` 的默认运行时。内核 `create_agent_app` 保持精简（显式传参才启用）；webui / Part A 既有测试 48 项全部兼容。
- 常规回归：**563 passed**（functional 从 CI 排除），覆盖底线 78 不变；Part B 真实 LLM 19/19。
- 遗留：缺陷#3（docker daemon 未启动，环境）、#4（WSL executor 需显式开启，配置依赖）、#5（worker 抽取类型由 LLM 自判，留观）。
