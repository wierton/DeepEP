# 无插桩验证：恢复初始化重叠与宽 EID 读取

在 known-batch 基础上仅修改初始化阶段，仍调用公开的 HcommPeer/HcommJetty 构造函数：

1. 两卡测试中 peer 初始化由 warp 0 执行；Jetty 初始化从 thread 0 移至 warp 1 的 lane 0。保留原有 block 同步和 BeginBatch，因此后续消费者仍在两者完成后开始。
2. HcommPeer::Init 的 remoteEID 从 16 次 byte 读取/移位，改为原版使用的两次 uint64 读取。保留完整 16 字节及其他字段的读取，并静态检查 offsetof 对齐。

| 版本 | Dispatch 第1轮 / μs | 第2轮 / μs | 均值 / μs |
|---|---:|---:|---:|
| 同场原版 | 24.114 | 24.005 | 24.0595 |
| known-batch | 25.632 | 25.472 | 25.552 |
| known-batch-init | 24.740 | 24.813 | 24.7765 |

两项初始化调整合计减少 **0.7755 μs**。这组实验不将收益再拆成每一项的独立收益；它们的重叠关系会相互影响。更多 peer 的配置可能占用 warp 1，不能将两卡调度直接推广到任意 rank 数。

顺序 original → known-batch → known-batch-init → known-batch-init → known-batch → original，六次 exit=0，原有校验保留。没有额外时钟插桩。cached Dispatch 并未改善（21.3995 → 21.6405 μs），因此结论限定当前非 cached Dispatch 路径。

补丁：[known-batch-init.patch](known-batch-init.patch)。日志：[logs](logs)。JIT 预处理与对象验证：[instruction-summary.json](instruction-summary.json)。生成脚本 `prepare_known_batch_init.py`。
