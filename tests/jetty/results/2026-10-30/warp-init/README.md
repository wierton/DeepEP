# 反证实验：warp 协作加载描述符不保证非 cached Dispatch 更快

在 dynamic-sge 基础上新增实验 API `LoadWarp`，由 warp 1 的 lanes 0..11 分担 JettyInfo 地址、计数和大小字段加载。调用前用三参数构造附着现有 UB，lane 0 初始化批次状态，warp 同步后设置已知批次总量，原有 block 同步保留。请求构建、AdvanceSq、doorbell、Drain 不变。

这不是把原版 88B 描述符直接 memcpy 给新 API：新 HcommJettyInfo 为 80B，成员布局不同，实验按字段正确映射，保留运行时 sqDepth。

无额外性能插桩，卡 2、3，正反两轮，六次 exit=0，原有结果校验保留。

| 版本 | Dispatch 均值 / μs | cached 均值 / μs |
|---|---:|---:|
| 同场原版 | 24.1875 | 20.547 |
| dynamic-sge | 24.1155 | 21.4385 |
| warp-init | 24.324 | 21.137 |

该实验对当前非 cached Dispatch **没有收益**，不能用“原版多 lane 加载”直接解释上一组剩余的 0.2 μs。同期原版也从上一组 23.914 变为 24.1875 μs，而 dynamic-sge 保持约 24.116 μs，说明这一级差距不稳定，不应把上一组的零点几微秒当成已经锁定的新根因。

cached 有约 0.30 μs 改善但仍未对齐，不能据此宣称所有路径已解决。此改动只保留作实验，不替换 dynamic-sge。

补丁：[warp-init.patch](warp-init.patch)，日志：[logs](logs)。生成脚本 `prepare_warp_init.py`。实际 JIT 预处理验证 LoadWarp 和动态 Write 调用存在，无 scalar/SIMT 性能插桩。
