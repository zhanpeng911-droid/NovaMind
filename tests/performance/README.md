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
- 确认隔离 workspace 不触碰生产数据库/会话库
- **隔离限制（如实披露）**：测试 workspace / SQLite / office 目录独立于生产，
  但源码模式下 audit logs 仍写入项目 `logs/`；只允许使用无隐私测试材料。
  不要写“日志完全隔离”。需要完全独立日志再做源码快照运行或增加日志路径配置。

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

`calls.jsonl`：调用级 model/usage/耗时/status/reserved（凭据）。`summary.json`：
每档样本量/p50/p95/max/失败原因。`report.md`：验收判定。少样本不作可靠
p99 承诺；成功样本延迟与失败等待分表报告。客户端每次运行写入唯一结果子目录
（`<out>/<时间戳>/turns.jsonl + summary.json`），不覆盖前一档；逐轮只保存
批次/场景/thread_id/状态/耗时/长度，不含提示词或回复。

## 费用保护（个人收尾 Phase 1）

- unknown usage（usage 缺失/部分缺失/无法确认）**不按免费处理**：预留保留为
  风险金额（pending 不释放），`summary` 区分“仍在途”与“已结束但费用未知”
  （`unknown_reserved_yuan`）；实际费用超预留时如实记账并停止准入。
- 输出上限/超时/重试经 `build_metered_llm` 显式传给真实模型（预检验证送达，
  模型不支持即预检失败）；预算与单价拒绝 NaN/inf/非正。
- 重启保护：calls.jsonl 已非空或未显式填写 `--budget` 时拒绝启动，防止误
  操作恢复成全额预算；续测需新批次目录 + 显式剩余额度。预检不写付费日志。
- 本地保护不宣称能终止已提交到供应商的任务或消除延迟出账。
