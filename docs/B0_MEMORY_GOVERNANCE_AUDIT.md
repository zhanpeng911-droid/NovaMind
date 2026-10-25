# B0 记忆链路审计 + 治理最小实现

> 日期：2026-10-19  
> 范围：Phase 0 / B0（只做审计、治理最小 Schema、迁移与反例测试；Memory Center UI 与 /memories API 属 B1）  
> 结论：链路已打通「候选 → 确认/拒绝/禁用/删除 → 召回过滤」，删除有 tombstone 硬屏障，worker 不能复活已删除记忆；历史记忆迁移为 ACTIVE，既有召回不回归。

---

## 1. 审计对象与真实链路

从实际代码追踪一条记忆从「产生」到「进入提示词」的完整路径：

```
MemoryConsolidationMiddleware.aafter_model（每 N 轮）
  → MemoryWorker.submit(MemoryTask)
  → daemon 线程 _extract_and_encode
      → LLM 抽取 JSON: [{content, type, importance}]
      → DefaultMemoryManager.encode
          → id = SHA256(content + "\x00" + type)[:16]
          → MarkdownFileStore.add（traces.md 追加，正文事实源）
          → _wrap_store 包装层 → HybridRetriever.on_trace_added（派生 BM25 索引）
      → （B0 新增）MemoryGovernanceService.propose → 候选记录
  → _maybe_consolidate（episodic ≥ 10）
      → LLM 合并 → DefaultMemoryManager.consolidate
          → 新 semantic trace + 旧 trace 标 forgotten
      → （B0 新增）note_consolidated → 合并产物候选化，旧 ACTIVE 置 DISABLED

MemoryRecallMiddleware.abefore_model（每轮）
  → 最新用户消息作为 query
  → HybridRetriever.retrieve（BM25 + 0.7 相似度 / 0.3 强度）
  → （B0 新增）filter_recallable（只保留 ACTIVE、非 tombstone、非 superseded）
  → 格式化为 [Recalled Memories] → HumanMessage(name="memory_recall", hide_from_ui)
  → 追加到本轮消息（不碰 system prompt，保护缓存）
```

关键事实（代码位置）：

| 环节 | 位置 | 说明 |
|---|---|---|
| 触发 | `novamind/core/middlewares/memory_consolidation_middleware.py` | `aafter_model`，`user_turn_count % N == 0`，非阻塞 submit |
| 抽取 | `novamind/core/memory/worker.py` | daemon 线程；LLM 失败 log + skip，不阻塞主流程 |
| 编码 | `novamind/core/memory/strategies/default/manager.py` | 内容寻址 id；同内容同类型幂等 |
| 正文持久化 | `novamind/core/memory/strategies/default/store.py` | `traces.md` 是正文唯一事实源 |
| 检索索引 | `novamind/core/memory/strategies/default/retriever.py` | 内存 BM25 倒排，`_wrap_store` 保证增量同步（缺陷 #1 修复） |
| 注入 | `novamind/core/middlewares/memory_recall_middleware.py` | per-call HumanMessage；2000 token 预算 |
| 默认装配 | `novamind/core/middlewares/default_stack.py` | CLI/GUI 默认栈（本次新增治理装配） |

---

## 2. 审计发现的治理缺口（实现前）

1. **自动抽取直接生效**：worker 产出的记忆默认就是可召回状态，没有「候选 → 用户确认」环节；模型推断可能变成稳定画像。
2. **删除无屏障**：没有 tombstone/版本屏障；worker 若在处理旧对话，可能在用户删除后重新 encode 同一内容（内容寻址 id 相同，但无删除状态可查）。
3. **禁用/拒绝无状态**：只有 `forgotten`（衰减/合并用），没有「用户主动暂停/拒绝」的语义，无法下一轮立即生效。
4. **冲突无裁决**：两条矛盾偏好可以同时 ACTIVE、同时注入。
5. **来源与信任未分级**：外部网页/选中文本进入对话后，模型抽取的产物与用户明确指令没有区分，存在 prompt-injection → 长期记忆的升级路径。
6. **治理元数据无单一事实源**：没有独立的生命周期索引，状态只能散落在 trace.metadata，难查询、难审计。

---

## 3. 本次最小实现（After）

新增 `novamind/core/memory/governance.py`：

### 3.1 状态机

```
Proposed ──confirm──▶ Active ──disable──▶ Disabled ──enable──▶ Active
   │                    │
   ├──reject──▶ Rejected│
   └──delete──▶ Deleted ◀┘（tombstone 永久屏障）
Active ──supersede(old→new)──▶ Disabled(superseded_by=new)
```

- **Proposed**：worker/模型推断产物；召回层过滤。
- **Active**：已确认或显式用户指令；唯一可召回状态。
- **Rejected**：用户拒绝；worker 不得再次提议同 id。
- **Disabled**：用户暂停或因合并/冲突被替代；可重新启用（未被 tombstone 时）。
- **Deleted**：tombstone 永久屏障 + 正文标 forgotten；worker 在 encode 前即被拒绝。

### 3.2 存储

独立 SQLite 治理索引（`workspace/memory_governance.sqlite3`），正文不复制：

| 表 | 用途 |
|---|---|
| `memory_governance` | 每条记忆的当前状态、分类、来源、来源轮次、信任级别、scope、revision、superseded_by |
| `memory_revision` | 追加式变更审计（from_state → to_state、reason、actor、时间） |
| `memory_tombstone` | 永久删除屏障（memory_id 主键） |

**唯一事实源声明**：
- 正文：`workspace/memory_traces/traces.md`（MarkdownFileStore）；
- 生命周期：`workspace/memory_governance.sqlite3`；
- 检索索引：进程内 BM25（派生结构，可重建）；
- 不新增第二份 JSON/Markdown 正文副本。

### 3.3 接入点

| 文件 | 改动 |
|---|---|
| `novamind/core/config.py` | 新增 `MEMORY_GOVERNANCE_DB_PATH` |
| `novamind/core/memory/worker.py` | 写入前 `blocked_for_worker` 墓碑/拒绝屏障；抽取产物 `propose`（Proposed）；合并产物 `note_consolidated` |
| `novamind/core/middlewares/memory_recall_middleware.py` | 召回后 `filter_recallable`（只允许 ACTIVE / legacy） |
| `novamind/core/middlewares/default_stack.py` | 装配治理服务 + `migrate_legacy`，传给 recall 与 worker |
| `novamind/core/memory/bootstrap.py` | `start_memory_worker(..., governance=None)` 兼容旧调用 |

### 3.4 迁移策略

- 启动时 `migrate_legacy(store.list_all())`：无治理记录的历史记忆 → `ACTIVE`（`trust_level=legacy`，`user_confirmed=True`）；
- 历史 `forgotten` 记忆 → `DISABLED`（不召回，但不 tombstone，避免误伤）；
- 迁移幂等：已有记录不覆盖；
- 结论：升级后既有召回数量不减少（回归测试验证）。

### 3.5 信任分级与 prompt-injection

- `trust_level`：`user` / `conversation` / `untrusted`；
- worker 抽取固定 `conversation` 且 `model_proposed=True` → Proposed；
- `untrusted` 来源即使 `confirm` 也要求 `force=True`（显式用户操作）——外部网页内容不能靠一次自动确认变成长期指令；
- 用户删除/拒绝是最高优先级：治理状态先于衰减/评分。

---

## 4. 反例测试（15 项，全部通过）

`tests/unit/test_memory_governance.py`：

| 测试 | 断言 |
|---|---|
| `test_legacy_trace_migrates_active_and_recallable` | 历史记忆迁移后仍可召回 |
| `test_legacy_migration_is_idempotent` | 重复迁移不重复写 |
| `test_legacy_forgotten_migrates_disabled_not_recalled` | 历史 forgotten 不召回 |
| `test_proposed_not_injected_until_confirmed` | 候选确认前 0 注入，确认后 1 注入 |
| `test_rejected_not_injected` | 拒绝后 0 注入 |
| `test_untrusted_requires_explicit_force` | 不可信来源直接 confirm 抛错，force 才生效 |
| `test_disable_takes_effect_immediately_then_enable` | 禁用下一轮立即 0，启用恢复 |
| `test_delete_tombstone_blocks_resurrection` | 删除后 tombstone + 正文 forgotten |
| `test_worker_cannot_resurrect_tombstoned_content` | worker 拿到同内容也不 re-encode |
| `test_decay_does_not_override_deleted` | 衰减不覆盖删除状态 |
| `test_supersede_only_new_value_recalled` | 冲突只注入新值 |
| `test_revision_conflict_rejected` | 并发覆盖被 revision 检查拒绝 |
| `test_worker_creates_proposed_candidate` | worker 产物是 Proposed，确认后才召回 |
| `test_middleware_filters_disabled` | 召回中间件真实过滤 |
| `test_cross_session_recall` | 跨会话召回不受来源会话限制 |

补充：`tests/unit/test_local_guard.py` 与 `tests/unit/test_single_instance.py` 属 A0，见 ADR。

---

## 5. B0 门禁结论

- [x] 已有记忆召回原回归不退化（全量 741+8 测试通过；迁移后 legacy Active）。
- [x] 能明确证明一条记忆从产生到注入的完整链路（本文第 1 节 + worker/recall 测试）。
- [x] 删除/禁用/拒绝下一轮立即生效。
- [x] worker 在飞任务不能复活已删除/已拒绝记忆。
- [x] 冲突不双注入（supersede）。
- [x] 不可信来源不能自动升级为长期指令。

## 6. 遗留（B1/B2/B3 范围）

1. **Memory Center API/UI**（B1）：`GET/POST /memories/*`、确认/拒绝/编辑/禁用/删除、来源与用途展示。
2. **候选队列与编辑冲突**（B1）：编辑需 `revision` 前置检查；删除需处理备份/缓存。
3. **智能召回与 scope**（B2）：按类别/scope/top-k/预算注入；Quick Action 只读必要偏好。
4. **个性化评测与冲突自动识别**（B3）：30 条偏好 + 30 条诱导 + 10 组跨会话 + 20 组竞态样本。
5. **worker 抽取类型偏差**（既有缺陷 #5）：真实模型可能把偏好归 semantic；治理层已按 Proposed 兜底，但分类仍需评测。
