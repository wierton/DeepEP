# 无插桩验证：运行时 SGE 数量 API

## 对比的唯一阶段

在 known-batch-init 基础上，原调用根据 pairs 走六个 `Write<2/4/6/8/10/12, false>` 分支。原版 DeepEP 则按运行时数量直接填同一种请求，不展开六套请求构建逻辑。

本实验在 Jetty 头文件副本增加 `WriteDynamic(warp, peer, dst, sge, pairs * 2)`，仍由 API 的 PostSend → ReserveBatchSq → FillSqWarpLane 路径构建完整 WQE，仍保留 fence、warp 同步、block 同步、AdvanceSq、doorbell 和 Drain。

API 变化：

- 普通 PostSendArgs 增加动态 SGE 数，旧调用默认 0，仍使用模板参数。
- 新入口限制 warp 协作、普通 Write、非即时提交；检查数量在 1..MAX_SGES。
- 按运行时 activeSges 填 header、SGE 和 padding/NOP，而不是仅修改 header 后仍写满所有 SGE。
- 每个请求仍固定占四个 WQEBB，已知批次的最后标志逻辑保持不变。

## 真机结果

卡 2、3，同一配置、正反两轮、四次 exit=0，保留原测试结果校验。

| 版本 | Dispatch 第1轮 / μs | 第2轮 / μs | 均值 / μs |
|---|---:|---:|---:|
| known-batch-init | 24.706 | 24.672 | 24.689 |
| dynamic-sge | 24.102 | 24.228 | 24.165 |

减少 **0.524 μs**。cached 均值 21.563 → 21.5905 μs，没有收益；未修改的 Combine 也不作为此项优化收益。

## 编译证据及边界

实际 JIT 预处理确认 switch(pairs) 消失，新 API 调用存在，且没有性能时钟桩。相同 non-cached / final_barrier=false 实例，readelf -sW 中 `simt_persistent_worker` 的函数字节数：

| 版本 | kernel cache key | worker Size |
|---|---|---:|
| known-batch-init | dispatch.b339a8f19965c3d5e464a1cd355404f2 | 18072 |
| dynamic-sge | dispatch.024f8be205856c52323ceb56d067df57 | 12336 |

这证明模板分支展开造成了代码形态差异，并且恢复动态数量接口有整体收益；**没有硬件计数器证据能把收益进一步严格分配给分支、寄存器压力或指令缓存**，不把“代码更小”直接当成已证明的微架构根因。

补丁：[dynamic-sge.patch](dynamic-sge.patch)，原始日志：[logs](logs)，对象哈希：[instruction-summary.json](instruction-summary.json)。生成脚本 `prepare_dynamic_sge.py`。仅供当前固定大小 DeepEP Write 批次实验，不是已经完成通用 API 验证或生产合入。
