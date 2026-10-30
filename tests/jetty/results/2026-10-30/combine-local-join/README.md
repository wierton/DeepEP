# Combine：仅远端写任务进行 block join

## 改动与边界

实验从 `fixed-control` 复制，生成器为 `tests/jetty/prepare_combine_local_join.py`。
将 `load_meta_issue_write` 末尾的 `asc_syncthreads()` 移入所有线程一致的 `if (issue_write)`。
本地任务没有 WQE 写入，调用者仍等待 VF 完成后读取 metadata；原版也没有这里的 block join。
远端任务的 Write、block join、AdvanceSq、task_heads 发布及后续完成等待全部保留。
没有计时桩，没有修改正式代码或安装目录头文件，Combine 初始化 Drain 仍保留。

## 实测

2026-10-30，设备 2、3，2 ranks，1024 tokens，hidden 4096，topk 4，8 experts，BF16，16 AI cores。
运行顺序：control121 → local-join121 → local-join122 → control122。
前三次通过功能校验；最后一次在 EPBuffer 构造期间 HCCL 初始化失败，尚未执行待测 Combine。

| 版本/轮次 | Combine 耗时（μs） | 状态 |
|---|---:|---|
| fixed-control 121 | 38.423 | 通过 |
| combine-local-join 121 | 38.651 | 通过 |
| combine-local-join 122 | 39.281 | 通过，但没有配对成功的对照 |
| fixed-control 122 | — | HCCL 初始化：Error code 15，call runtime api fail |

统计取两个 rank 各自 profiler 均值的最大值，**不是每轮先取 rank 最大值再平均**。
第一组配对变化为 +0.228 μs，没有提速。第二组不完整，不参与配对收益计算。
原始日志保留，包括失败日志；不能用不同轮次/设备的结果补齐此配对。

## 结论

局部 VF 打点看到约 1351 tick 的本地任务 block join 区间，并不意味着删除它就会让总 kernel 少这么多时间。
当前无桩对照没有证明收益，不能将局部时间乘任务数作为整体收益，也不能据此推广生产改动。
下一步仍需拆解远端任务的 Write 与 join/AdvanceSq，并用无桩配对验证。
