# 真实模型压测工具

对独立隔离实例发起真实模型调用，验证并发/稳定性/工具链路/取消恢复与费用。
**不进入普通 pytest、不接入默认 CI**；运行前必须填写执行清单（见
`docs/REAL_MODEL_LOAD_TEST_PLAN.md` §3）。

## 文件

| 文件 | 作用 |
|---|---|
| `real_model_server.py` | 隔离实例启动器：独立 workspace、凭据受控注入、注入 `MeteredChatModel`（预算预留 + 调用级 usage 记录） |
| `real_model_load.py` | SSE 压测客户端：预热/基线/闭环并发阶梯、指标采集、预算熔断、报告 |
| `harness.py` | `BudgetController`（预算准入/结算/80% 熔断）、`CallRecord`、`SseParser`（增量分块解析） |
| `metered.py` | `MeteredChatModel`：包装 BaseChatModel，调用前预留、调用后按 usage 结算 |
| `.run/` | 运行产物（隔离 workspace、calls.jsonl、summary.json、report.md）——不提交 |
| `../unit/test_real_model_load_harness.py` | 免费单元测试（预算/解析器，进入普通 pytest，不联网） |

## 执行清单（运行前确认）

- 供应商/模型（固定单一，避免降级混入）与金额上限
- 机器信息、Python/依赖版本、源码 commit
- 模型设置（温度/输出上限/超时/重试）与运行配置绝对路径
- 确认隔离 workspace 不触碰生产数据库/日志

## 常用命令

```powershell
# 预检（不联网）
python tests/performance/real_model_server.py --precheck --workspace tests/performance/.run --budget 10 --calls-log tests/performance/.run/calls.jsonl

# 启动隔离实例（另开终端）
python tests/performance/real_model_server.py --workspace tests/performance/.run --budget 10 --calls-log tests/performance/.run/calls.jsonl --port 8976

# 预热 + 基线
python tests/performance/real_model_load.py --base http://127.0.0.1:8976 --scenario warmup

# 独立会话阶梯（并发 1→2→4，每档 20）
python tests/performance/real_model_load.py --base http://127.0.0.1:8976 --scenario ladder --concurrency "1,2,4" --per-tier 20

# 免费单测（进入普通 pytest）
python -m pytest tests/unit/test_real_model_load_harness.py -q
```

## 配置字段（real_model_load.py）

| 参数 | 默认 | 说明 |
|---|---|---|
| `--base` | `http://127.0.0.1:8976` | 隔离实例地址 |
| `--scenario` | `warmup` | `warmup`=预热+基线；`ladder`=闭环并发阶梯 |
| `--concurrency` | `1,2,4` | ladder 并发档位（逗号分隔） |
| `--per-tier` | `20` | 每档轮次上限 |
| `--budget` | `10.0` | 客户端侧预算熔断（元，读取 calls.jsonl 累计） |
| `--out` | `tests/performance/.run` | 结果目录 |

## 指标口径

- 轮次总耗时：发起到合法 `done` 终态（`error` 后 `done` 不判成功）；
- 首文本帧时间：第一个有内容的 `text` 帧，**不标为 token TTFT**；
- 调用耗时：每次模型 invoke 开始到结束（SDK 内重试另计，无法细分明确标注）；
- 成本：`estimate_cost` 按估算单价（非供应商结算），报告明确区分实测/估算。

## 结果解读

`calls.jsonl`：调用级 model/usage/耗时/status（凭据）。`summary.json`：每档
样本量/p50/p95/max/失败原因。`report.md`：验收判定（参考方案 §7 建议标准）。
少样本不作可靠 p99 承诺；成功样本延迟与失败等待分表报告。
