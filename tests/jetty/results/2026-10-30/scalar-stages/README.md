# Scalar 主流程定位：差距出现在等待 SIMT 完成

**有效性更正：旧 Dispatch 插桩没有为 cached correctness 的备用输入预留 trace 尾部；以下 tick 数据待修复重测，不能作为可靠定量依据。见 [存储复查](../TRACE_STORAGE_AUDIT.md)。**

在此前 ReserveBatchSq 精简实验之后，重新从无插桩的原版、迁移 control、unchecked 副本生成三套 scalar-only 插桩。没有给 SIMT worker 或每条 Write 加时间戳。

## 测量边界

统一使用 `asc_get_system_cycle()`，保存以下八个边界，所有数据回写放在末尾：

1. 入口 Barrier 前。
2. Barrier 后、准备启动 VF 前。
3. VF_CALL 后、开始搬 metadata 前。
4. metadata 搬运完成，发布 metadata_ready 前。
5. 等待 local_copy_ready 完成、开始本地 payload 搬运前。
6. 本地 payload 搬运完成、等待 VF 前。
7. PIPE_V → PIPE_S 等待结束。
8. 发布 SQ doorbell 之后。

采样路径设置 `set_barrier_in_prologue(False)`，与发起 kernel 性能路径一致；最终通信等待保留在 epilogue，采样回拷前同步。标记记录检查确认 kDoBarrier=0。

## 结果（scalar tick，不与 SIMT tick 混算）

每个样本选择入口 Barrier 后到发布结束跨度最长的一个 core，所有阶段都取这个 core，保证阶段之和等于该 core 的总跨度；不是分别挑每段最大值后相加。每版本 2 轮 × 2 rank × 20 样本。

| 阶段 | 原版 | 迁移 control | unchecked |
|---|---:|---:|---:|
| VF 启动 | 52 | 59 | 59 |
| metadata | 760 | 772 | 769 |
| 等 local slots | 3607 | 3708 | 3698 |
| 本地 payload | 20401 | 17081 | 17437 |
| 等 SIMT 结束 | 201 | 4953 | 4508 |
| 发布 | 498 | 483 | 476 |
| Barrier 后合计 | 25520 | 27057 | 26948 |

它支持以下定位：差距不是末尾 doorbell 发布；迁移后的 scalar 本地拷贝完成时，SIMT worker 仍未结束。ReserveBatchSq 精简降低了一部分等待，但未恢复到原版状态。

不能将等 SIMT 的全部差值当作整体回归：本地 payload 的实测跨度也不同，两个执行路径本来就有重叠。入口 Barrier 的等待受 rank 到达时刻影响很大，单列、不拿它推导迁移原因。最长跨度 core 也不等于未经对齐即可认定的全局最后结束 core。

## 扰动检查与复现

同一次运行同时保留 stock profiler 测试和原有结果校验。原版第一轮 profiler Dispatch 为 25.787 μs，而无插桩为约 24.06 μs，表明八个时钟读取及末尾 GM 回写有可见开销。因此本表是定位依据，不能替代无插桩优化验证。

六次测试全部 exit=0。原始日志见 [logs](logs)。生成脚本 `tests/jetty/prepare_scalar_timing.py`，解析脚本 `tests/jetty/summarize_scalar_timing.py`：

```bash
python3 tests/jetty/summarize_scalar_timing.py tests/jetty/results/2026-10-30/scalar-stages/logs
```
