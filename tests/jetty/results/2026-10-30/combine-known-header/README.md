# Combine：定位 AdvanceBatch 的 WQE 头读回开销

## 唯一通信实现改动

原 AdvanceBatch 从 SQ 的 GM 地址读回最后 WQE 的第一个 uint64，然后替换 PI、设置 final flags。
Combine 该批次只使用普通 `Write<2, false>`，其 FillSqWarpLane 对 word 0 写入的是：

```cpp
HCOMM_SIMT_WRITE_HEADER | sqIdx
```

实验用同样的表达式重建 `firstWord`，继续执行原来的头更新、store、threadfence、完成计数更新、SQ head 发布、batch reset。
没有删掉 Write、AdvanceSq 或完成等待，也没有改变 SGE、数据量、WQEBB 数量及 doorbell 时机。
仅 Combine translation unit 定义实验宏；其他 kernel 仍走原来的 GM 读回。
这不是通用替换：inline Write/notify/混合批次不能直接使用这个已知头表达式。

生成器 `prepare_combine_header.py` 默认生成无桩版本，`--timed` 从单 warp 打点版本生成等价改动。
正式代码和安装目录不变。补丁、实际 JIT 头文件/对象核验与原始日志均在本目录。

## 分段对照：已完成

设备 0、1，2 ranks，1024 tokens，hidden 4096，topk 4，8 experts，BF16，16 AI cores。
运行顺序 control141 → known-header141 → known-header142 → control142；四次均退出 0，功能校验保留。
每 rank 20 次样本、每核 8 个任务（4 个远端）。记录来自同一个 warp 的 clock64，只有 thread 0 写计时区。

| 远端任务区间（tick） | 原迁移实现 | 重建已知头 |
|---|---:|---:|
| metadata | 1594.38 | 1792.19 |
| 请求构建 | 1541.29 | 1554.86 |
| block join | 711.54 | 750.56 |
| AdvanceSq + head 保存 | 2081.43 | 787.54 |
| 整个 VF 区间 | 5928.63 | 4885.14 |

收尾减少约 **1294 tick**；请求构建没有相应下降，符合改动只影响收尾的预期。
这支持 GM 头读回是收尾开销的重要来源，但不能称为该 load 的孤立硬件延迟：编译调度、fence 与 overlap 也可能变化。
其他阶段时间也发生变化，故不能把 1294 tick 直接乘远端任务数作为总 kernel 收益。

## 无桩验证：已完成

同卡运行 fixed-control151 → combine-known-header151 → combine-known-header152 → fixed-control152，四次全部退出 0，通过功能校验。

| Combine 发起 kernel（μs） | 第 151 轮 | 第 152 轮 | 两轮平均 |
|---|---:|---:|---:|
| fixed-control | 38.780 | 38.916 | 38.8480 |
| combine-known-header | 37.343 | 37.274 | 37.3085 |

平均减少 **1.5395 μs，约 3.96%**，正反两个顺序均减少。
这里是完整 device 发起 kernel 的时间，不是 Host API 耗时，也不包含随后 epilogue 中的通信完成等待。
每轮取两个 rank 各自 profiler 均值中的较大值，再平均轮次，不是逐迭代跨 rank 求 max。
实际 JIT 核验确认三个 Combine 实例均使用实验头且不含计时桩。

因此在这个配置下，头读回已被等价接口改写和无桩测试确认为整体差距的一部分。
它没有解释余下全部差距，也不是任意通信操作都能使用的通用优化；不跨配置推广约 4% 的收益。
