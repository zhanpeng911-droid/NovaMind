# NovaMind WebUI 内部 API（加固 Phase 4/5 后的实际契约）

本文档描述 GUI 后端（`novamind/webui/server.py`）的内部 API。前端
工作台与助手小窗是内置消费者；桌面默认端点只绑定
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

`GET /doctor` 另含 `application` 对象，不改变既有 `ok/counts/findings`：

- `version`：源码取当前 `pyproject.toml` 的项目版本（安装版回退包元数据）；EXE 取打包时嵌入的版本。
- `build_time`：打包时记录的带时区 UTC ISO 时间；源码运行或构建记录缺失时为 `null`，不使用启动时间或文件修改时间代替。
- `mode`：`packaged` 或 `source`。
- `workspace`：当前实际使用的绝对工作区路径，不是 EXE 位置或临时解包目录。

诊断页顶部显示上述信息，时间按本机时区展示并附时区。诊断检查失败时仍保留应用信息；
构建信息不可用则明确显示“未记录”，源码显示“未打包”。该接口只读，不读取凭据或用户文档内容。

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

## 技能开关与 GUI 接线 [已实现，待用户验收]

技能列表中的内置技能简介由前端静态中文翻译表展示，原始技能文件、ID、
`GET /skills` 的 `description` 及模型侧目录不变；不为翻译额外调用模型。

记忆中心读取 `GET /memories?exclude_deleted=true` 时，在分页与计数前排除已删除记录，
因此删除后卡片会退出当前列表。参数默认 `false`，保留原 API 的全状态查看语义；
显式筛选 `state=deleted` 可查看删除标记。标记仍保存在治理库中，阻止后台重新写回，
不会参与模型召回。此变更不删除墓碑、修订审计或其他个人数据。

- `GET /skills` 每项新增 `skill_id`，保留 `enabled`；关闭的技能仍在列表中，
  `count` 仍统计所有 active 版本（不等于已启用数）。
- `PATCH /skills/{skill_id}` 接收 `{"enabled": true}` 或 `{"enabled": false}`，
  必须为 JSON 布尔值，不接受额外字段。成功返回 `{"skill_id":"...","enabled":true}`。
  请求设置目标状态而非翻转，重复同一请求是幂等操作。
- 未知技能返回 404 `skill_not_found`，非法输入返回 422；保存失败返回
  500 `skill_update_failed`，统一错误形状，服务端日志关联 request_id。
- 状态保存至技能 SQLite 库；重复扫描只更新文件元数据，不覆盖已保存的开关。
  不删除技能文件，不改变版本 active 指针。
- GUI Agent 每次调用模型前读取已启用技能目录，通过只读 `load_skill` 工具
  按需加载注册文件；工具也再次校验 active/enabled，不接受任意文件路径。
  不新增独立的模型筛选请求。技能不能授予额外工具权限。
- 关闭后不再列入后续目录，也不能再通过 `load_skill` 加载；已经在执行的
  模型调用不取消，历史已读取的内容不擦除。开关不是文件系统权限开关。
- 前端保存期间禁用重复点击，成功后才更新状态，失败保留原显示并提示；
  网络中断时应重新进入技能页确认实际保存结果。
- 本次依用户要求未运行测试或发起模型调用；使用新版后端需重启应用。

## 并发不变量 [已实测]

以下实测描述是原有门禁记录，不代表本轮新增界面功能已经执行验收。

- 同一 thread 的 run/astream/删除互斥（Agent per-thread 协调器）；不同
  thread 并发重叠；
- 运行中的 thread 状态不参与 LRU 淘汰；
- 取消/异常/生成器提前关闭路径上，`after_agent` 收尾钩子恰好执行一次，
  沙箱 acquire/release 恰好各一次；
- 半轮失败（含断连）不落盘：内存状态回滚到轮次开始前。

## 个人桌面增量接口（2026-10-22，本轮未运行测试）

- `POST /chat` 新增可选 `reasoning`（`auto/quick/low/high/max`，默认 auto）、`client_id`（最长 64 字符）与 `source`（workspace/quick），原输入与 SSE 契约保留。思考参数逐次传递；各供应商映射及限制见 [本轮记录](GUI_PERSONAL_POLISH_2026-10-22.md)。
- `GET /workspace/events` 返回 SSE：先 `snapshot {turns: [...]}`，再 `start/frame/finish/history`。事件含 thread_id、turn_id、client_id；客户端忽略本端重复事件，重连用在途快照恢复。心跳是 SSE 注释；该通道不调用模型，不永久保存聊天副本。
- `POST /workspace/turns/{turn_id}/cancel` 取消对应在途请求；未知/已结束轮次幂等返回 `{"status":"ok"}`。桌面本机安全守卫照常保护写请求，client_id 不是令牌。
- `GET /personal/memory-document?scope=global` 返回 `content/revision/path/scope`；并发文件编辑冲突时还返回 `conflict=true/server_content`，不覆盖文件。
- `PUT /personal/memory-document` 接收 `content`（至多 32000 字符）、64 位十六进制 revision、可选 scope（至多 120 字符），同步原治理条目后返回新文本与版本。每条正文至多 4000 字符；语义非法 422，过期版本 409。不写入候选或墓碑内容。读取可能同步用户直接修改的本地 MEMORY.md；没有外部文件修改时仅更新投影。
- `DELETE /personal/schedules/{task_id}` 真正移除日程及其投递记录，重复删除成功；与 PATCH cancelled 不同。`ScheduleChange` 新增可选 task_date；空 target_time 配合法日期可改为无具体提醒时刻的待办。

### 2026-10-24 后续修复契约

- `/chat` 和 `/quick/actions` 的模型失败保持稳定 error 形状，根据已识别异常可返回 `model_timeout`、`model_connection_failed`、`model_auth_failed`、`model_rate_limited`、`model_unavailable`、`image_input_invalid` 或 `image_model_rejected`。未知异常保留原 generic fallback code；不会把所有带图失败归因为不支持视觉，也不回显异常正文。
- `/workspace/events` 的 snapshot 仍使用 `turns`，每个条目可附 `truncated:true`，或有限终态 `finish:{cancelled,error}`。每轮最多 256 帧及 128 KiB UTF-8 帧预算，保留后缀并明确告知省略；慢监听者队列满时发送校正 snapshot。正常结束后客户端从历史校正，刚好溢出的正常结束也保留明确终态。失败/取消只保留最近 8 轮、60 秒的进程内回放；正常成功轮次不加入该终态缓存。未持久化且已省略的片段不会被宣称已恢复。
- 记忆冲突的 `server_content` 在界面以只读文本展示，保存仍必须通过原有 revision 校验；失败不更新 revision，也不自动覆盖草稿。日程表单将 `target_time` 对应字段称为“事项时间”，预览实际提醒时刻，后端计算与存储契约不变。

本轮回归证据与验证范围见 [迭代记录](ITERATION_POLISH_2026-10-24.md)。

## 个人技能学习（2026-10-25）

实现位于 `learning_routes.py`，前缀 `/personal/learning`，沿用 LocalGuard 和统一 HTTP 错误边界。读取页面、候选、作业不会发起模型调用；请求模型禁止额外字段、非有限金额和无效文本约束。

| 路径 | 主要契约 |
| --- | --- |
| GET/PUT `/settings` | 本地观察、最少样本、留存和冷却；PUT 仅更新设置，不启动付费步骤 |
| GET `/patterns`；GET `/patterns/{id}/sources` | 有界建议与来源预览；来源不是成功证明；预览过滤常见凭据/路径并排除隐藏 reasoning |
| POST `/patterns/{id}/dismiss` | body 带 revision，拒绝后不重复建议 |
| GET/POST `/candidates`；GET/PATCH `/candidates/{id}` | 有界列表、创建/阅读/编辑。来源最多 10 条，正文最多 16000 字符；编辑须 revision 并使旧验证失效 |
| POST `/candidates/{id}/generate` | 用户确认预算后整理一个候选；需要 revision、幂等键和预算，不发布技能 |
| POST `/candidates/{id}/evaluate` | 用户确认预算后比较文本基线和候选；2–5 个不同输入，每个必须有非空 required 约束；不执行工具 |
| POST `/candidates/{id}/publish` | revision/hash/expected_base_id/mode/enable/confirmed；trial 与 verified 区分，静态/行为失败不能用试用绕过 |
| POST `/candidates/{id}/reject` | revision 校验，清理草稿正文并抑制相关重复建议 |
| GET `/jobs`；GET `/jobs/{id}`；POST `/jobs/{id}/cancel` | 状态、spent、reserved、有限调用计量；轮询不重复排队 |
| POST `/jobs/{id}/acknowledge-risk?confirmed=true` | 仅对已结束且存在未知费用的作业确认已核对账单；保留未知预留记录，不恢复旧额度 |
| GET `/usage?thread_id=…`；POST `/feedback` | 实际加载的个人方法与用户反馈；反馈绑定 run_id/skill_id/revision，不以没反馈当成功 |
| POST `/skills/{id}/rollback?expected_active=…`；DELETE `/skills/{id}` | 只回退有效获准版本，保留开关；撤销停止加载并清理应用管理正文，清理失败保留待处理标记 |
| DELETE `/observations?confirmed=true` | 明确确认后清理观察并撤销相关派生方法，不删除原始聊天或普通技能 |

候选的 source_scope 与适用 scope 分开。把项目来源整理为 global 方法必须显式 `global_confirmed:true`；不能混合来源范围后默默全局使用。失效来源、过期候选 revision 或基线返回 409，记录不存在为 404，参数验证为 422，未知内部异常不回显正文。

模型作业需要 `budget_yuan/input_price/output_price/provider/model`，金额单位为人民币、单价为人民币/百万 token；不预填未经核对的价格。首版沿用当前已配置的 OpenAI 兼容供应商地址与凭据，不能通过这个入口切换到另一个供应商类型；没有模型 fallback 或自动重试。环境显式禁止技能进化时不调用模型。usage 不全保留风险预留，实际超预留如实记录并停止继续准入。

verified 只表示记录中的有限文本约束通过；前端展示验证覆盖范围，不能把它理解为真实文件/网络操作或事实核验通过。原始设计与实际验收分别见 [设计](PERSONAL_SKILL_EVOLUTION_DESIGN.md) 和 [实现记录](PERSONAL_SKILL_EVOLUTION_DELIVERY_2026-10-25.md)。
