# Combine 的差距进一步落在请求构建和逐 task 收尾

## 四次双边测试

设备 0、1，1024 tokens、hidden 4096、topk 4、8 experts、16 AI cores / 32 AIV、BF16。顺序 original:111 → control:111 → control:112 → original:112，四次 exit=0，所有新增采样调用均校验输出和权重。

每轮前 5 次预热、后 20 次采样。VF 内每个 warp 的 lane 0 保存自己的四个 `clock64()`；只在同一个 warp 内相减，不减 scalar 时间戳，也不跨 core 相减。下表为 warp 0 的 task 均值，再平均两个 rank、两轮；没有将多个 warp 的耗时相加。

| task | 阶段 | 原版 / tick | 迁移版 / tick |
|---|---|---:|---:|
| local | metadata / weights / first slot | 2114 | 1820 |
| local | 请求分支，无实际 Write | 42 | 43 |
| local | block join，无 Advance | 2 | 1351 |
| local | 函数测量区间 | 2158 | 3215 |
| remote | metadata / weights / first slot | 1940 | 1736 |
| remote | 请求构建 | 406 | 1565 |
| remote | block join + thread 0 AdvanceSq | 2 | 2857 |
| remote | 函数测量区间 | 2348 | 6158 |

原版最后一段只是相邻两次取时钟，没有对应的同步/Advance 操作，所以约 2 tick 不能解释成其通信完成成本。原版最终的完成请求在 scalar 末尾发布路径，不在这个 VF 里。

最长 warp、全部有效 warp 的均值，以及每轮每 rank 的详细结果见 [summary.json](summary.json)。所有记录仍是每 core 8 tasks / 4 remote tasks，未触发周期性 doorbell。

## 两边实际测量哪些代码

原版是 `load_meta_issue_wqe`，迁移版是 `load_meta_issue_write`，均在 `deep_ep/include/deep_ep/impls/ep/combine.hpp`。

1. `vf0`：读取 threadIdx 之前。
2. `vf1`：得到 first_slot_idx 之后，开始构建请求之前。
3. `vf2`：请求构建完成；迁移版在 block barrier 之前。
4. `vf3`：函数主体结束，写 trace 之前。

迁移版请求阶段包含 attach Jetty/peer、远端边界检查、SGE 准备和完整 `Write<2,false>`；不是仅测 Reserve。收尾原代码：

```cpp
asc_syncthreads();
if (thread_idx == 0 && issue_write) {
    jetty.AdvanceSq();
    ub->task_heads[stage_idx] = ub->jetty_info.packedHead;
}
```

因此：

- local task 虽然没有 Write，依然支付 block barrier。
- remote task 的 warp 0 还负责 Advance；其他 warp 不执行 Advance。不能用全 warp 的平均数代替 warp 0 的该段成本。
- remote 的 2857 tick 仍混合了等待其他 warp 和 Advance 本身，本轮不能将二者再拆开。

## 测量限制

这些是插桩区间，不能直接乘 task 数或换算成 kernel 增量；VF 与 scalar/MTE 重叠。trace 写入安排在本 warp 的末次时间戳之后，但其他 warp 的 trace store 仍可能与 warp 0 的 Advance 重叠，因而这个 2857 tick **不是无扰动的纯 Advance 成本**。更精细地测 Advance 时应改为仅 warp 0 记录，避免这种交叉影响。

当前数据支持两个具体下一步：用无桩等价改写验证 local task 的无效 join；继续将 remote 请求阶段和 batch 收尾拆开。不能仅凭表格宣称优化已获得多少微秒。

## 私有 trace 布局与验证

所有 Combine 输入都有独立前缀：640 个 uint64 存 scalar 边界，后面按 `[64 cores][32 tasks][8 warps][8 words]` 存 VF 数据，输入数据与末尾 dummy row 保留。编译期检查 core/task 上界，Host 仅复制实际 core/task 的记录。trace 不放在通信 window，不修改通信参数内容。

每 warp 的 8 words 是四个时间戳、remote 标志、有效 token 标志、task token 数、stage 下标。分析器检查两个 rank、20 samples、阶段单调、stage 交替、有效 token、每次采样的时钟增长，拒绝不完整或陈旧数据。

实际 JIT 预处理核对了新参数、时钟和前缀，原版仍用原始 handle 实现；见 [instruction-summary.json](instruction-summary.json) 和两份 `.patch`。系统头文件和生产 kernel 没有改动。

```bash
python prepare_combine_vf_timing.py /mnt/docker/o00939992
ASCEND_RT_VISIBLE_DEVICES=0,1 bash run_fixed_batch.sh \
  'combine-vf-original:111 combine-vf-control:111 combine-vf-control:112 combine-vf-original:112'
python summarize_combine_vf.py jetty-init-study/fixed-batch-logs
python verify_ablation_headers.py jetty-init-study --combine combine-vf-original combine-vf-control
```

生成器以 coarse Combine 打点副本为输入，目标目录须不存在。运行中的日志可能尚无完整采样，分析器会拒绝；待测试退出后再汇总。
