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

当前进度：0、1 卡原版 131 轮完成并通过校验；远端请求构建平均 406.812 tick。
三个 Combine 实例的实际预处理路径和编译产物已核验。
迁移版 131、132 以及原版 132 仍在本次顺序任务中，完整配对结论待收集。

生成：`prepare_combine_join_timing.py`；解析：`summarize_combine_join.py`。
另准备 `prepare_combine_header.py`，仅在普通 Write 批次下重建最后 WQE 固定头，保留头更新、fence、完成计数和 SQ head 发布。
它是待验证的等价接口实验，不是已经证明有效的优化；不适用于 inline/notify/混合批次。
