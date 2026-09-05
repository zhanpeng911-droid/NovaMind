# 收尾修复设计：事务一致性、前端终态与监控增量读取

状态：待实施。基线：`df8aa03`。本文件是设计方案，不代表以下能力已经实现。

## 1. 范围和验收目标

本次针对复审确认的四项问题及一项测试缺口，按五个实施阶段交付。

| 问题 | 目标行为 | 主要落点 |
|---|---|---|
| 摘要写入失败后消息仍留在数据库 | 一轮消息和摘要一起提交或一起回滚 | ConversationStore、Agent 持久化 |
| SSE error 只写 DOM，重绘丢失 | error → done 后消息状态仍有错误说明 | 前端 send/renderMessages |
| DELETE 失败返回 200，前端移除会话 | 失败返回结构化 500，列表与内存保留 | DELETE、Agent clear、前端删除 |
| 监控增量先读完整文件、EOF 丢 cursor | 每次读取有字节上限，EOF 仍可继续查询更新 | 日志读取器、monitor API、前端缓存 |
| metadata 回归测试没有执行修改 hook | 先证明嵌套对象被修改，再验证取消后还原 | Agent 并发测试 |

执行顺序：Phase 0 证据和契约 → Phase 1 事务 → Phase 2 前端终态/删除 → Phase 3 监控 → Phase 4 整体验收。每阶段可独立提交；本文不授权自动推送或发布。

## 2. Phase 0：已核实的接口和事实

以下行号按基线记录，修改后以符号名定位。

| 现有接口或实现 | 事实与可复用位置 |
|---|---|
| `ConversationStore.save_messages(thread_id, messages) -> None` | `novamind/core/state_machine.py:165`，锁、参数化 executemany、单连接 commit/finally close 可复用 |
| `ConversationStore.save_summary(thread_id, summary) -> None` | 同文件 `:188`，另开连接并单独提交摘要 |
| `NovaMindAgent._persist_state(thread_id, state) -> None` | 同文件 `:615`，消息提交后更新计数/清 pending，再保存摘要 |
| `NovaMindAgent.aclear_conversation(thread_id) -> None` | 同文件 `:978`，已有 per-thread 锁，但先清内存再删数据库 |
| `_error_response(status, code, message)` | `novamind/webui/server.py`，统一 ErrorResponse JSON 工厂 |
| `fetchJson(url, options)`、`deleteSession(id, e)` | `novamind/webui/static/index.html`，fetchJson 只把非 2xx 视为失败 |
| `send()` 的 SSE error 分支 | 同文件 `:1013`，没有给 errorText 赋值；finally 已支持 note |
| `monitor_events(thread_id, limit=None, cursor=None)` | `novamind/webui/server.py:482`，现有模式是 tail-follow |
| `encode_cursor(kind, data)` / `decode_cursor(raw, kind, fields)` | `novamind/webui/api_models.py`，v1 严格校验字段集合；不能直接向现有 cursor 塞额外字段 |

已复现的事务缺陷：让 `save_summary()` 抛错，运行结束后内存消息为 `[]`，但 `load_messages()` 返回 `['user', 'reply']`；清空缓存重新加载后失败轮次重新出现。

文档参考：`docs/session-model.md` 的跨重启恢复契约；`docs/webui-api.md` 的 SSE、错误和 tail-follow 契约；原始 `docs/NEXT_HARDENING_IMPLEMENTATION_PLAN.md` 的验收要求。以代码和可复现行为为现状依据。

### 实施前的测试规则

- 用 asyncio.Event 协调竞态，不用固定 sleep 猜测是否已经执行到目标阶段。
- 故障注入发生在真实写入事务内部；必须检查数据库和新 Agent 重载结果。
- 前端测试要运行错误帧处理和状态更新；源码包含某个字符串只能作为辅助检查。
- 先运行当前目标测试，记录基线；每阶段新增测试应能在未修复实现上失败。

## 3. Phase 1：一轮消息和摘要的原子提交

### 3.1 存储接口

拟新增，不是现有方法：

```python
def save_turn(
    self,
    thread_id: str,
    messages: list[BaseMessage],
    *,
    summary: str | None = None,
) -> None:
    ...
```

契约：`summary=None` 表示不更新摘要；字符串（包括空字符串）表示保存该值。允许只更新摘要或只追加消息。

复用 `save_messages()` 的锁、序列化和参数化 SQL 模式：

1. 准备序列化参数，在获得连接后显式开启事务。
2. 在同一连接执行消息 INSERT 和摘要 UPSERT。
3. 两项均成功后执行唯一一次 commit。
4. 失败时 rollback，保留原异常，finally 关闭连接。
5. 不在持有非重入锁时再调用现有 save_messages/save_summary，避免嵌套获取同一把锁。

现有 `save_message/save_messages/save_summary` 公共签名保留；可委托新事务原语或共享接收连接的私有 SQL helper。数据库表结构不变。

### 3.2 Agent 的提交顺序

`_persist_state()` 获取本轮 pending 消息副本和待保存摘要，一次调用 `save_turn()`。只有成功返回之后才推进 `_persisted_counts` 和清空 pending。

沿用当前 after-hook 恰好一次的正常路径顺序：执行节点 → after-hook → save_turn → 更新持久计数。失败时恢复内存快照；事务自身保证数据库没有半轮残留。`run` 和 `astream` 使用同一提交 helper。

提交后的故障必须与提交前失败区分：不要编写“先调用成功的 save_turn，再人为抛错，就假定数据库已回滚”的测试。真实 commit 成功之后不能靠恢复 Python 字典撤销提交。本阶段不引入跨系统事务，也不承诺撤销工具外部副作用。

### 3.3 测试修复

修改 `tests/unit/test_state_machine.py` 和 `tests/unit/test_agent_concurrency.py`：

- 在临时 SQLite 库中安装仅用于测试的摘要写入失败触发器，或在同连接摘要 SQL helper 注入失败，确认消息 INSERT 已被执行但事务未提交。
- 失败后断言：消息和摘要与轮次前相同、pending 和 counts 恢复、新 Agent 重载结果相同。
- 去掉故障后重试一次，消息恰好新增一轮，没有重复或遗漏。
- 同时覆盖 run、astream、仅消息、仅摘要、空摘要，以及 after-hook 抛错时尚未发生写库。
- 修正 nested metadata 测试：预置 `governance` 嵌套值；测试节点实际 dispatch `before_model`，hook 原地修改后设置 Event，节点阻塞等待。测试等待 Event 后先断言值已变化，再取消，最后断言还原。

### 验收及防误改

- [ ] 摘要 SQL 失败后数据库没有本轮新消息。
- [ ] 重新实例化 Agent 后也没有失败轮次。
- [ ] 现有存储公共方法行为不变。
- [ ] 新 nested metadata 测试能令旧浅拷贝版本失败。
- 不用补偿 DELETE 猜测哪几行属于失败轮次；不修改旧消息、表结构或全量恢复接口。

## 4. Phase 2：SSE 终态与删除失败处理

### 4.1 SSE 错误保存

复用现有 `send()` 的 errorText 和最终 `{role, content, tools, note}` 结构。

- 收到 SSE `error` 时先给 errorText 赋稳定展示文案，再更新当前可见 DOM。
- `done` 仅结束本轮，不清除已经保存的错误。
- `finally` 恰好追加一次结果消息；纯错误也有 note，不要求存在 text/tool 才保存。
- 网络异常和用户停止沿用同一终态入口；若已有明确服务端错误，后续连接关闭不要把它覆盖成无意义通用文案。
- note 是当前页面的会话状态，支持切换/重绘恢复；不宣称已经持久化到 SQLite、刷新页面后仍在。

建议将“处理事件→更新本轮状态→构建最终消息”提取为小型纯 JS helper，供 send 和测试共用。只提取必要逻辑，不重写整页。

### 4.2 DELETE 的服务端与客户端契约

成功及删除不存在的 thread 保持 200 `{"status":"ok"}`。

实际删除失败返回 HTTP 500：

```json
{
  "error": {
    "code": "session_delete_failed",
    "message": "删除会话失败，请稍后重试",
    "request_id": "..."
  }
}
```

复用 `_error_response()` 和 `_error_responses(500)`；同一个 request_id 写入异常日志并返回响应。不要通过现有 HTTPException handler 把明确 code 转成通用 http_error。

`aclear_conversation()` 在原 per-thread 锁内先完成数据库删除，成功后再移除 `_states/_persisted_counts`。同步 clear 也保持同样顺序；失败时内存和数据库都保留旧会话。

前端在收到成功响应并确认 `data.status === 'ok'` 后再移除列表和会话状态；遇到非 2xx、无效响应或遗留 200 error 均显示失败并保留。取消聊天后可以请求 DELETE，等待同 thread 协调器完成互斥。

### 测试与验收

文件：`tests/integration/test_webui.py`、`tests/integration/test_webui_endpoints.py`、`tests/unit/test_agent_concurrency.py`；新增执行型前端测试文件和必要 helper。

- [ ] 输入 `thread → error → done`，最终消息有 note；切换并重绘后仍显示。
- [ ] `text → error → done` 保留部分文本和错误，不重复追加气泡。
- [ ] abort 和网络错误都有稳定终态；正常轮次无错误 note。
- [ ] Agent 已创建、未创建两条 DELETE 路径均覆盖数据库故障。
- [ ] 删除失败：500、稳定 code、无原始异常；服务端缓存仍在，前端列表仍在。
- [ ] 删除不存在会话仍成功；同 thread 删除仍等待运行结束。

前端测试可用 Node 内置测试运行器验证共用 helper，再用浏览器 mock SSE/fetch 验证 DOM；不需要调用真实 LLM来复现这些确定性路径。浏览器环境不可用时明确记录剩余验收项。

## 5. Phase 3：真正有界的监控读取与长期有效游标

### 5.1 保留 tail-follow 的含义

本阶段沿用已落地的“最新页＋加载更新”，不把向后翻历史和向前读取新增混进同一个 cursor。更早历史不是本阶段交付项，更新原总方案中的完成状态，明确这一差异。

仅 monitor events 调整为：默认 limit=200，上限1000，非法 limit 返回422。省略 limit 不再返回全量，这是明确的响应语义变更；保留顶层 events 和 pagination 字段，并同步修正文档和旧契约测试。其他集合端点不在本阶段改默认语义。

区分两个字段：

- `has_more`：本次文件快照中是否还有尚未处理的数据，不等于未来不会再有日志。
- `next_cursor`：下次增量读取的起点。文件到 EOF 时也返回，空页也返回。

例如读取完当前数据：

```json
{
  "events": [],
  "pagination": {
    "limit": 200,
    "count": 0,
    "has_more": false,
    "next_cursor": "<当前位置的有效游标>"
  }
}
```

### 5.2 提取日志读取器

拟新增 `novamind/webui/event_reader.py`，承载二进制 JSONL 扫描。endpoint 只负责校验、通过 to_thread 调读取器、构造响应。

建议参数：块大小8 KiB，单行上限256 KiB，单请求扫描上限2 MiB；均为命名常量和待实施默认值。结果可少于 limit，字节预算优先于填满条数。

增量读取：

1. 捕获文件大小作为本次扫描上界，验证 offset 非负且不超过文件大小。
2. 使用有界 `read(size)`，同时限制累计扫描字节；完整行解析成 dict 后才进入 events。
3. 达到事件数或扫描预算就返回；cursor 按实际消费字节推进，使用二进制位置，不使用 `len(line)+1` 猜测换行长度。
4. 同时覆盖 LF、CRLF、UTF-8 跨块字符。EOF 没有换行的尾行视为未完成，不提前丢掉，下一次追加后再解析。
5. 坏 JSON、非对象 JSON、过长行跳过并记录可控日志；不得把超长整行读进内存。

为了让超长行在2 MiB预算下也能持续向前推进，拟为 monitor 单独定义 v2 cursor，携带 offset 和“正在丢弃过长行”的状态。达到预算时保存该状态，下次跳到换行后恢复解析。旧 v1 monitor cursor 继续解码为普通行边界起点；其他端点仍使用现有 v1 codec。显式为 v2 写 decoder，不放宽全局字段校验。

首次最新页：从文件尾分块向前扫描，累计换行数；只有从文件中间起读时才丢弃头部不完整行。尾部未完成行保留其起点作为下一次增量 cursor。预算内少于 limit 也可以返回，不能为了凑满页无限向前扫描。若超长未完成尾行已超过单行上限，cursor 使用上述 discard 状态保证后续进展。

文件截断导致 offset 越界返回400 invalid_cursor；前端清空旧 cursor并重载第一页。相同路径文件被替换为更大文件的识别需要文件世代标识，本阶段不宣称能可靠检测所有轮转方式。

### 5.3 前端更新行为

- has_more=true 时显示“继续加载”；false 时仍保留“检查更新”。不因到 EOF 隐藏后续更新入口。
- 空增量页只更新 cursor，不清空已有事件或重复加载最新页。
- 每次请求捕获 thread_id 和请求 token；切换会话后丢弃旧响应，避免旧日志追加到新会话缓存。
- 防止同一监控页同时发起两次增量请求。
- 长时间查看时限制浏览器缓存，建议保留最新2000条，并提示更早记录仍在日志文件中。

### 测试与验收

文件：新增 `tests/unit/test_webui_event_reader.py`，扩展 `tests/integration/test_webui_phase5.py` 和前端执行型测试。

- [ ] 首次尾页 → 读到EOF → 无新增空页 → 追加日志 → 同一游标读到新事件。
- [ ] 每次读取均有限 size，累计扫描量不超过预算；用包装文件对象统计实际读取字节，禁止 `read(-1)`。
- [ ] 大量追加后 limit=1 仍不会读取全部后缀。
- [ ] 跨块、LF/CRLF、UTF-8、未完成尾行续写均不重复不丢失。
- [ ] 坏 JSON、非对象 JSON、超长完整/未完成行不导致内存无界或 cursor 卡死。
- [ ] 负 offset、越界、截断、错误版本、跨端点 cursor 返回明确错误。
- [ ] v1兼容读取、v2 discard恢复有独立测试。
- [ ] 切换会话不会串日志，重复点击不重复追加，EOF仍能检查更新。

防误改：不保留 monitor events 的无 limit 全量读取入口；不把 cursor=None 当 EOF；不把有限输出条数等同于有限读取内存。

## 6. Phase 4：最终门禁与文档核对

按 `.github/workflows/ci.yml` 执行常规测试和现有门禁：

```powershell
uv run --no-sync pytest tests -q --tb=short --ignore=tests/functional --cov=novamind --cov-report=term-missing --cov-report=xml --cov-fail-under=78
uv run --no-sync ruff check novamind entry tests
uv run --no-sync mypy novamind/core/policy.py novamind/core/token_tracker.py novamind/core/event_bus.py novamind/core/task_store.py novamind/core/skill/types.py novamind/core/sandbox/types.py novamind/core/middlewares/sandbox_middleware.py novamind/webui/api_models.py novamind/webui/runtime.py novamind/core/context.py
uv run --no-sync pytest tests --collect-only -q
git diff --check
```

新增 JS 行为测试接入 CI；如果新增 reader/helper 不在现有类型检查范围，单独运行针对性检查并记录，不能把10文件 mypy 通过写成全项目类型检查通过。

更新：`docs/session-model.md`、`docs/webui-api.md`、`docs/MASTER_TEST_REPORT.md`、原总方案，以及 README 实测计数。明确区分常规测试、functional收集数和浏览器实际执行结果。

最终验收凭据：

1. 摘要写入失败时数据库回滚及跨实例重载的测试输出。
2. error→done、删除500时的前端执行测试或浏览器验收。
3. monitor读取字节统计、EOF后追加读取和超长行的测试输出。
4. 新 nested metadata 测试先修改再取消的断言。
5. 门禁结果及明确列出的尚未验证环境。

## 7. 提交和回滚建议

- 第一个提交：原子 save_turn、Agent调用和事务/metadata回归。
- 第二个提交：SSE note、DELETE失败契约、删除顺序及行为测试。
- 第三个提交：有界日志读取器、游标兼容、监控前端与测试。
- 第四个提交：文档和最终验收记录；不预写通过数量。

每个实现提交都应自带相应契约文档修订。无数据库破坏性迁移。monitor游标和前后端一起交付/回退；回退后新v2游标不再可读时，前端通过400刷新第一页恢复。

本方案完成的判定依据是失败路径和恢复行为测试，而不是提交数量或一轮正常LLM聊天通过。
