# 2026-10-29：Jetty 迁移验证与性能测量汇总

## 结论

- 原版与迁移版都在直接写 Host 映射内存时失败；不含通信的最小程序也能复现。
- 改为 Device 写计数、同 stream D2H 回拷后，默认 CPU 同步可以运行，未关闭结果校验。
- 原版对照和迁移版均通过两卡 18 模式正确性测试；迁移版非延迟 epilogue 也通过。
- 当前 BF16 两卡性能案例中，新版发起段增加约 1.6–4 μs，Dispatch 的 `urma` 段接近原版，Combine 的 `urma` 段缩短约 49 μs。
- 这是单机两卡结果，不代表多机、其他规模或端到端性能结论。

## 环境与版本

| 项目 | 配置 |
|---|---|
| 服务器 | `root@179.59.1.1`，工作目录 `/mnt/docker/o00939992` |
| 硬件 | x86_64，Ascend950PR；正确性测试使用卡 0/1，性能使用空闲卡 2/3 |
| CANN | Toolkit + 950 ops `9.3.0~weekly.20261021.01` |
| 驱动 | `25.7.rc1.6`，没有更换 |
| Python | 3.12.3，torch 2.7.1+cpu，torch_npu 2.7.1.post4 |
| 迁移版 | `wierton/DeepEP` 的 `migrate/asc-comm-jetty`，性能测试时 HEAD `c7b5e92` |
| DeepJIT | `wierton/DeepJIT`，`c966a2b`，修复 x86 的头文件发现路径 |
| 原版对照 | 官方 `deepseek-ai/DeepEP-Ascend` 的 `3b25377`，保留原版 Jetty 和 device kernels |

**原版对照不是完全未修改的源码**：两版使用相同构建环境、DeepJIT 路径修复、netlayer ID 0 和计数 D2H 兼容修改。
原版只有 netlayer 0、尚未添加计数修复时也失败。不能将修复后的对照称为“原版无需修改即可运行”。

性能脚本两版校验和相同：

```text
tests/ep/test_ep.py
6e8955089f54f147e0911cc359a148e8636da2d779736e6105d911d9724600d9
deep_ep/utils/testing.py
8ade9aa62c7cdef8d3cf061145bbc641cbd966a508a6d2be4c2dd6c9e3bc0c75
```

## 正确性与故障定位

| 测试 | 结果 | 日志 |
|---|---|---|
| Host 构建/import、Device 模板编译、4 个 CPU 调度模型测试 | 通过；不等同于 NPU 运行验证 | 详见 [完整记录](../../REMOTE_VALIDATION.md) |
| 最小程序：Host 映射地址作为 view/pointer 传入 SIMT，再写回地址值到 Device | 两种传参地址均正确，退出 0 | [地址验证](logs/host-writeback-addresses.log) |
| 最小程序：SIMT 写 Host 映射内存 | Stream 同步失败；plog 报 334 | [SIMT 写](logs/host-writeback-store.log) |
| 最小程序：Scalar 写 + DCCI；改用 HostRegisterV2 后 SIMT 写 | 均失败 | [Scalar](logs/host-writeback-scalar.log)、[V2](logs/host-writeback-v2.log) |
| 迁移版尚未修复计数回拷时，`--do-cpu-sync 0` 隔离测试，小/大案例 | 均退出 0；不能代替默认路径验证 | [小案例](logs/ep-layer0-no-cpusync-2rank.log)、[大案例](logs/ep-layer0-large-2rank.log) |
| 迁移版默认 CPU 同步，BF16 小案例 | 退出 0 | [小案例](logs/ep-count-copy-v2-2rank.log) |
| 原版 + 相同计数修复，BF16 小案例 | 退出 0 | [原版小案例](logs/baseline-count-copy-2rank.log) |
| 迁移版完整 18 模式，大案例，默认延迟 epilogue | 退出 0 | [迁移版](logs/ep-count-copy-all-modes-2rank.log) |
| 原版 + 相同计数修复，完整 18 模式，大案例 | 退出 0 | [原版](logs/baseline-count-copy-all-modes-2rank.log) |
| 迁移版完整 18 模式，大案例，非延迟 epilogue | 退出 0 | [非延迟](logs/ep-count-copy-immediate-2rank.log) |

小案例：128 tokens、hidden 512、top-k 2、4 experts。大案例：1024 tokens、hidden 4096、top-k 4、8 experts。均为两卡、`--num-ai-cores 16`。

18 模式：alignment 128/1；BF16/FP8；无 bias/单 bias/双 bias；FP8 scale 行/列布局。测试内部保留 cached handle 校验。
原版和迁移版分别对照测试的参考结果，并不是两次独立运行输出的逐位互比。

计数修复见 `1de9a92`：复用 `EPSignals::host_rank_expert_count` 的 Device 区域，在 Dispatch 后拷回 Host，Host 等拷贝完成事件。
它增加 D2H/event 开销，失去原先提前通知 Host 的重叠机会；**没有修复底层 Host 映射访问问题，也不保证性能等价**。
最初尝试单独普通 Device 分配时未满足 `EPSignals` 的 2 MiB 对齐，最终改为复用已对齐的 workspace；未放宽对齐要求。

## 性能测量方法

- BF16，大案例参数；alignment 128、无 bias，仅测首个模式；默认 CPU 同步和正确性校验均开启。
- 使用原仓 `bench_msprof`：每段预热 10 次、采样 50 次，清 L2，启用通信 barrier profiling。
- 顺序：原版第 1 轮 → 新版第 1 轮 → 新版第 2 轮 → 原版第 2 轮，全部退出 0。
- 每个 rank 的值是原脚本对 profiler 记录的平均值。下表每轮取两 rank 均值中较大者，再平均两轮。
- **不是逐迭代的跨 rank 最大值，也不是 Python API 端到端耗时。** 发起段与 `urma`/epilogue 来自切换 barrier 位置后的不同测量，不能直接相加当作总耗时。
- 脚本中的 GB/s 按逻辑数据量计算，包含本地数据；不能作为物理链路净带宽。
- 性能卡 2/3 在启动前为空闲；服务器其他卡上有其他用户进程，并非独占服务器。

### 汇总：单位 μs，越小越好

| 操作 | 阶段 | 原版 | 新版 | 新版相对变化 |
|---|---|---:|---:|---:|
| Dispatch | 发起 | 24.125 | 26.664 | +10.52% |
| Dispatch | `urma` | 222.623 | 224.008 | +0.62% |
| Dispatch | epilogue | 42.022 | 42.284 | +0.62% |
| Cached Dispatch | 发起 | 20.491 | 22.103 | +7.87% |
| Cached Dispatch | `urma` | 219.248 | 220.805 | +0.71% |
| Cached Dispatch | epilogue | 35.637 | 35.571 | −0.18% |
| Combine | 发起 | 34.610 | 38.588 | +11.50% |
| Combine | `urma` | 283.052 | 234.138 | −17.28% |
| Combine | epilogue | 19.543 | 19.501 | −0.21% |

原始每 rank 数据：[原版轮 1](logs/perf-baseline-1.log)、[新版轮 1](logs/perf-jetty-1.log)、[新版轮 2](logs/perf-jetty-2.log)、[原版轮 2](logs/perf-baseline-2.log)。
两轮不足以给出统计置信区间；约 1% 的差异不作显著性判断。Combine 差异在两轮均出现，但本次未隔离其具体成因。

## 复现性能测试

准备仓库和构建参见根目录 [JETTY验证流程.md](../../../../JETTY验证流程.md)。以下在待测源码根目录运行，两版依次执行，不能同时占用同一组卡：

```bash
source /mnt/docker/o00939992/Ascend/cann/set_env.sh
source /mnt/docker/o00939992/venv/bin/activate
export PYTHONPATH="$PWD"
export ASCEND_RT_VISIBLE_DEVICES=2,3 MASTER_PORT=29675
export TMPDIR=/mnt/docker/o00939992/tmp
export EP_JIT_CACHE_DIR="$PWD/.jit-cache"
export ASCEND_PROCESS_LOG_PATH="$PWD/logs/perf-ascend"
mkdir -p logs
timeout --kill-after=15s 300s python -u tests/ep/test_ep.py \
  --num-processes 2 --num-ai-cores 16 \
  --num-tokens 1024 --hidden 4096 --num-topk 4 --num-experts 8 \
  --dispatch-dtype bf16 --test-first-only > logs/perf.log 2>&1
echo "exit=$?"
```

本目录只归档文本 stdout/stderr，不含二进制 dump、库、模型或 profiler 原始大文件。
远端日志保留在 `/mnt/docker/o00939992/logs/`。退出码来自执行器记录，不是从日志中猜测。
