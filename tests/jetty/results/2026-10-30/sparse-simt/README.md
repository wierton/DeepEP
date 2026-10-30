# SIMT 外层四点测量：初始化与请求构建仍有差距

与逐请求打点实验独立，重新从无插桩原版和 unchecked 副本生成。保留四个外层 `clock64()`：描述符初始化前、初始化同步后、整批请求构建前、整批请求构建/提交后。每个 warp 的 lane 0 在被测区间结束后写回结果；没有逐请求 probe。

采样路径将最终 Barrier 放在 epilogue，与发起 kernel 性能路径一致；没有删除最终通信等待。每版本两轮、每轮两 rank、每 rank 20 样本，四次运行均 exit=0，保留正确性校验和 stock profiler。

| 区间 | 原版 SIMT tick | unchecked SIMT tick |
|---|---:|---:|
| 描述符初始化至现有 block 同步完成 | 8309 | 15958 |
| 中间 histogram 发布 | 102 | 137 |
| 整批请求构建＋提交 | 4245 | 11728 |
| 合计 | 12656 | 27823 |

每个 core 取完整跨度最长的一个 warp，三段均来自它，再对 core、样本、rank 和轮次平均。各段可加，但这个统计不等于整个 kernel 关键路径：原版 scalar 还负责 AdvanceSq/doorbell，而 unchecked 的 AdvanceSq 在 SIMT 完成；原版请求区间结束没有新增 block barrier，unchecked 保留原来就有的 block barrier。表中的差异正包含这些编排差异，不能将其都解释为 WQE word 填充成本。

这说明删掉 ReserveBatchSq 的约 2000 tick 后，仍要调查初始化和请求构建/提交。下一步用无插桩的已知批次接口，将 final flag 前移到首次写 WQE，保留实际通信、发布和等待，检验回读/修改最后一条请求的影响。

原始日志：[logs](logs)。生成脚本 `prepare_sparse_simt.py`，解析脚本 `summarize_sparse_simt.py`。解析恢复 int64 tensor 中的 uint64 时钟位模式，并检查所有时间戳单调；不能把高位为 1 的时钟值判作负时间。

```bash
python3 tests/jetty/summarize_sparse_simt.py tests/jetty/results/2026-10-30/sparse-simt/logs
```

没有把 SIMT 与 scalar 时钟相减，也没有将不同版本的阶段差值直接折算成整体 kernel 收益。
