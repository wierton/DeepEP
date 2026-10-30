# Combine：单 warp 记录，拆开 join 与 AdvanceSq

本实验继承 `combine-vf` 的独立输入前缀计时区，但只有 thread 0 写记录。
这样其他 warp 的计时写入不会在 thread 0 执行 AdvanceSq 时并发干扰它。
原有通信、线程同步、完成等待均不删除；新增的 `clock64()` 仍会扰动执行，不能代替无桩性能验证。

记录布局（8 个 uint64）：

| 下标 | 含义 |
|---|---|
| 0 | VF 开始 |
| 1 | metadata/weights 处理完成 |
| 2 | 请求构建完成、block join 前 |
| 3 | AdvanceSq 和 task_heads 保存完成 |
| 4 | 是否远端任务 |
| 5 | token 是否有效 |
| 6 | block join 后、AdvanceSq 前 |
| 7 | ping-pong stage |

原版没有对应的 join/AdvanceSq，故记录相邻时钟作为对照；不能说原版完全没有 SQ 发布，其发布在别处。
脚本检查其余 warp 的记录全零、每核计时单调、20 次样本、两个 rank、stage 交替。
同一任务的五个时钟均来自 thread 0，不混减 scalar/SIMT 或不同 warp 的时钟。

## 完整配对结果

0、1 卡：original131 → control131 → control132 → original132，四次全部退出 0，并保留结果校验。
两边各三个 Combine 实例的实际预处理路径和编译产物均已核验。
每核每次 8 个任务，其中 4 个远端任务；每 rank 20 次样本。先在每 rank/轮次取平均，再等权平均四组。

| 远端任务，warp 0，tick | 原版 | 迁移版 |
|---|---:|---:|
| metadata/weights | 1890.09 | 1611.35 |
| 请求构建 | 406.93 | 1534.47 |
| block join | 2.11（相邻时钟） | 708.24 |
| AdvanceSq + task_heads 保存 | 2.01（相邻时钟） | 2068.41 |
| 整个区间 | 2301.15 | 5922.47 |

该结果把此前混在一起的收尾拆开：多出的时间不是全部在 block join 中，AdvanceSq 一侧还有约 2068 tick。
不过此项包括封装构造、条件判断和 task_heads 保存，不是只测 AdvanceBatch 的 GM load。
原版其他位置仍有发布与同步，不能将这个表直接转换成整体 kernel 收益。
本地任务的 join 平均约 1338 tick；此前独立无桩条件 join 实验没有测出提速，见 [local-join](../combine-local-join/README.md)。

生成：`prepare_combine_join_timing.py`；解析：`summarize_combine_join.py`。
另准备 `prepare_combine_header.py`，仅在普通 Write 批次下重建最后 WQE 固定头，保留头更新、fence、完成计数和 SQ head 发布。
它是待验证的等价接口实验，不是已经证明有效的优化；不适用于 inline/notify/混合批次。
`--timed` 从本轮 control 生成单 warp 计时版本；默认生成无桩版本，两组独立配对，不能混算。
