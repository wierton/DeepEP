# 无插桩验证：已知批次大小，首次填 WQE 时写入完成标志

## 问题与等价改写

unchecked 已去掉 ReserveBatchSq 的完成进度读取、容量检查和 atomic_max，但 AdvanceBatch 仍回读最后一条 WQE 的首字，再写入完成标志。原版 DeepEP 提前知道 num_sqes，不需要这次回读/回写。

本实验在独立 asc-comm 头文件副本加入 `BeginBatch(totalWqebbs)`：初始化时记录 DeepEP 已统计出的总量。每个 warp 仍调用 Write，FillSqWarpLane 判断 `head + 4 == batchEnd`，首次写 WQE 时直接形成**与原 AdvanceBatch 相同**的最后首字（包含 batchEnd 的低 16 位和 FINAL_FLAGS）。

AdvanceSq 调用仍保留，仍进行原有 fence、更新 UB/GM packedHead、重置批次状态；只是不再回读和重写已经正确填写的 WQE。所有生产者的现有 block 同步、发布 doorbell 和 Drain 均保留。这个接口实验限定固定四-WQEBB、非 inline 的普通 Write 批次，不是生产通用 API 承诺。

## 无额外时钟插桩的实测

同一台 950PR 的卡 2、3，stock 正确性和 profiler。顺序 unchecked → known-batch → known-batch → unchecked，四次 exit=0。

| 版本 | Dispatch 第1轮 / μs | 第2轮 / μs | 均值 / μs |
|---|---:|---:|---:|
| unchecked | 26.005 | 26.107 | 26.056 |
| known-batch | 25.585 | 25.449 | 25.517 |

该接口/编排改写使 Dispatch 平均缩短 **0.539 μs**。这证明提交路径存在整体可见的成本；不是删除通信来制造收益。它同时改变了 final flag 的产生位置，不能把全部收益狭义归因于单条 GM load 指令。

每轮取两个 rank 的 profiler 均值中较大者，再对两轮平均。cached 均值 21.569 → 21.3865 μs。两轮不是统计显著性检验，更不是所有配置的性能保证。

补丁：[known-batch.patch](known-batch.patch)，原始日志：[logs](logs)。生成脚本 `prepare_known_batch.py`。使用 `verify_ablation_headers.py` 验证实际 JIT 预处理：BeginBatch 调用存在、AdvanceBatch 内不再有 firstWord，但 fence 保留；无 traceTicks_/scalar_probe_/sparse_0。
