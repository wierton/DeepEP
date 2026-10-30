# Combine：原版与 Jetty 迁移版的分阶段差分

本组只定位 Combine，不将 Dispatch 的结论套过来。保留所有 Write、AdvanceSq、Drain、doorbell 和原有正确性校验；额外 25 次 Combine 也逐次校验输出与权重，前 5 次预热，后 20 次取样。

## 相同边界的打点结果

物理卡 2、3，2 ranks，1024 tokens，hidden 4096，topk 4，8 experts，16 AI cores / 32 AIV，BF16。顺序：original:71 → control:71 → control:72 → original:72。

两边都在 scalar 使用 `asc_get_system_cycle()`，不混用 SIMT `clock64()`。每次选取 entry barrier 后总跨度最长的那个 core，再从**同一个 core**取各阶段；最后平均 20 samples × 2 ranks × 2 rounds。不是各阶段独立取最大值，也不是 profiler 的逐轮 rank-max。完整数据见 [summary.json](summary.json)。

| 阶段 | 原版 / tick | 迁移版 / tick | 迁移 − 原版 |
|---|---:|---:|---:|
| Jetty / peer 初始化（含 VF 完成等待） | 3120 | 4109 | +990 |
| task 规划、初始化 reducer | 338 | 333 | −5 |
| 前两项 task 预取提交 | 215 | 180 | −35 |
| task 主循环 | 31752 | 34493 | +2741 |
| 尾部 MTE3 等待、最终发布 | 3623 | 2625 | −999 |
| 未启用 final barrier 的空区间 | 3 | 1 | −2 |
| entry barrier 后的总跨度 | 39051 | 41741 | +2690 |

主循环包括等待 VF metadata/WQE、局部规约与搬运、下一项 VF 提交、周期性 doorbell，以及有条件的 CQ 回收。**不能将 +2741 tick 全算给 ReserveBatchSq。** 初始化和主循环均有差距；末尾发布反而更短。

入口 barrier 的等待会受两进程到达偏差影响，单独保存在 JSON，不计入上述 post-barrier 总跨度。打点改变编译结果，本表用于定位，不直接将 tick 差换算成无桩性能收益。

## 源码差异：下一步要逐项验证的内容

两边入口都是 `deep_ep/include/deep_ep/impls/ep/combine.hpp::combine_impl`。

| 位置 | 原版 | 迁移版 | 目前能下的结论 |
|---|---|---|---|
| 初始化 | scalar 构造 Jetty，VF 加载 peers | VF thread 0 构造 Jetty 并 Drain，然后同一 warp 加载 peer | 比较的是不同初始化路径；不是空构造函数 |
| 每 token 请求 | `load_meta_issue_wqe` 按已有槽位填 word | `load_meta_issue_write` 调用 `Write<2,false>` | 新版包含分配、检查和 API 同步，不只填 word |
| 每 task 收尾 | VF 返回，没有逐 task Advance | block sync 后 thread 0 `AdvanceSq`，保存 `task_heads` | 增加逐 task batch 收尾和完成请求 |
| 周期发布 | 复用已构造 Jetty，更新 head 后 doorbell | 重新加载 publisher descriptor，覆盖为已完成 task 的 head，再 doorbell | 必须保留完成前缀，不能把尚未准备好 payload 的预取任务一起发布 |
| 末尾发布 | 补带完成请求的强序 NOP、对齐、advance、doorbell | 已逐 task 产生完成请求，此处 publisher + doorbell | 两边完成协议不同，发起与完整通信时间不可混为一谈 |

另一个已确认的布局差异：原版每 token 的 2 SGE 请求占 `ceil((48 + 2*16)/64) = 2` WQEBB；公开 warp Jetty 的固定 slot 占 4 WQEBB。这里只确认空间/填充差异，尚未证明它贡献多少耗时。

**Combine 的显式 startup Drain 仍存在。此前“已移除 startup Drain”的结论仅针对 Dispatch。** 本组没有删除它；后续初始化对照也保留它。

## 如何复现和核对

```bash
python prepare_combine_timing.py /mnt/docker/o00939992
bash run_fixed_batch.sh 'combine-original:71 combine-control:71 combine-control:72 combine-original:72'
python summarize_scalar_timing.py jetty-init-study/fixed-batch-logs --combine
python verify_ablation_headers.py jetty-init-study --combine combine-original combine-control
```

输入张量前额外分配 64×10 个 uint64 的私有区域，kernel 仅在测量区间之后写入，Host 等整个调用完成再回拷。所有 Combine 调用复用同一带前缀的 input，未占用通信 window、workspace 或改变 kernel 参数。原本用于无效 slot 的末尾 dummy row 保留。

预处理重放确认实际使用实验副本；原版仍使用原始 `handle.hpp`，迁移版使用绝对路径绑定的覆盖头文件。见 [instruction-summary.json](instruction-summary.json) 和两份 `.patch`。系统安装头文件、生产源码均未修改。
