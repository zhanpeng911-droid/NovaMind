# NovaMind WebUI 内部 API（加固 Phase 4/5 后的实际契约）

本文档描述 GUI 后端（`novamind/webui/server.py`）的内部 API。前端
`novamind/webui/static/index.html` 是唯一被支持的消费者；所有端点只绑定
loopback，不对网络暴露。

## 能力状态标记

本文与 `sandbox-policy.md`、`tool-contracts.md`、`runtime-overview.md`
一致地使用三种状态标记能力：

- **[已实测]** —— 有自动化测试 + 真实环境 smoke 证明；
- **[已接线]** —— 默认链路已接入并有测试覆盖，但未做真实环境 smoke；
- **[组件存在]** —— 代码与契约存在，未接入默认链路。

## 网络边界 [已实测]

- `run_gui()` 与 `_start_server()` 都调用 `_require_loopback_host()`；
- 仅允许 `127.0.0.1` / `localhost` / `::1`；`0.0.0.0`、`::`、LAN IP 与
  任意 hostname 在创建 uvicorn server 前被 `ValueError` 拒绝；
- IPv6 URL 自动方括号化（`http://[::1]:8765`）。

## 生命周期 [已实测]

FastAPI lifespan 持有 `WebRuntime`：

- 共享 Agent / ConversationStore / skill store（懒加载 + 初始化锁）；
- 容量 semaphore（`NOVAMIND_WEB_MAX_MODELS`，默认 4）：/chat 先取许可，
  再进入 Agent；同 thread 的正确性由 Agent per-thread 协调器保证；
- active task registry：shutdown 时有界等待（`NOVAMIND_WEB_SHUTDOWN_TIMEOUT`，
  默认 5s）后取消在飞任务；
- 关闭顺序：skill store → memory worker/provider → Agent（含 owned 沙箱
  provider）→ history store。

## 请求边界 [已实测]

- 请求体上限 `NOVAMIND_WEB_BODY_LIMIT`（默认 1MB）；中间件同时校验
  `Content-Length` 头与实际累计字节（覆盖 chunked / 伪造头）→ 413；
- `/chat` 只接受 `application/json` → 其他 Content-Type 返回 415。

## 统一错误形状 [已实测]

所有 4xx/5xx 返回：

```json
{"error": {"code": "invalid_cursor", "message": "...", "request_id": "..."}}
```

| 状态码 | code | 场景 |
|---|---|---|
| 400 | `invalid_cursor` | 游标非法（版本/kind/字段类型/越界/文件截断） |
| 413 | `body_too_large` | 请求体超限 |
| 415 | `unsupported_media_type` | /chat 非 JSON |
| 422 | `validation_error` | 字段验证失败、非法 limit |
| 500 | `internal_error` / `http_error` | 未处理异常、存储故障（traceback 只留服务端日志） |
| 500 | `session_delete_failed` | DELETE /sessions/{id} 实际删除失败（同一 request_id 写日志并返回） |

说明：sessions/history/skills 的存储故障返回结构化 5xx（不伪装 200 空数据）；
未知 thread 本身不抛错，仍返回 200 空数组。`/doctor` 的执行失败保持 200 +
结构化 fallback 报告（`ok:false` + `doctor_failed` finding），诊断语义不变。
DELETE 只对**实际删除失败**返回 500；删除不存在的会话幂等 200
`{"status":"ok"}`。前端收到成功响应并确认 `status==='ok'` 后才移除列表。

成功响应经 `response_model` 声明（`exclude_none`）；旧顶层字段全部保留，
分页字段只新增不改名。未知 history/monitor 会话返回 200 空数组；删除未知
会话幂等返回 `{"status": "ok"}`。

## 分页游标 [已实测]

- 格式：带版本的 base64url JSON：`{"v":"v1","k":"<kind>","d":{...}}`；
- 严格校验：kind 必须匹配（防跨端点复用）、字段名精确匹配、类型严格
  （bool 不算 int）、长度 ≤ 512 字符；
- `limit` 缺省时各端点保持旧行为（返回全量，响应不含 `pagination`）；
  **例外：`/monitor/events` 省略 limit 时默认 200**（自收尾修复起不再返回
  全量，这是明确的响应语义变更）。

| 端点 | kind | 游标字段 | 语义 |
|---|---|---|---|
| `/sessions` | `sessions` | `last_id`,`thread_id` | `(MAX(id), thread_id)` 逆序，稳定 |
| `/history/{tid}` | `history` | `before_id` | 原始行 id（含 tool 行），tool-only 页可推进 |
| `/monitor/sessions` | `monitor_sessions` | `mtime_ns`,`thread_id` | 逆序，相同 mtime 稳定 |
| `/monitor/events/{tid}` | `monitor_events_v2` | `offset`,`discarding` | 字节偏移 tail-follow（见下） |
| `/skills` | `skills` | `name`,`skill_id` | `(lower(name), skill_id)` 升序；`count` 恒为全量 active 数 |

`/monitor/events` 分页（tail-follow，**有界读取**）：
- 默认 limit=200，上限 1000，非法 limit → 422；
- 无 cursor：从文件尾读最近 limit 条完整行；`next_cursor` 指向尾行起点
  （EOF 与空页也返回），供后续增量读取；
- 有 cursor（v2：`offset` + `discarding` 状态）：只读其后新增行，单请求
  扫描上限 2 MiB、单行上限 256 KiB；`has_more` 表示本次文件快照内还有
  未处理数据，`next_cursor` 在 EOF 仍返回（前端据此显示"检查更新"）；
- 超长行/坏 JSON/非对象 JSON 跳过并记录可控日志；超长整行不进内存；
- 文件截断（offset 越界）→ 400 `invalid_cursor`，前端清空旧游标刷新
  第一页；旧 v1 monitor cursor 兼容解码为普通行边界起点；
- EOF 无换行的未完成尾行视为未完成：保留其起点，追加日志后再解析。

## SSE 事件与断连语义 [已实测]

`POST /chat` 返回 `text/event-stream`，帧类型：`thread` / `tool` / `text` /
`limit` / `error` / `done`。`error` 帧形状（前端把错误说明保存为消息
`note`，重绘/切换会话后仍可见）：
`{"type":"error","code":"internal_error","message":"<稳定文案>","request_id":"..."}`
——不含 provider/路径等内部细节，完整 traceback 只留服务端日志。

- `done` 只在正常完成时发送；客户端断连（取消）不发 `done`；
- 普通运行错误：先发 `error`（兼容旧 `message` 字段），再发 `done`；
- 断连即取消本轮：服务端显式处理并 re-raise `CancelledError`，关闭下层
  Agent 流 —— 半轮状态回滚（不残留 user/assistant 半截消息），沙箱等
  资源恰好释放一次；
- 前端按会话隔离：每个会话独立 `abortController` 与请求 token，晚到的
  旧 token 帧直接丢弃。

## 并发不变量 [已实测]

- 同一 thread 的 run/astream/删除互斥（Agent per-thread 协调器）；不同
  thread 并发重叠；
- 运行中的 thread 状态不参与 LRU 淘汰；
- 取消/异常/生成器提前关闭路径上，`after_agent` 收尾钩子恰好执行一次，
  沙箱 acquire/release 恰好各一次；
- 半轮失败（含断连）不落盘：内存状态回滚到轮次开始前。
