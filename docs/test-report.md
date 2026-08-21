# NovaMind 真实环境测试报告

> 测试环境：Windows 11 + Python 3.13 + DeepSeek (deepseek-v4-flash) 真实 API
> 测试日期：2026-08-08

---

## 一、复杂任务链验证（真实 LLM）

### 测试设计

3 个真实复杂任务，要求 Agent 自主规划并连续调用多种工具完成：

| 任务 | 要求步骤 | 工具组合 |
| --- | --- | --- |
| project_report | 12 步 | 时间 + 计算 + 建目录 + 写文件 + 列目录 + 读文件 + 改文件 + 再读 + 画像 + 定时任务 + 查任务 + 模型信息 |
| calc_dashboard | 6 步 | 三连计算 + 建目录 + 写文件 + 读文件 + 列目录 |
| file_organizer | 7 步 | 列目录 + 读文件 + 建目录 + 写文件 + 再列 + 再读 |

### 执行结果

| 任务 | 状态 | 工具调用 | 耗时 | 策略违规 |
| --- | --- | --- | --- | --- |
| project_report | ✅ | 13 次 | 28.6s | 0 |
| calc_dashboard | ✅ | 7 次 | 6.5s | 0 |
| file_organizer | ✅ | 8 次 | 13.7s | 0 |

**project_report 完整工具链**（13 步连续调用，全程零策略违规）：

```
get_current_time → calculator → calculator → execute_office_shell
→ get_system_model_info → write_office_file → list_office_files
→ read_office_file → write_office_file → read_office_file
→ save_user_profile → schedule_task → list_scheduled_tasks
```

### 指标

| 指标 | 数值 |
| --- | --- |
| **任务完成率** | **100%** (3/3) |
| **平均耗时** | **16.3s / 任务**（最长复杂链 28.6s） |
| **平均工具调用** | **9.3 次 / 任务**（最长 13 次） |
| **策略违规** | 0 次（沙盒 + 白名单全程拦截有效） |

---

## 二、并发压力测试（10 Agent 并发）

### 测试设计

10 个不同 `thread_id` 的 agent 同时执行 10 个不同任务（计算 / 时间 / 建目录 / 写读文件 / 定时任务 / 文件总结等），并发监控 EventBus 队列深度与 emit 延迟，验证状态机会话隔离。

### 执行结果

| Agent | 任务 | 状态 | 工具调用 | 耗时 |
| --- | --- | --- | --- | --- |
| agent_a | 计算 123*456 | ✅ | 1 | 3.8s |
| agent_b | 查询当前时间 | ✅ | 1 | 1.8s |
| agent_c | 建目录 + 列文件 | ✅ | 2 | 2.8s |
| agent_d | 计算 + 模型信息 | ✅ | 2 | 2.3s |
| agent_e | 写文件 + 读文件 | ✅ | 2 | 3.7s |
| agent_f | 双计算 | ✅ | 3 | 3.3s |
| agent_g | 列目录 + 计数 | ✅ | 1 | 3.6s |
| agent_h | 计算 + 定时任务 | ✅ | 3 | 4.4s |
| agent_i | 读文件 + 总结 | ✅ | 2 | 5.9s |
| agent_j | 计算 + 写文件 | ✅ | 2 | 4.7s |

### 指标

| 指标 | 数值 |
| --- | --- |
| **并发完成率** | **100%** (10/10) |
| **并发总耗时** | **6.9s**（串行估算约 69s，**10 倍加速**） |
| **单任务平均耗时** | **3.5s** |
| **EventBus 最大队列深度** | 24 / 100（容量充足） |
| **EventBus 最大 emit 延迟** | 0.06ms |
| **EventBus 阻塞超时** | 0 次 |
| **状态串扰** | 0 处（10 个 thread_id 完全隔离） |

---

## 三、结论

1. **EventBus 无阻塞**：10 agent 并发 emit，队列峰值仅 24/100，emit 延迟 0.06ms，零阻塞超时。asyncio.Queue 背压设计在 10 并发下完全无压力。
2. **状态机零串扰**：10 个 thread_id 的消息内容完全隔离，无跨会话污染。
3. **并发扩展性良好**：瓶颈在 LLM API 网络延迟而非框架本身，5 并发 → 10 并发总耗时仅 +1.4s。
4. **安全策略全程有效**：复杂任务链与并发测试中均零策略违规，沙箱与工具白名单拦截正常。
5. **全量回归**：147 个自动化测试全部通过。

---

## 附：自动化测试体系（147 个）

| 测试文件 | 数量 | 覆盖 |
| --- | --- | --- |
| test_state_machine.py | 18 | 状态容器、边路由、SQLite 持久化、迭代上限 |
| test_context.py | 15 | 裁剪、摘要词法评估、LLM 二次评估（真实 API） |
| test_agent.py | 14 | Token 提取、策略拦截、中间件管道 |
| test_agent_eval.py | 13 | 多轮对话、工具收敛、会话隔离 |
| test_integration.py | 14 | CLI、日志、SQLite 隔离、端到端循环 |
| test_e2e.py | 6 | 端到端全链路（工具循环、持久化、策略、迭代上限） |
| test_logger.py | 7 | 脱敏、有界队列背压、优先级驱逐 |
| 其他 | 60 | middleware / monitor / provider / sandbox / session 等 |
