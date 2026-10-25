# Phase 0 验收报告（A0 + B0）

> 日期：2026-10-19  
> 范围：严格遵守任务书 §10 —— 本轮只交付 **A0 桌面可行性 Spike** 与 **B0 记忆治理审计**，不抢跑 A1/A2/B1/B2。  
> 结论：**A0 与 B0 门禁全部通过**；全量常规测试 **749 passed**，ruff 全绿；PySide6 在当前发布基座不可用，桌面 Shell 决策为「pywebview + 原生薄壳」。

---

## 一、交付物

| 类型 | 文件 |
|---|---|
| 记忆治理实现 | `novamind/core/memory/governance.py` |
| 接入改动 | `config.py` / `memory/worker.py` / `memory/bootstrap.py` / `middlewares/memory_recall_middleware.py` / `middlewares/default_stack.py` |
| 记忆治理测试 | `tests/unit/test_memory_governance.py`（15 项） |
| 桌面单实例 | `novamind/desktop/single_instance.py` |
| 桌面原生薄壳 | `novamind/desktop/win32_shell.py`（托盘 + 全局热键） |
| 桌面 Spike/冒烟 | `novamind/desktop/spike.py` |
| 本机 API 守卫 | `novamind/webui/local_guard.py`；`app.py` / `server.py` 接线 |
| 安全/单实例测试 | `tests/unit/test_local_guard.py`（6 项）/ `tests/unit/test_single_instance.py`（2 项） |
| B0 审计报告 | `docs/B0_MEMORY_GOVERNANCE_AUDIT.md` |
| A0 ADR | `docs/adr/0001-desktop-shell.md` |

---

## 二、B0 门禁验收

| 门禁 | 证据 | 结果 |
|---|---|---|
| 已有记忆召回不退化 | `migrate_legacy` 将历史记忆归 ACTIVE；`test_legacy_trace_migrates_active_and_recallable` | ✅ |
| 证明一条记忆从产生到注入 | 审计报告 §1 代码级链路 + `test_worker_creates_proposed_candidate` + `test_middleware_filters_disabled` | ✅ |
| 删除/禁用/拒绝下一轮立即生效 | `test_delete_tombstone_blocks_resurrection` / `test_disable_takes_effect_immediately_then_enable` / `test_rejected_not_injected` | ✅ |
| worker 不能复活已删除记忆 | `test_worker_cannot_resurrect_tombstoned_content`（encode 前 tombstone 屏障） | ✅ |
| 冲突不双注入 | `test_supersede_only_new_value_recalled` | ✅ |
| 不可信来源不能自动升级 | `test_untrusted_requires_explicit_force` | ✅ |
| revision 并发覆盖防护 | `test_revision_conflict_rejected` | ✅ |
| 跨会话召回 | `test_cross_session_recall` | ✅ |
| 衰减不覆盖删除 | `test_decay_does_not_override_deleted` | ✅ |

**唯一事实源**：正文 `traces.md`；生命周期 `memory_governance.sqlite3`；BM25 索引为派生结构。未新增正文副本。

---

## 三、A0 门禁验收

### 3.1 实测数据

| 指标 | 方案 A（PySide6 6.12.0） | 方案 B（pywebview + ctypes） |
|---|---|---|
| 依赖体积 | 647.1 MB | 2.1 MB（已有依赖） |
| 导入 | **失败（WinError 127）** | 0.155 s |
| 托盘 | 未进入实测 | ✅ |
| 全局热键 | 未进入实测 | ✅ Ctrl+Shift+Space |
| 置顶无边框窗口 | 未进入实测 | ✅ |
| 主进程 RSS | 未进入实测 | 96.9 MB |
| 单实例 | 未测 | ✅ 二次启动被拒、释放后可重新获取 |
| Host 伪造 | 未测 | ✅ 403 |
| 跨站写请求 | 未测 | ✅ 403 |
| Cookie/Token 授权 | 未测 | ✅ 200 |
| 后端退出端口释放 | 未测 | ✅ |

复现：

```powershell
python -m novamind.desktop.spike --mode pyside --self-test
python -m novamind.desktop.spike --mode pywebview --self-test
python -m novamind.desktop.spike --smoke
```

### 3.2 A0 门禁清单

| 门禁 | 证据 | 结果 |
|---|---|---|
| 启动 → 托盘 → 唤起 → 关闭 → 完全退出 → 再启动 | `--smoke`：单实例、托盘、热键、后端启停、端口释放 | ✅（程序化冒烟） |
| 无僵尸进程 / 重复后端 | `port_released=true`；单实例第二次 acquire 被拒 | ✅ |
| 托盘/热键/置顶 | `tray_added=true`、`hotkey_registered=true`、`window_created=true` | ✅ |
| 本机 API 安全基线 | Host/Origin/Token 三类测试 + 冒烟 | ✅ |
| 技术方案 ADR | `docs/adr/0001-desktop-shell.md` | ✅ |

> 尚未覆盖（明确列为 A1）：真实人工 DPI/双屏/全屏遮挡矩阵、二次启动「唤醒」IPC、Spike 打包 exe 的体积/冷启动、热键冲突 UI。

---

## 四、测试与质量

| 项 | 值 |
|---|---|
| 常规测试（unit + integration） | **749 passed** |
| 新增测试 | 23 项（B0 15 + A0 8） |
| 总覆盖率 | **79.85%**（CI 底线 78 通过；新增桌面 Spike/win32 薄壳属 UI 路径，由 A0 冒烟实测而非 pytest 覆盖） |
| ruff | 全绿 |
| CI 覆盖率底线 | 78（沿用） |
| 功能验收（真实 LLM） | 本轮未跑（无新增真实模型链路；发布前手动执行） |

---

## 五、变更边界与风险

**已做**

- 记忆：候选/确认/拒绝/禁用/删除/冲突/迁移/召回过滤；tombstone 版本屏障；信任分级。
- 桌面：单实例、托盘、全局热键、置顶窗口 Spike、本机 API 守卫。

**未做（按任务书不抢跑）**

- 不推倒现有五层记忆，不新建向量记忆库；
- 不做持续截屏/剪贴板/键盘监听；
- 不做自动桌面操控；
- 不实现 Memory Center UI/API（B1）；
- 不做全平台适配。

**风险**

1. PySide6 结论受当前 Anaconda 基座 venv 影响，需在干净 CPython venv 复核（ADR 已记录）。
2. WebView2 子进程内存未计入 96.9 MB，A1 需按进程树汇总。
3. 默认栈每次构建都会创建治理库并迁移；当前幂等，但 A1 应改为进程级单例以避免重复 IO。
4. B1 的编辑/删除 API 必须复用 `revision` 前置检查，否则并发覆盖会绕过治理。

---

## 六、后续精确任务清单

### A1 — 悬浮球与轻量窗口（桌面线）

1. 人工矩阵：单/双屏 × 100%/125%/150% DPI × 全屏应用 × 最小化/休眠唤醒；记录坐标、焦点、遮挡。
2. 单实例唤醒：命名管道 `\\.\pipe\novamind-<user>` 或 loopback `/shell/wake`；二次启动唤醒既有实例后退出。
3. exe 打包：Spike 入口并入 `NovaMind.spec`；实测单文件体积、冷启动、托盘图标、退出无僵尸进程。
4. 热键冲突：注册失败提示 + 重设；持久化设置。
5. 焦点行为：浮窗默认不抢焦点；`Esc` 收起；重复热键不重复开窗。
6. 验收：上述人工矩阵通过 + 打包实验数据 + 无重复后端。

### B1 — Memory Center（记忆线）

1. API：`GET /memories`、`GET /memories/{id}`、`POST /confirm|reject|disable|enable`、`PATCH`（revision 前置）、`DELETE`。
2. UI：内容/类别/来源/时间/状态/用途；停止自动抽取开关；导出/批量清理。
3. 删除彻底性：主存储 + 候选队列 + 缓存 + 可控备份；刷新/重启不复活。
4. 审计：每次治理操作落 append-only revision；API 只允许本机会话。
5. 验收：UI 操作后下一轮召回立即生效；拒绝候选不进入 prompt；删除后无复活。

### B2 — 个性化召回

1. 召回加 `state=active`、scope、来源校验、相关性门槛、top-k/预算。
2. 区分通用偏好与任务上下文；Quick Action 只读必要偏好。
3. 用户修正优先于旧推断；负反馈不被高频旧记忆压过。
4. 验收：相关场景应用、无关场景不滥用、修改后下一轮生效、成本可观测。

### B3 — 评测与安全

1. 样本：30 条明确偏好/纠正 + 30 条诱导 + 10 组跨会话 + 20 组竞态。
2. 指标：删除/禁用后注入次数 = 0；偏好遵循率 ≥ 90%；纠正后下一轮生效 100%。
3. Prompt Injection 信任分级：外部内容不得写成长期指令。
4. 验收：反例全过 + 成本/延迟基线。

---

## 七、一句话状态

> Phase 0 完成：记忆从「自动写入即可召回」升级为「候选 → 确认/拒绝/禁用/删除 → 召回过滤」，删除有 tombstone 屏障；桌面侧完成可运行 Spike，实测选定「pywebview + 原生薄壳」，本机 API 安全基线与单实例冒烟全绿。全量 749 测试通过，未触碰 A1/B1 范围。
