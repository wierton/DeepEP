# 移除 Dispatch 启动 Drain 后复测

代码修改：`de4971d`。仅删除迁移时在 Jetty 初始化后增加的 `Drain()`；保留描述信息加载、SQ 容量检查、发送操作和最终完成等待。对照版本为 `1ad38a0`。

## 环境与流程

- 服务器：179.59.1.1，950PR，使用卡 2、3；Toolkit 9.3.0 weekly 20261021.01。
- 远程目录：`/mnt/docker/o00939992/jetty-no-start-drain`，before/after 为独立副本，分别使用独立 JIT 缓存。
- 两份副本仅 dispatch.hpp 的上述调用不同，没有修改安装目录的 Jetty 头文件，也没有插入时间戳。
- 两卡、1024 tokens、hidden 4096、top-k 4、8 experts、BF16、16 AI cores；`--test-first-only`。
- 运行脚本：[run_no_start_drain.sh](../../../run_no_start_drain.sh)。顺序 before:1 → after:1 → after:2 → before:2。
- 四次退出码均为 0；原测试 Dispatch、cached Dispatch、Combine 的正确性检查保留并通过。不是完整模式矩阵回归，也不是多机验证。
- 每项 10 次预热、50 次采样。每轮取两个 rank 各自均值的最大值，再平均两轮；不是每一轮迭代的 rank 最大值平均。
- 本体：关闭 prologue 末尾通信完成 barrier 的整个 kernel 时间，不是单独的 Host launch 或 SQE 填充时间。
- 包含等待：开启 prologue 通信完成 barrier 的 kernel 时间，不包括 epilogue 和 Host/D2H 开销。

## 结果

单位 μs。

| 操作 | 修改前本体 | 修改后本体 | 修改前包含等待 | 修改后包含等待 |
|---|---:|---:|---:|---:|
| Dispatch | 26.566 | 26.170 | 222.487 | 223.984 |
| Cached Dispatch | 21.891 | 21.652 | 220.786 | 219.938 |
| Combine（未修改） | 38.682 | 38.688 | 234.029 | 234.010 |

Dispatch 本体减少 0.396 μs（约 1.5%），但包含等待增加 1.498 μs（约 0.7%）。本次没有证实完整 Dispatch 通信收益，不能把移除 Drain 称为整体加速。

| 轮次 | Dispatch 本体 | Dispatch 包含等待 |
|---|---:|---:|
| before:1 | 26.645 | 222.323 |
| after:1 | 26.039 | 224.203 |
| after:2 | 26.301 | 223.765 |
| before:2 | 26.487 | 222.650 |

原始输出在 [logs](logs)，可复算：

```bash
python3 tests/jetty/summarize_ablation.py tests/jetty/results/2026-10-29/no-start-drain/logs
```

此次通过仅覆盖上述串行测试流程，不能据此证明任意未完成批次重叠、多生产者或多机场景安全。远程正式工作副本的 dispatch.hpp 也已同步为去掉启动 Drain 的版本。
