# 依赖审计记录（未完成处置）

2026-10-22，GUI 收尾时执行只读命令：

```powershell
uv audit --locked --no-build --output-format json
```

工具报告审计 127 个包，63 条漏洞公告记录、1 条归档状态告警，退出码 1。公告可能互为别名，不将此数量解读为 63 条独立可利用攻击链。

| 包 | 锁定版本 | 公告记录 | 工具列出的修复版本 |
| --- | --- | ---: | --- |
| aiohttp | 3.14.3 | 8 | 3.14.4 |
| certifi | 2026.7.22 | 2 | 2026.10.19 |
| langgraph-sdk | 0.4.3 | 2 | 0.4.4 |
| multidict | 6.7.1 | 2 | 6.9.1 |
| Pillow | 12.3.0 | 28 | 13.0.0 |
| urllib3 | 2.7.0 | 6 | 2.8.0 |
| virtualenv | 21.7.5 | 15 | 各公告分别给出 21.7.11–21.14.9 范围内的修复版本 |

`langchain-community` 另外被标记为 archived。

这轮修改针对界面与桌面窗口，不进行整套业务依赖升级。未忽略公告、未改低门槛、未将本次审计写为通过。尚需逐条去重、判断运行时/开发时可达性，特别是现有图片入口使用的 Pillow；不能因应用仅供个人使用就直接认定所有告警不可达。后续应分别评估兼容性、升级锁文件并跑回归，不宜盲目批量升级。

## 2026-10-24 复核与兼容修复

上文保留为历史记录，不能作为当前公告数量或已处置状态。本轮由主代理直接接续完成复核；未读取 `.env`、调用真实模型或访问个人记忆文件。

已按官方发布说明将 **certifi 2026.7.22 → 2026.10.19**、**urllib3 2.7.0 → 2.8.0** 更新到锁文件和本地环境，原生导出 `requirements.txt`。没有升级模型 SDK。urllib3 的代理 TLS 行为变更已阅读；自有源码未使用公告涉及的自定义 HTTPS 转发代理参数，不将这项源码核对冒充真实供应商连接验收。

- [certifi 官方发布](https://github.com/certifi/python-certifi/releases/tag/2026.10.19)
- [urllib3 官方变更记录](https://urllib3.readthedocs.io/en/stable/changelog.html)

解析器的 `--no-build` 会拒绝整张锁图中无 wheel 的既有 jieba / proxy-tools。核对官方 PyPI JSON 和已安装 METADATA 后，为 **jieba 0.42.1**、**proxy-tools 0.1.0** 提供了版本精确限定的空 `requires-dist` 静态元数据，未编造 Python 版本限制。锁文件由 `uv lock` 生成，没有手工改锁。对其余锁定包逐个设置 `--no-build-package`；这两项旧包使用静态元数据完成解析，没有执行它们的源码构建。实际 dry-run 仅列出上述两个目标包的版本变化。安装使用 `uv pip install --no-build --no-deps`，只安装官方 wheel。

静态元数据机制依据 [uv 官方设置说明](https://docs.astral.sh/uv/reference/settings/#dependency-metadata)。`uv pip check` 检查 109 个已安装包，兼容性通过。

### 升级后的原生审计快照

```powershell
uv audit --locked --no-build --output-format json
uv audit --locked --no-build --no-extra dev --no-extra ollama --output-format json
```

| 范围 | 审计包数 | 公告记录（含别名） | 归档告警 | 退出码 |
| --- | ---: | ---: | ---: | ---: |
| 全锁图 | 127 | 79 | 1 | 1 |
| 排除 dev、ollama extra | 80 | 43 | 0 | 1 |

全锁图记录按包与 CVE/GHSA/PYSEC 别名合并后为 73 组；不是 79 条互相独立的攻击链。全锁图记录分布为 aiohttp 8、filelock 3、langgraph-sdk 2、multidict 2、Pillow 41、virtualenv 23。默认依赖范围剩余 langgraph-sdk 2、Pillow 41。此快照中 certifi、urllib3 已没有告警。公告数据库会变动，这些数字只代表本次调用结果。

### Pillow 升级前的未处置项（历史快照）

1. **Pillow 12.3.0 → 13.0.0 尚待用户授权，未升级。** [官方 High 公告 GHSA-g4qv-2jxx-769h](https://github.com/python-pillow/Pillow/security/advisories/GHSA-g4qv-2jxx-769h) 涉及 PNG eXIf 解析的二次复杂度。项目 `save_image()` 确实调用 `ImageOps.exif_transpose()`，当前文件/像素上限不能证明阻断该路径。官方修复版本为 13.0.0；已核对 [13.0.0 发布说明](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html) 与当前 RGB/RGBA、LANCZOS、绘图用法，但尚未对新版做本项目回归。按用户“大版本另行确认”的边界保留待办；不以禁用图片或忽略公告替代修复，也不发布一个宣称安全的新 EXE。
2. **LangGraph SDK 自定义授权装饰器告警保留。** [官方公告](https://github.com/langchain-ai/langgraph/security/advisories/GHSA-fvww-7h3r-vfhp) 仅涉及使用 `actions=` 的资源授权装饰器。本项目自有源码未使用相关 `auth.on.threads/assistants/crons` 路径，未发现这条已核对调用链的可达使用；这不是对所有第三方或未来功能的不可达保证。没有通过忽略规则隐藏该告警。
3. 可选 Ollama、开发工具依赖和 archived 状态没有在本轮批量升级；其余公告没有逐条做完整利用复现。不得将“默认应用无需该可选功能”泛化为所有构建、开发、平台均安全。

功能回归、Ruff、mypy 通过不能替代安全审计。当前原生审计仍退出 1，尤其图片入口存在已确认的可达 High，**审计未通过，Pillow 授权与升级完成前不覆盖正式 EXE**。

## 2026-10-24 获准升级 Pillow 后的结果

用户明确允许 Pillow 大版本升级后，已将 **Pillow 12.3.0 → 13.0.0**，并将项目约束设为 `Pillow>=13.0.0,<14`。原生 uv dry-run 仅列出 Pillow 这一项变化；保留先前两项版本精确限定的静态元数据和针对其他依赖的禁构建配置，未执行未知源码安装脚本。安装仅使用官方 wheel，`requirements.txt` 从更新后的锁文件导出，模型 SDK 保持原版本。

阅读官方 13.0.0 的兼容性说明后，确认本项目未使用被删除的 I-mode PNG 保存、改变类型的 fromarray(mode)、Image._show 或 Makernote 旧别名；RGB/RGBA、EXIF 转向、LANCZOS 和当前图标绘图实际回归通过。这是当前用法的兼容性证据，不是对 Pillow 全部 API 的兼容承诺。

同样原生审计命令重新执行，未换审计源、未添加 ignore：

| 范围 | 审计包数 | 剩余公告记录（含别名） | 归档告警 | 退出码 |
| --- | ---: | ---: | ---: | ---: |
| 全锁图 | 127 | 38 | 1 | 1 |
| 排除 dev、ollama extra | 80 | 2 | 0 | 1 |

**Pillow 告警 41 → 0，已确认图片入口的该项 High 不再命中。** certifi、urllib3 仍无告警。全锁图剩余 aiohttp 8、filelock 3、langgraph-sdk 2、multidict 2、virtualenv 23 条记录；默认依赖范围剩余 SDK 同一漏洞的 2 条别名记录，仍按上文自有源码不使用相关授权装饰器的事实跟踪，不把“未发现当前可达调用”写成所有场景安全保证。

其余可选/开发包与 archived 状态没有擅自批量升级；下次复核日期为 **2026-11-07**，若开始使用相关功能或出现新公告应提前复核。这只是风险跟踪日期，没有创建后台自动任务。

升级后图片/桌面针对性检查 **95 项通过**（含新增 5 条格式和 EXIF 正向兼容用例），完整常规回归 **953 passed / 3 skipped / 2 subtests passed，覆盖 78.56%**；43 项 Node、29 项离线原生检查、Ruff、10 文件 mypy 与已安装包兼容性检查通过。没有真实模型调用，没有把功能回归当成审计全绿。

正式 `dist-personal-ui/nova-mind-gui.exe` 已重新打包并完成包内核对：Pillow 版本为 13.0.0，原生图片扩展与本地 wheel 一致。实际 EXE 在隔离工作区启动，四个原生窗口响应、后端健康、诊断构建信息及五项前端资源检查通过；未发送聊天请求或操作个人数据。构建与产物校验信息见 `ITERATION_POLISH_2026-10-24.md`。审计退出码仍为 1，剩余风险如实保留。
