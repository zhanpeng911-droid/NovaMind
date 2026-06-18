# NovaMind 变更日志

## [2.0.0] - 2026-05-28

### 重大变更
- 项目从 CyberClaw 全面重命名为 NovaMind
- 替换 LangGraph StateGraph 为自定义异步状态机引擎
- 新增中间件管道系统（计时、日志、速率限制）
- 新增 Token 追踪与成本估算模块
- 使用 AST 安全计算器替代 eval() 方案
- 新增事件总线（EventBus）解耦组件通信
- 新增插件生命周期管理（on_load/on_unload 钩子）
- 全新终端 UI 设计（深空蓝+翡翠绿配色）
- 全新监控面板（卡片式事件展示）
- 新增 Docker 部署支持
- 新增 pyproject.toml 现代包配置
- 新增 ProviderFactory 工厂类（支持 LLM 实例缓存）

### 架构改进
- 状态机支持条件路由和最大循环次数限制
- 中间件管道支持洋葱模型（请求从外向内，响应从内向外）
- TokenTracker 支持多会话隔离和成本预警
- ContextManager 独立封装上下文裁剪逻辑
- PluginManager 新增插件启停控制和版本管理

### 安全改进
- AST 解析替代 eval，杜绝代码注入
- 保留五重 Shell 命令安全过滤
- 保留路径穿越防护机制
- 保留沙盒执行环境隔离

## [1.0.0] - 2026-01-01

### 初始版本（CyberClaw）
- LangGraph StateGraph 智能体循环
- 多模型适配器（OpenAI/Anthropic/阿里云/腾讯/智谱/Ollama）
- 双水印记忆系统（用户画像 + SQLite对话历史）
- 懒加载技能系统
- 零信任技能执行（两阶段调用）
- 异步审计日志（JSONL格式）
- Rich 实时监控面板
- 沙盒安全执行环境
- 心跳调度器（定时任务）
- 12个内置工具
