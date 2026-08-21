# NovaMind 测试与排障日志（test-log）

> 按时间倒序记录真实环境下的测试、排障与修复。每条记录含：现象 → 根因 → 修复 → 验证。

---

## 2026-08-21：多 Agent（delegate_to_pi）链路打通 + GUI 修复

### 背景接线（本轮新增功能）

- `novamind/core/multiagent/bootstrap.py`：pi CLI 检测（`NOVAMIND_PI_COMMAND` 覆盖 / PATH 探测）→ 生成 `delegate_to_pi` 工具；`NOVAMIND_PI_PROVIDER`/`NOVAMIND_PI_MODEL` 透传 provider/model。
- `agent.py`：默认工具面（tools=None）追加委派工具 + 自动挂 OrchestrationMiddleware（共享沙箱透传 + 打点）。
- `policy.py` + `harness/policies.json`：新增 `delegate_tool_prefixes: ["delegate_to_"]` 前缀放行（动态工具名无法枚举；删前缀即整体关闭委派）。
- pi 侧配置：`~/.pi/agent/models.json` 声明自定义 provider `novamind`（baseUrl=DeepSeek 官方端点，key 用 `$OPENAI_API_KEY` 环境变量插值，不落盘）；`.env` 补 `NOVAMIND_PI_PROVIDER=novamind` / `NOVAMIND_PI_MODEL=deepseek-v4-flash`。

### 六轮故障排障记录（表象 → 根因 → 修复）

| # | 表象（GUI 委派报错） | 真实根因 | 修复 |
| --- | --- | --- | --- |
| 1 | `pi command not found: pi` | Windows npm 全局包只生成 `pi.cmd` shim 无 `pi.exe`；`shutil.which` 找到了但 `subprocess` 无扩展名只按 `.exe` 查找 → FileNotFoundError | bootstrap 返回 which 解析的完整路径（含扩展名） |
| 2 | `TypeError: webidl.util.markAsUncloneable is not a function`（undici 崩溃） | pi 0.84.2 要求 node >= 22.19.0，本机 20.18.1（npm 装包只警告不拦截） | 升级 Node v24.19.0 LTS（绿色解压版换 PATH），实测 pi `--help` 正常 |
| 3 | `'node' 不是内部或外部命令` | exe 从旧终端启动，继承启动时刻冻结的陈旧 PATH（指向已删的旧 node 目录） | 注册表当前 PATH（Machine+User）重建环境后启动 exe |
| 4 | `Request timed out` / `pi exited with code 3221225786`（0xC000013A，弹黑窗） | **多行 prompt 穿过 npm `.cmd` shim 时被 cmd.exe 按换行撕碎执行**（第二行被当新命令；单行测试不炸是假象） | bootstrap 解析 shim 提取 JS 入口，改 `node.exe + cli.js` 直调绕过 cmd.exe；`CREATE_NO_WINDOW` 顺带消除黑窗 |
| 5 | （隐藏雷）pi 0.84.2 不认 `--sandbox-url` | 实测 `Error: Unknown option: --sandbox-url`，传了必炸 | PiRuntime 移除 sandbox-url 透传（留待 pi 未来支持） |
| 6 | `'NoneType' object has no attribute 'strip'` | **exe（windowed 无控制台）locale 为 GBK**，`subprocess(text=True)` 默认 GBK 解码 pi 的 UTF-8 中文输出 → readerthread 静默崩溃 → `communicate` 返回 `stdout=None`。pi 侧任务实际完美完成（session 可证），崩在 NovaMind 接收侧 | `encoding="utf-8", errors="replace"` 显式指定 + stdout/stderr None 兜底 |

排障过程中的误诊澄清：GUI 内 agent 多次把锅甩给"pi 未安装/pi 配置缺字段"，实际 4/6/6 轮根因都在 NovaMind 侧或环境层。`'NoneType' object has no attribute 'strip'` 是 Python 风格报错（Node 会报 `Cannot read properties of null`），可据此快速定位崩溃归属。

### 同轮其他修复

- **监控面板时间慢 8 小时**：审计日志 ts 为 UTC（带 Z），前端 `tsShort` 未做时区转换。改为 `new Date(ts)` 解析后按本地时区格式化（`novamind/webui/static/index.html`）。
- **打包 DLL 新坑（避坑清单第 9 条）**：`libssl-3-x64.dll` / `libcrypto-3-x64.dll`（`_ssl.pyd`/`_hashlib.pyd` 依赖）不在 spec binaries 时 exe 静默崩溃（import ssl 失败 → langchain 链断）。已补进 `NovaMind.spec` 的 `_MISSING_DLLS`（6→8 个）。此前未踩是因为打包 shell 的 PATH 恰好含 `Anaconda3\Library\bin`。
- **pi models.json BOM 坑**：PowerShell `Set-Content -Encoding UTF8` 写 BOM，Node `JSON.parse` 不容忍 → pi 报 `Unknown provider "novamind"`。需无 BOM 写入（`[IO.File]::WriteAllText` + `UTF8Encoding($false)`）。

### 验证结果

- 全量测试 **325 passed**（304 基线 + 21 新增：multiagent 接线 13 + pi provider/model 3 + shim 解析 3 + None stdout 回归 1 + sandbox-url 禁透传调整等）。
- 端到端实测（源码环境，PiRuntime 原样调用）：中文多行真实任务 49.1s 完成（pi 自主写文件 + 发现无系统 Python 主动用 `uv` 执行验证 + 三段式结构化汇报）。
- exe 重打包验证：`/health` ok、`/skills` 37 个技能无 error、无 DLL 警告。

### 遗留事项

- pi 产物落在其工作目录（项目根），沙箱透传（`--sandbox-url`）待 pi 上游支持后再收敛到工位。
- pi 委派在本机系统无 Python 时会自行用 `uv` 执行验证，可能产生 `.venv` 副作用（已清理测试产物）。
- `delegate_to_subagent`（fork 自身副本）仍未接线：`SubagentProvider` 只有 Protocol，无默认实现。
- 记忆模块 `storage_path` 默认 `.novamind/memory`（相对 CWD），不跟随 DATA_ROOT 约定，跨目录启动会散落（历史遗留，待统一）。

---

## 2026-08-20 之前

见 `docs/test-report.md`（2026-08-08 真实环境测试报告：复杂任务链 / 10 Agent 并发）与 `HANDOFF.md`。
