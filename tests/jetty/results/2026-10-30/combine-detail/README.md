# Combine 主循环差距集中在等待 VF 完成

在当前两卡 BF16 配置下，迁移版的额外主循环时间主要表现为 scalar 等待 VF metadata/WQE 完成；不是周期性敲 doorbell 或反复构造 publisher。本组保留每一项通信、等待和结果校验。

## 对照条件与结果

设备 **0、1**；1024 tokens、hidden 4096、topk 4、8 experts、16 AI cores / 32 AIV。单独成组，不与之前设备 2、3 的阶段数据合并。运行顺序 original:91 → control:91 → control:92 → original:92，四次 exit=0。

每次新增 25 次 Combine，前 5 次预热，20 次记录；每次都验证输出与权重。只用 scalar `asc_get_system_cycle()`，不混用 SIMT 时钟。每个 sample 选 entry barrier 后跨度最长的 core，其所有阶段来自同一个 core，再平均两个 rank 和两轮。数值见 [summary.json](summary.json)。

| 主循环中的区间 | 原版 / tick | 迁移版 / tick | 差值 |
|---|---:|---:|---:|
| 等待 VF，迁移版还读取已完成的 task head | 17521 | 21907 | +4386 |
| 按 token 规约、搬运本地数据 | 12961 | 11433 | −1527 |
| 提交下一项 VF | 1217 | 657 | −560 |
| 发布分支判断及执行 | 45 | 59 | +14 |
| 循环中的其余开销 | 172 | 196 | +24 |
| 主循环合计 | 31916 | 34253 | +2337 |

差值存在阶段间迁移：不能将 +4386 tick 全解释成某个 API 的独立成本，更不能把它直接当成 kernel 的最终增量。这里给下一次细分提供方向：**VF 内 metadata/Write/AdvanceSq，而非 scalar publisher**。

## 打点对应哪些代码

两边都在 `combine_impl` 的 task 循环中记录以下边界，具体修改见两份 `.patch`：

```cpp
loop0 = asc_get_system_cycle();
asc_sync_wait(PIPE_V, PIPE_S, static_cast<event_t>(stage_idx));
// 迁移版接着读取 ub->task_heads[stage_idx]。
loop1 = asc_get_system_cycle();

// 原来的逐 token reducer / MTE 代码，原样保留。
loop2 = asc_get_system_cycle();
issue_load_meta_write(stage_idx, task_iter_idx + kNumStages);
// 原版相应调用名是 issue_load_meta_wqe。
loop3 = asc_get_system_cycle();

// 原来的周期性 MTE3 等待、doorbell、条件 CQ 回收，原样保留。
loop4 = asc_get_system_cycle();
```

每个 task 只在 scalar 寄存器中累加四段时差；kernel 结束测量后才写私有 trace 区，Host 等整个调用结束再回拷。未向 SIMT worker 增加同步。每个 core 的 trace 前缀从 10 扩为 16 个 uint64，额外保存四段累计值和 total/remote task 数。

## 当前配置没有触发周期性 doorbell

被选中 core 的每次记录都是 **total tasks = 8，remote tasks = 4**。循环条件是：

```cpp
if (!is_local && (++num_issued_remote_tasks % kNumTasksPerDoorbell == 0)) {
    // publisher / RingDoorbell / conditional Drain
}
```

而 `kNumTasksPerDoorbell = 8`，故这批任务不会进入该分支；59 tick 包含分支判断，不是 doorbell 的成本。最终的 doorbell 仍在循环结束后的发布阶段执行。

这是针对当前规模的排除证据，不代表更大规模永远不经过周期发布。

## 下一层待验证的差异

迁移版 `load_meta_issue_write` 每 remote token 调用 `Write<2,false>`，随后 block sync，再由 thread 0 `AdvanceSq()` 并保存 task head；原版 `load_meta_issue_wqe` 直接写预先计算的槽位，末尾没有这套逐 task batch 收尾。迁移版的 local task 也经过函数末尾的 block sync。

这些操作都包含在 VF 完成条件中，但本轮尚未将 VF 内部的 metadata、请求构建、同步和 AdvanceSq 分开。因此不能进一步声称具体多出的 tick 全来自 atomic、容量检查或某条 fence。

初始化调整的一轮无桩测试见 [单独记录](../combine-init/README.md)，倒序测试被 HCCL 初始化异常打断；本组打点不是它的无桩性能验证。

## 复现

```bash
python prepare_combine_detail.py /mnt/docker/o00939992
ASCEND_RT_VISIBLE_DEVICES=0,1 bash run_fixed_batch.sh \
  'combine-detail-original:91 combine-detail-control:91 combine-detail-control:92 combine-detail-original:92'
python summarize_scalar_timing.py jetty-init-study/fixed-batch-logs --combine-detail
python verify_ablation_headers.py jetty-init-study --combine combine-detail-original combine-detail-control
```

生成器以已创建的 coarse Combine 打点副本为输入，要求新目录不存在。实际 JIT 预处理确认使用实验副本，产物哈希见 [instruction-summary.json](instruction-summary.json)，原始日志已压缩保留。数据、计算与通信操作未省略；打点仍会改变编译和调度，须再用无桩等价改写确认具体优化收益。
