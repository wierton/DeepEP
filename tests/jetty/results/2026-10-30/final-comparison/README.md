# Dispatch 性能差异调查汇总

## 已确认的结果

针对当前追查的 **2-rank 非 cached Dispatch 发起 kernel**，主要差距不是一个 ReserveBatchSq 检查，而是初始化与批次/请求接口形态共同造成的。全程未删除 Write、发布或最终通信等待来制造性能收益。

最终同场顺序：原版 → 未优化迁移版 → 组合调整版 → 组合调整版 → 未优化迁移版 → 原版；每次保留原有结果校验、10 warmups 和 50 次 profiler 采样，六次 exit=0。

| 版本 | 第1轮 / μs | 第2轮 / μs | 均值 / μs |
|---|---:|---:|---:|
| 原版 DeepEP | 23.855 | 23.973 | 23.914 |
| 未优化迁移版（已经删除 startup Drain） | 26.371 | 26.047 | 26.209 |
| 组合调整版 dynamic-sge | 24.124 | 24.108 | 24.116 |

迁移版与原版相差 2.295 μs，组合调整缩小 2.093 μs。该组剩余 0.202 μs；紧接的独立同场复测中原版为 24.1875 μs、dynamic-sge 为 24.1155 μs，剩余差值方向反转，**不能将其认定为稳定的新回归**。详见 [反证实验](../warp-init/README.md)。这些样本仍不足以声称严格统计等价。

这里的发起耗时是 device kernel 完整执行耗时，不是 Host launch API 的调用耗时；最终通信等待在 epilogue。每轮取两个 rank 的 profiler 均值中较大者，再按轮平均，不是逐迭代跨 rank 求 max 后平均。

## 三处有无插桩对照支持的差异

### 1. 初始化失去重叠，且 EID 变成逐字节读取

原版由 warp 0 处理 peer，warp 1 加载 Jetty；迁移版将 Jetty 放在线程 0，和当前两卡配置下的 peer 初始化落在同一 warp。API 的 EID 读取同时变成 16 次 byte load/移位，原版是两次 64-bit load。

保留构造 API、全部字段和 block 同步，恢复两者重叠、宽读取后，无桩整体从 **25.552 → 24.7765 μs**。这是两项组合的阶段收益，不分别相加推算。[源码补丁与日志](../init-validation/README.md)

### 2. 不知道批次总量，导致最后一条 WQE 被后补修改

原版先算出 num_sqes，填最后一条时直接写 final flags。迁移接口直到 AdvanceBatch 才确认最后一条，并从 GM 回读首字、改标志、再写回。

加入实验 BeginBatch(totalWqebbs)，首次填 WQE 时产生与原 AdvanceBatch 一致的最终首字；AdvanceSq、fence、head 发布和 doorbell 全部保留。无桩整体从 **26.056 → 25.517 μs**。[补丁与日志](../known-batch/README.md)

### 3. 运行时 SGE 数量被转成六路模板分支

原版按实际 SGE 数在同一条路径填请求；迁移版根据 pairs 分派 Write<2/4/6/8/10/12>。实验接口 WriteDynamic 保留全部通信逻辑，但用运行时 SGE 数构造 header、payload 与 NOP。

无桩整体从 **24.689 → 24.165 μs**；相同实例的 SIMT worker 从 18072 B 降到 12336 B。这支持“接口展开方式造成了差距”，但没有硬件计数器证明具体是指令缓存或寄存器压力。[补丁与日志](../dynamic-sge/README.md)

不同对照组的收益存在环境和编译交互，不能机械相加。最终三版本同场测试才是组合结果的依据。

## 为什么删掉约 2000 tick，整体却没有变化

同边界局部插桩证实：申请区间原版 368 tick，迁移版 2368 tick，精简后 330 tick；但单项无桩整体 26.065 → 26.217 μs，没有收益。[证据](../fixed-batch/README.md)

不能把每请求的平均区间直接当成 kernel 关键路径：多个 warp 并行，SIMT 又与 scalar/MTE 的 metadata、本地 payload 搬运并行，最后才通过 PIPE_V→PIPE_S 合流。逐请求插桩还会改变编译结果。后续只在 scalar 主流程插桩，看到精简后等待 VF 的平均阶段缩短，但本地 payload 阶段同时变长，总跨度只小幅变化；这解释了为什么不能从单段 tick 推导等量 kernel 收益，而不将具体变化武断归因于某个微架构原因。

进一步的外层测量表明，即使申请逻辑已精简，初始化和整批请求构建/提交仍明显慢于原版；之后分别用上述无桩等价改写验证了主要差异。[scalar 阶段](../scalar-stages/README.md)、[SIMT 外层阶段](../sparse-simt/README.md)

## 边界与尚未完成的部分

- 当前结果限定两卡、1024 tokens、hidden 4096、topk 4、8 experts、BF16、16 AI cores/32 AIV，不能外推全部配置。
- cached Dispatch 仍约 21.54 μs，对应原版约 20.54 μs；Combine 也仍有发起耗时差异。本轮没有把这两条路径的差异定位完。
- 这些是可复现的实验头文件副本/patch，不是已经修改系统安装或生产通用 API。变长/混合请求、任意 rank 数、队列容量边界需要另行设计与验证。
- 不是每个局部 tick 都已被分配到唯一的微架构根因；保留未证实项，不将假说写成结论。

## 位置与复现

远端根目录 `/mnt/docker/o00939992`，独立实验在 `jetty-init-study`，独立 JIT cache 前缀 `cache-fixed-`，所有运行使用物理卡 2、3。安装头文件未改；实际 JIT 的覆盖头文件通过预处理重放验证，生成对象有哈希记录。

实验生成顺序：`prepare_ablation.py --init fixed-control unchecked-fixed-batch` → `prepare_known_batch.py` → `prepare_known_batch_init.py` → `prepare_dynamic_sge.py`。每个脚本参数为远端根目录；生成目录需不存在（warp-init 的生成器允许显式再生成）。

```bash
bash run_fixed_batch.sh 'original:51 fixed-control:51 dynamic-sge:51 dynamic-sge:52 fixed-control:52 original:52'
```

本组原始日志：[logs](logs)。完整实验产物保留在相邻目录，不覆盖上一轮原始结果。
