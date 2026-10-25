# NovaMind VNext 迭代验收报告（A0–A3 / B0–B3）

> 日期：2026-10-19  
> 基线：Phase 0 验收后继续实施，按任务书 §8 排期完成 A1–A3、B1–B3 与综合验收（A0/B0 见 `PHASE0_ACCEPTANCE_REPORT.md`）。  
> 结论：**可运行闭环完成**——桌面快捷入口 + 轻量面板 + Quick Actions + Quick→Agent 续接；记忆候选/确认/编辑/禁用/删除/墓碑/冲突/迁移 + Memory Center；个性化召回与评测样本集；本机 API 安全基线。  
> 质量：**788 passed**，覆盖率 **79.62%**（底线 78），ruff 全绿。

---

## 一、交付范围

### A 线（桌面）

| 阶段 | 交付 | 代码/接口 | 验证 |
|---|---|---|---|
| A1 | 托盘 + 悬浮球 + 全局热键 + Quick Panel + 单实例唤醒 | `novamind/desktop/shell.py`、`win32_shell.py`、`single_instance.py` | `--smoke` 全绿；9 项单测 |
| A1 | 位置/可见性设置持久化 | `workspace/desktop_settings.json` | 单测 roundtrip |
| A2 | 翻译/解释/总结/改写（单次模型、禁工具、限额/超时/取消） | `/quick/actions`、`novamind/webui/quick_actions.py`、`quick.html/js` | 10 项单测 |
| A2 | 划词捕获（best-effort）+ 显式粘贴兜底 | `selection.py`（Ctrl+C + 剪贴板快照/恢复） | 平台路径；失败回退手动粘贴 |
| A3 | Quick → 完整 Agent 续接 | `/quick/escalate` + `?thread=` 前端续接 | 单测 + 前端逻辑 |
| A3 | Quick 审计事件 | `quick.open / selection.capture / quick.request / quick.cancel / quick.escalate` | 元数据审计，不记原文 |

### B 线（记忆）

| 阶段 | 交付 | 验证 |
|---|---|---|
| B0 | 生命周期 + tombstone + revision + 迁移 | 15 项反例单测 |
| B1 | `/memories` 列表/详情/确认/拒绝/编辑/禁用/启用/删除 + `/settings/memory` + Memory Center UI | 10 项 API 单测 |
| B2 | scope/相关性门槛/信任校验/修正优先/偏好快照 | 7 项单测 |
| B3 | 30 偏好 + 30 干扰 + 10 跨会话 + 20 竞态评测 | 确定性 harness 全过 |

---

## 二、B3 评测指标（确定性 harness）

`tests/unit/test_memory_eval.py`（不依赖真实模型）：

| 指标 | 结果 | 目标 |
|---|---|---|
| 明确偏好遵循（期望记忆被召回） | **30/30（100%）** | ≥ 90% |
| 无关/诱导查询误注入 | **0/30** | 尽量低，单独报告 |
| 跨会话召回 | **10/10** | 10/10 |
| 删除/拒绝/禁用/在飞竞态注入 | **0/20** | 0 |
| 竞态场景行为正确 | **20/20** | 20/20 |

实现中修复的真实精度缺陷：BM25 中文单字停用词（如「不」）导致无关查询误命中；已在 `HybridRetriever` 增加中文停用词过滤，误注入从 1/30 降到 0/30。

真实模型偏好遵循率需人工跑（`tests/functional` 真实 API 门禁），本轮未把真实模型分数冒充为确定性结果。

---

## 三、API 面（新增）

| Method | 路径 | 说明 |
|---|---|---|
| POST | `/quick/open` | Quick Panel 打开审计 |
| POST | `/quick/selection` | 选区捕获审计（只记 chars/source，不记原文） |
| POST | `/quick/actions` | SSE 流式翻译/解释/总结/改写；禁工具、20k 字符上限、30s 超时、并发 4、断连取消 |
| POST | `/quick/escalate` | 保存显式授权文本到指定/新会话，返回 `thread_id` + 续接 URL |
| POST | `/shell/wake` | 二次启动唤醒既有实例（Token 校验） |
| GET | `/memories` | 记忆治理视图 + state/category/scope 过滤 + 分页 |
| GET | `/memories/{id}` | 详情 + revision 历史 |
| POST | `/memories/{id}/confirm` | 确认候选；untrusted 需 `force=true` |
| POST | `/memories/{id}/reject` | 拒绝（终端态，worker 不复活） |
| POST | `/memories/{id}/disable` / `enable` | 暂停/恢复；下一轮召回立即生效 |
| PATCH | `/memories/{id}` | 编辑正文/分类/scope；`revision` 前置检查；正文变更生成新版本并禁用旧版 |
| DELETE | `/memories/{id}` | 删除：tombstone + 正文 forgotten |
| GET/PUT | `/settings/memory` | 召回/自动抽取/预算开关 |

---

## 四、安全模型

1. **loopback 不等于安全**：桌面入口启用 `LocalGuardMiddleware`（纯 ASGI，不缓冲 SSE）：
   - Host 白名单（防 DNS rebinding）；
   - Origin 有值时必须是 loopback；
   - 写请求要求 SameSite=Strict Cookie 或 `X-NovaMind-Token`。
2. **记忆信任分级**：`user / conversation / untrusted`；untrusted 未显式确认不注入；worker 产物一律候选。
3. **删除不可复活**：tombstone 永久屏障 + 正文 forgotten + 内容寻址 id 在 worker encode 前拦截；重启后仍有效。
4. **Quick 不越权**：不绑定工具、单次调用、输入/并发/超时限制；审计只记元数据，不落原文。
5. **日志脱敏**：沿用 AuditLogger 的 key/value 脱敏（api_key/token 等 → [REDACTED]）。

---

## 五、测试与质量

| 项 | 值 |
|---|---|
| 常规测试 | **788 passed**（unit + integration，无 functional） |
| 新增/累计新增（Phase 0 起） | B0 15 + A0 8 + B1 10 + B2 7 + A2 10 + A1/A3 9 + B3 3 = 62 项 |
| 覆盖率 | **79.62%**（CI 底线 78） |
| ruff | `novamind entry tests packaging` 全绿 |
| 真实 LLM 功能验收 | 本轮未跑；发布前手动 `pytest tests/functional` |

---

## 六、打包与运行

```powershell
# 完整工作台（默认）
dist\nova-mind-gui.exe

# 桌面快捷助手（托盘 + 悬浮球 + 热键 + Quick Panel）
dist\nova-mind-gui.exe --desktop
# 或源码：
novamind desktop
```

打包配置无需新增 datas：`NovaMind.spec` 的 `collect_submodules("novamind")` 覆盖 `novamind.desktop`，static 目录整体打包（含 `quick.html` / `quick.js` / `workspace.css`）。

### 打包冒烟证据（2026-10-20）

| 项 | 结果 |
|---|---|
| 产物 | `dist/nova-mind-gui.exe`，**50.87 MB**（排除 PySide6 后；首次误打包为 68 MB） |
| 默认 GUI 模式 | `/health` 200；首页含 `data-nav="memory"`；`/quick.html` 200；`/quick.js` 200；`/memories` 返回迁移后的记忆；`/settings/memory` 200 |
| `--desktop` 模式 | `/health` 200；`/quick.html` 200；进程数 2（onefile 引导 + 主进程） |
| 二次启动唤醒 | 第二次启动后进程数仍为 2，未起第二套后端；`/health` 正常；单实例信息文件含 `pid/url/token` |
| PySide6 泄漏修复 | `Analysis-00.toc` 中 PySide6 引用 44 → 1（仅 excludes 字面量）；exe 体积恢复 |

---

## 七、仍需人工/真实环境验证（未冒充完成）

1. **DPI/双屏/全屏遮挡/失焦**：A1 人工矩阵（100%/125%/150%，单/双屏，全屏应用）。
2. **三类外部应用划词**：浏览器、编辑器、PDF 阅读器；失败时手动粘贴兜底。
3. **真实模型偏好遵循率**：确定性 harness 100%，真实模型需按样本集跑一次并记录成本/延迟。
4. **打包 exe 的桌面模式**：启动/托盘/悬浮球/热键/退出无僵尸进程（本轮已完成默认 GUI、`--desktop`、二次唤醒的程序化冒烟；托盘/悬浮球的视觉交互仍需人工确认）。
5. **二次启动唤醒**：已实现 loopback `/shell/wake` + Token；已用打包 exe 双开验证进程数不增加、健康检查正常；仍建议人工确认面板确实被唤起。
6. **录屏与干净安装/卸载**：发布动作，未在本轮完成。

---

## 八、关键设计决策

1. **治理与正文分离**：`traces.md` 存正文；`memory_governance.sqlite3` 存生命周期。迁移把历史记忆归 ACTIVE，保证升级不回归。
2. **候选默认不注入**：模型推断一律 Proposed；显式用户操作才 Active。防止「聊两句就变成长期画像」。
3. **删除是墓碑而非物理删除**：审计可追溯 + worker 不可复活；用户仍可通过 Memory Center 查看已删除记录。
4. **编辑生成新版本**：内容寻址 id 变化 → 新 trace ACTIVE、旧 trace forgotten/DISABLED、revision 历史保留。
5. **Quick 与 Agent 共用 Runtime**：Quick Action 不另起进程/不复制 Agent；升级时只传显式授权文本与会话 ID。
6. **桌面 Shell 选型**：沿用 A0 ADR 的 pywebview + 原生薄壳（PySide6 在当前发布基座不可用且 647MB）。

---

## 九、交互修复（2026-10-20 用户反馈）

| 问题 | 修复 |
|---|---|
| Quick Panel 拖不动 | 启用 pywebview `easy_drag` + `DRAG_REGION_DIRECT_TARGET_ONLY=True`；只有 `.pywebview-drag-region`（标题栏/悬浮球）触发拖拽，textarea/按钮不误触 |
| 悬浮球白色矩形底 | 悬浮球、Quick Panel、功能菜单均改为 `transparent=True` + `shadow=False`，HTML 背景透明；悬浮球只显示圆形球体 |
| 点悬浮球直接开翻译界面 | 改为「点悬浮球 → 功能菜单（翻译/解释/总结/改写/完整工作台）→ 选择动作 → Quick Panel 并预选该动作」 |
| 面板位置固定/贴边 | 打开时贴近悬浮球并夹在屏幕范围内 |
| 二次启动唤醒 | 唤醒后显示功能菜单（而不是直接弹面板） |

验证：`tests/unit/test_desktop_shell.py` + `test_quick_actions.py` 共 19 项通过；打包 exe 重新构建后 `/bubble_menu.html`、`/quick.html`、`/quick.js` 均 200，拖拽区与 `novaMindSelectAction` 已生效。

---

## 十、一句话状态

> VNext 闭环完成：从「托盘/悬浮球唤起 → 划词翻译 → 转入完整 Agent」到「记忆候选 → 确认/编辑/禁用/删除 → 个性化召回」，全部有代码、接口、测试与验收证据；确定性评测 30/30、0 误注入、20/20 竞态通过；788 测试全绿、覆盖率 79.62%。剩余仅为人工/真实模型/发布动作，已在第七节逐项列出。
