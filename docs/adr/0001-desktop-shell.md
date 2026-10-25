# ADR 0001：桌面 Shell 技术选型（Phase 0 / A0）

- 状态：**Accepted（Phase 0 结论，A1 前可复核）**
- 日期：2026-10-19
- 决策范围：Windows 先行；桌面托盘 / 悬浮入口 / 全局快捷键 / 轻量面板的承载方式
- 关联任务书：NovaMind VNext 迭代方案 §3.1、§4 A0

---

## 1. 背景

NovaMind 现有完整 GUI 是 pywebview（`novamind/webui/app.py`）+ 本地 FastAPI/SSE。VNext 需要新增：

- 系统托盘与「二次启动唤醒既有实例」；
- 可拖动/贴边/隐藏的悬浮入口；
- 全局快捷键（默认 `Ctrl+Shift+Space`）；
- 轻量 Quick Panel，与完整工作台共享同一 `WebRuntime`；
- loopback API 的 Host/Origin/会话令牌安全基线。

A0 要求先做可行性 Spike，用实测数据在两条路线间决策，不在主 GUI 内盲目大改。

---

## 2. 候选方案

### 方案 A：PySide6 作为桌面 Shell

- `QSystemTrayIcon` 托盘；透明无边框置顶窗口；`QWebEngineView` 复用 Web UI。
- 优点：原生窗口/托盘/快捷键能力集中，多窗口生命周期可控。
- 代价：PySide6 + Qt WebEngine 依赖与打包体积；与现有 pywebview 事件循环并存；Qt 与 Anaconda DLL 的兼容性。

### 方案 B：pywebview + 原生薄壳（ctypes）

- 保留 pywebview 作为面板窗口；托盘与全局热键用 Win32 API（`Shell_NotifyIconW`、`RegisterHotKey`）在独立线程实现。
- 优点：零新增重型依赖，最大化复用现有 GUI/前端；托盘/热键实测可用。
- 代价：Windows 细节需自行适配（DPI/多屏/焦点）；无原生菜单生态；依赖系统 WebView2 运行时。

---

## 3. 实测数据

环境：Windows 11、Python 3.13.9（Anaconda 基座 venv）、PyInstaller 6.22.2、pywebview 6.x、PySide6 6.12.0。

| 指标 | 方案 A（PySide6） | 方案 B（pywebview + ctypes） |
|---|---|---|
| 依赖安装体积 | **647.1 MB**（PySide6 + shiboken6） | 2.1 MB（webview，已在依赖中） |
| 导入耗时 | **失败**：`DLL load failed while importing QtWidgets: 找不到指定的程序 (WinError 127)` | 0.155 s |
| 托盘注册 | 未能进入实测 | ✅ `tray_added=true` |
| 全局热键 | 未能进入实测 | ✅ `hotkey_registered=true`（Ctrl+Shift+Space） |
| 窗口创建 | 未能进入实测 | ✅ `window_created=true`（frameless + on_top） |
| 进程 RSS | 未能进入实测 | 96.9 MB（主 Python 进程；WebView2 子进程未计入） |
| 单实例 | 未测 | ✅ 二次 acquire 被拒；释放后可重新获取 |
| 本机 API 守卫 | 未测 | ✅ Host 伪造/跨站写请求 403；Cookie/Token 授权通过 |
| 端口释放 | 未测 | ✅ 后端 `should_exit` 后端口完全释放 |

复现命令：

```powershell
python -m novamind.desktop.spike --mode pyside --self-test
python -m novamind.desktop.spike --mode pywebview --self-test
python -m novamind.desktop.spike --smoke
```

> 说明：PySide6 失败的根因高度指向本项目 Anaconda 基座 venv 的 DLL 搜索路径/运行库冲突（已尝试预加载 msvcp140 系列、`os.add_dll_directory`，仍失败）。这不代表 PySide6 在干净 CPython venv 不可用；但当前项目发布基座就是该 venv，A0 以「可发布环境」为准。

---

## 4. 决策

**Phase 0 选择方案 B：pywebview + 原生薄壳（ctypes）。**

理由：

1. 方案 A 在当前可发布环境无法导入 QtCore/QtWidgets，连窗口都无法创建；
2. 即使修复环境，647 MB 依赖对单文件 exe 的打包体积/冷启动/分发成本过高；
3. 方案 B 复用现有 pywebview 与前端，新增代码仅 `win32_shell.py`（约 250 行 ctypes），托盘/热键/置顶窗口实测全部可用；
4. 单实例与本机 API 安全基线在方案 B 下已落地并全部通过冒烟。

**保留复核条件**：A1 冻结前，在干净 CPython 3.13 venv（非 Anaconda 基座）复测 PySide6 的导入、托盘、打包体积与冷启动；若显著优于方案 B 且打包可接受，再提交 ADR 修订。

> 2026-10-20 补充：Spike 结束后已从项目 venv 卸载 PySide6（实验依赖，不在 `pyproject.toml`）。首次打包发现 PyInstaller 会因 pywebview 的可选 Qt 后端把 PySide6 顺带收进包（exe 50.7MB → 68MB），已在 `NovaMind.spec` 的 `excludes` 显式排除 `PySide6` / `PyQt6`，重打包恢复体积。

---

## 5. 已落地实现

| 文件 | 内容 |
|---|---|
| `novamind/desktop/single_instance.py` | Windows 命名互斥量 / POSIX flock；二次启动拒绝 |
| `novamind/desktop/win32_shell.py` | 隐藏消息窗口 + 托盘图标（打开/退出菜单）+ 全局热键 |
| `novamind/desktop/spike.py` | 两方案自测 + A0 门禁冒烟（单实例/守卫/托盘/热键/端口释放） |
| `novamind/webui/local_guard.py` | 纯 ASGI 守卫：Host/Origin 白名单 + SameSite=Strict 会话 Cookie + X-NovaMind-Token |
| `novamind/webui/app.py` | 桌面入口默认 `NOVAMIND_LOCAL_GUARD=1` |
| `novamind/webui/server.py` | 挂载 `LocalGuardMiddleware`（默认关闭，不影响测试） |
| `tests/unit/test_single_instance.py` | 二次实例拒绝/释放后获取/上下文管理 |
| `tests/unit/test_local_guard.py` | Host 伪造、跨站 Origin、无令牌写请求、Cookie/Token 授权、默认关闭不回归 |

---

## 6. 后果与风险

**正向**

- 打包体积基本不变（不引入 Qt）；复用现有 WebView2 运行时；
- 单进程 + loopback 后端的治理模型保持简单；
- 托盘/热键/置顶窗口在真实 Windows 会话实测通过。

**负向 / 待验证**

1. **DPI / 双屏 / 全屏遮挡 / 失焦行为**：自测无法覆盖，A1 必须做人工矩阵（100%/125%/150%，单/双屏，全屏应用）。
2. **二次启动「唤醒」**：A0 只做到拒绝第二个实例；唤醒既有实例需要 IPC（命名管道或 loopback 控制端点），属 A1。
3. **打包**：尚未把 Spike 打进 exe 做体积/冷启动实测，属 A1 门禁。
4. **WebView2 运行时**：Windows 10/11 通常自带；缺失时需要引导安装或降级浏览器。
5. **热键冲突**：`RegisterHotKey` 失败时必须有 UI 提示与重设入口（A1）。
6. **RSS 口径**：96.9 MB 仅主进程；WebView2 子进程另计，A1 需按进程树汇总。

---

## 7. A1 前置任务（精确）

1. 人工矩阵：单/双屏 × 100%/125%/150% × 全屏应用 × 最小化/休眠唤醒，记录坐标、焦点、遮挡。
2. 单实例 IPC：命名管道 `\\.\pipe\novamind-<user>` 或 loopback `/shell/wake`，二次启动唤醒并退出。
3. exe 打包：把 Spike 入口并入 `NovaMind.spec`，实测单文件体积、冷启动、托盘图标、退出后无僵尸进程。
4. 热键冲突 UI：注册失败显示占用提示 + 可重设；持久化到设置。
5. 安全复核：Cookie 轮换/退出清理；SSE 在守卫下不缓冲（已用纯 ASGI 保证，需 A1 真机复验）。
