# DeepEP 与 Jetty 优化版：完整性能入口复测

本报告只比较原版 DeepEP 与优化版，不将中间迁移对照作为交付结果。
日期：2026-10-30；950PR，最终有效组使用物理卡 0/1、2 ranks。

## 版本及测量口径

- `original`：远端 DeepEP-baseline，原版通信实现，保留此前验证所需的相同环境兼容改动。
- `dynamic-sge`：Dispatch 组合优化（初始化并行、宽 EID、已知批次最终头、动态 SGE）。
- `combine-known-header`：独立 Combine 优化副本，重建普通 Write 头，省去 GM 读回。
- 两份优化是独立副本，**不是声称已在同一二进制合并验证**。EP 表中 Dispatch/cached 取 dynamic-sge，Combine 取 combine-known-header；其他入口使用 dynamic-sge 的迁移实现。
- 每项保留正确性检查。EP/PP/Engram/Barrier 使用原脚本 profiler；Bucket 使用原脚本 NPU events，包含 `.wait()`。
- 表中取每次测试两个 rank 均值的较大值，不是逐迭代跨 rank 求 max。发起 kernel 和通信等待计时分别展示，不能简单相加当作端到端延迟。
- EP 配置：1024 tokens，hidden 4096，topk 4，8 experts，BF16，16 AI cores。其他入口使用原脚本默认配置，仅进程数改为 2。

## 结果总表（耗时单位 μs）

本轮测试及异常复测已结束。**并非全部通过：PP 迁移版发生可复现的 Drain 断言失败；Bucket 迁移版两次结果校验失败，不能提供它们的性能比值。**

| 测试项 | 原版 DeepEP | 对应优化版 | 结论 |
|---|---:|---:|---|
| EP Dispatch 发起 | 24.081 | 24.302 | 正确性通过；约慢 0.92%，本轮单次有效配对 |
| EP cached Dispatch 发起 | 20.558 | 21.458 | 正确性通过；约慢 4.38%，仍未对齐 |
| EP Combine 发起，两轮均值 | 34.662 | 37.503 | 两轮都通过；约慢 8.20%，倒序复测确认差距 |
| PP send，4096×7168 BF16 | 1306.762 | 无有效结果 | 原版通过；迁移版两次在压力测试 seed=0 的 Drain 断言失败 |
| PP recv，同上 | 67.699 | 无有效结果 | 同上，未到性能阶段 |
| Engram fetch | 203.4 | 203.6 | 两边正确性通过；约 +0.10%，本配置基本一致 |
| Bucket AllGather，1 GiB/rank，包含 wait | 23753.8 | 无有效结果 | 原版通过；迁移版两次在小数据正确性前置检查失败 |
| Barrier，首轮 / 重测 | 10.742 / 8.189 | 8.531 / 9.003 | 两边通过；rank 和轮次波动明显，不认定稳定收益 |

Bucket 同场 HCCL 对照为 26707.7 μs；这不是 Jetty 优化版的成绩。默认脚本 10 warmups、30 iterations，输出完整操作的 max-rank mean。

### EP 通信等待与收尾的独立指标

下表来自首个有效配对，不是把发起时间相加得到的值。原脚本通过不同 profiling pass 测发起和 URMA 等待，详见 `tests/ep/test_ep.py:410` 的 `benchmark()`。

| 指标 | 原版 | 对应优化版 |
|---|---:|---:|
| Dispatch URMA | 222.099 | 220.246 |
| Dispatch epilogue | 42.168 | 42.195 |
| cached Dispatch URMA | 219.577 | 215.766 |
| cached Dispatch epilogue | 35.633 | 35.682 |
| Combine URMA | 283.489 | 233.457 |
| Combine epilogue | 19.525 | 19.467 |

Combine 发起慢不等于通信等待也慢；本轮优化版 URMA pass 更短。不能据此推导完整算子端到端加速率。

### 每个入口的配置

| 入口 | 除 2 ranks 外的关键参数 | 范围 |
|---|---|---|
| `tests/ep/test_ep.py` | 1024 tokens、4096 hidden、topk=4、experts=8、BF16、16 AI cores、test-first-only | 含 noncached/cached/Combine；不是全部 dtype、模式和规模 |
| `tests/pp/test_pp.py` | 4096×7168 BF16、4 inflight、4 stress iterations、128 sends | 先做随机收发压力校验，再测 send/recv |
| `tests/engram/test_engram.py` | 每 rank 两层 524288/524309 entries、hidden=256、512 tokens、24 entries/token | 默认存储类型，保留 fetch 校验 |
| `tests/bucket/test_all_gather.py` | 1 GiB/rank，10 warmups/30 iterations，EP_AVOID_RECORD_STREAM=1 | 小/非对齐分片、64-bucket 校验通过后才测性能 |
| `tests/comm/test_barrier.py` | 默认每次 callback 调 1000 次 barrier，10 warmups/50 profiler samples | 不运行无限压力模式 |

## 失败、干扰与重测记录

1. `pair45`：原版完成，但检测到同卡其他用户进程；随后迁移控制版本初始化报 HCCL error 15。该组不用于收益计算。
2. `pair01-first`：原版 profiler 在 Combine 阶段缺失目标 kernel，`StopIteration`，exit 1；只输出了 Dispatch/cached，整轮不纳入有效均值。
3. `pair01-retry1`：同配置原版重测成功，动态 SGE、Combine 已知头均完整成功。中途用户要求取消中间版本/固定重复轮；已开始的第二轮 Combine 被主动取消，不算算法失败。

### Combine 差距复测

| 顺序 | 原版发起 μs | 优化版发起 μs |
|---|---:|---:|
| 首轮：原版 → 优化版 | 34.611 | 37.405 |
| 复测：优化版 → 原版 | 34.713 | 37.601 |

原版变化 0.29%、优化版变化 0.52%；慢约 8.2% 的结论在这两次测量中一致。[复测日志](ep-retest)

### PP：不是性能波动，是运行失败

第一次与独立重测都在 `tests/pp/test_pp.py` 的 seed=0 压力测试失败。源码位置：`deep_ep/include/deep_ep/impls/pp/pp_send_recv.hpp:94`：

```cpp
const auto write_result = hcomm.WriteWithNotify<kNumTimeoutCycles>(...);
EP_DEVICE_ASSERT(write_result == 0);
const auto drain_result = hcomm.Drain<kNumTimeoutCycles>();
EP_DEVICE_ASSERT(drain_result == 0); // 两次报错处
```

日志报告 AIV trap，error code 286。能够确认 Drain 返回非零，不能由此确定究竟是 CQE 状态、权限还是其他底层原因；本轮没有修改通信实现以强行绕过断言。
[首测日志](other-first/pp-dynamic-sge-1.log) / [重测日志](pp-retest/pp-dynamic-sge-3.log)。

### Bucket：先补环境，再复查结果

首测两边均被 `csrc/buffers/bucket.hpp:145` 的 `EP_AVOID_RECORD_STREAM=1` 要求挡住，不算性能结果。按源码要求统一设置后，原版通过，而迁移版在 `tests/bucket/test_all_gather.py:88` 失败：

```python
result = handle.wait()
assert result.ndim == 1
assert torch.equal(result, ref.view(-1))
```

此时测的是小规模 out-of-place AllGather 前置正确性，不是 1 GiB 性能场景。第二次同配置重测也在同一行失败；不能跳过校验去报性能。
[补环境后的首测](extra-retest/bucket-dynamic-sge-3.log) / [第二次确认](bucket-retest2/bucket-dynamic-sge-4.log)。

### Barrier：重测后仍有波动

| 运行 | 原版 rank0 / rank1 μs | 优化版 rank0 / rank1 μs |
|---|---|---|
| 首测 | 6.670 / 10.742 | 7.553 / 8.531 |
| 重测 | 6.660 / 8.189 | 9.003 / 6.976 |

原版 max-rank 值变化超过 20%，两边反复出现 rank 不均衡；这里包含同步等待，数据不支持“稳定加速/回退”判断。保留全部四次成功结果，不取最小值包装收益。

## 环境与可信度限制

所有测试都在同一台共享 A5 主机执行，未终止其他用户进程。4/5 的干扰组单独保留，不混入最终对照；0/1 运行期间也曾观察到短暂外部 `ccu_print_test_` 进程，因此不是严格独占环境。小于约 1% 的差异不作微架构结论，Barrier 已重测仍不稳定。两 rank 的覆盖不能替代多服务器、多 rank、多 dtype 测试。

## 复现与证据

- [EP 首次有效数据](pair01-retry1)、[Combine 倒序重测](ep-retest)。
- [其余四类首测](other-first)、[Bucket/Barrier 重测](extra-retest)。
- [PP 失败复现](pp-retest)、[Bucket 失败复现](bucket-retest2)。
- [机器可读数据](summary.json)：保留所有成功样本和失败状态，包括取消前已测出的中间版本；本报告的最终比较只采用原版和对应优化版。
- 每条日志前附源文件哈希、命令、卡号、JIT cache 路径；各目录 `status.tsv` 为退出码。目录 `pair45` 与 `pair01-first` 保留干扰/采集失败样本，不计入有效汇总。

[执行脚本](../../../run_perf_suite.sh) 支持 `CASES`、`TRIALS` 指定独立复测项；[解析脚本](../../../summarize_perf_suite.py) 只将 exit 0 且所有 rank/指标齐全的日志纳入统计。缺失数据、重复 rank、失败运行分别列出。

明显异常先用轮间变化 >5%、rank 差异 >10% 筛查，再结合原版差距和占卡情况判断；重测保留原记录，不只选择更快的样本。
