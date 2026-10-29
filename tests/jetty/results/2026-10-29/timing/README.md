# 保留完整流程：原版 / asc-comm Jetty 调用区域打点

这次不是删调用实验。原版和迁移版各自保留全部操作、校验、通信和最终 barrier，
只添加时间戳和记录区。之前 `ablation/` 的删除实验不能作为等价实现的性能证明。

## 结果

卡 2/3，BF16，1024 tokens、hidden 4096、top-k 4、8 experts，16 AI cores / 32 AIV。
顺序 original-1 → migrated-1 → migrated-2 → original-2，四次退出码均为 0。
每次每 rank 5 次预热、20 次样本；每次样本前清 256 MiB L2 buffer 并同步。
两版的结果校验都保留；CPU 计数回拷兼容修改、netlayer 0 与前面测试一致。
original 是 3b25377 加相同环境兼容修改，不是完全未改的 upstream checkout。

以下均为**原始 tick**，不混用两个时钟域，也不根据假定频率换算 μs。

| 区域 | 时钟 | 原版 | 迁移版 | 边界与含义 |
|---|---|---:|---:|---|
| Jetty/peer 初始化 | SIMT clock64 | 7,944.43 | 21,301.75 | peer 和 Jetty 描述信息加载，到原有初始化同步结束；迁移版包含它原本已有的启动 Drain |
| 整批请求构建 | SIMT clock64 | 3,654.89 | 5,887.43 | 原版直接拼 WQE；新版生成 SGE、调用 Write；包含各自原有槽位分配、校验、循环 |
| SIMT 批次收尾 | SIMT clock64 | 不在此执行 | 2,899.35 | 新版 thread 0 等其他 warp 后执行 AdvanceSq；包含原有线程块同步 |
| scalar 等待 VF 结束 | systemcycle | 73.68 | 4,084.36 | 最后 V→S notify/wait 区域；是 scalar 与 SIMT 协同的暴露等待，不是一个 Jetty 函数本身 |
| scalar SQ 发布区域 | systemcycle | 543.73 | 473.05 | 原版 advance_sq + ring_doorbell；新版 publisher 构造 + RingDoorbell，收尾已搬到 SIMT |
| 最终通信 barrier | systemcycle | 199,113.76 | 197,387.15 | 包含 drain、跨 core/rank 同步等，不是纯 Drain 时间，不能由这行宣称网络更快 |

每个样本先在 core 内取 active warp 的构建时间最大值，再对 32 core 平均；
初始化取每个 core 的所有 warp 最大值；SIMT 收尾只取执行 AdvanceSq 的 thread 0。
最后平均样本、rank 和两轮。不是全机关键路径 max，也不是单个 WQE 的服务时间。
每 rank 每次请求数量两版一致：rank 0 共 188 条、rank 1 共 185 条。

**这次区域测量支持的定位**：迁移版初始化与请求构建区域更慢；额外 SIMT 收尾后，
scalar 等待 VF 的时间明显增加。scalar 发布区域没有变慢。
初始化区域的 2.68 倍差异不能全部归给 Drain：其中还包括原版 warp 协作加载
描述信息与新版单线程 Jetty 构造等差异，需要继续细分才能归因到一个 API。

## 打点具体放在哪里

源文件都是 `deep_ep/include/deep_ep/impls/ep/dispatch.hpp`。
精确改动见 [原版 patch](patches/original.patch)、[迁移版 patch](patches/migrated.patch)。

```cpp
// 两版同样的区域边界，未删除被测工作。
const uint64_t init0 = clock64();
// 原版：load_peer_info / warp load_jetty_info
// 新版：HcommPeer / HcommJetty 构造 / 已有 Drain
// 各自原有初始化同步
const uint64_t init1 = clock64();

const uint64_t build0 = clock64();
// 原版：完整 WQE 构建循环
// 新版：完整 SGE + Write 循环
const uint64_t build1 = clock64();
// 新版原有的 block sync + thread 0 AdvanceSq
const uint64_t finish1 = clock64();

// scalar：asc_get_system_cycle() 分别包住 VF join、SQ 发布、最终 barrier。
```

没有新增等待指令来强制改变被测区域的并发。这里测的是保留原有同步边界的执行区间，
不是把每次写 SQ 都强制等待到硬件消费完成。
原版没有 SIMT finalize，记录 `finish1 = build1`，表中明确写“不在此执行”，不是零成本。

反汇编确认 SIMT 打点是 `S2RL ..., CLOCK`，scalar 是 `MOV ..., SYS_CNT`。
例如 migrated 的 init0/init1 在 `0x2bb0/0x3890`，build0/build1 在 `0x39b0/0x5c50`，
finish1 在 `0x5f40`；对应 kernel cache 为
`dispatch.5cc78a6a0d9d7cf88401490cb7c5aba7`。
相邻两次 SIMT clock64 的差约 2 tick，仅说明裸读计数器开销小；
**不证明整个打点零扰动**，额外寄存器和记录写入仍会影响编译及调度。

## 记录方式与限制

- 输入 tensor 的逻辑 shape、内容不变，在其底层 allocation 尾部另留约 68 KiB 记录区；
  不修改 kernel ABI，不重编译 Host 扩展，不借用通信 buffer。
- 每个 core/warp 各写自己的槽位，没有记录区锁或共享 atomic。
- SIMT 的记录写入在被测构建/收尾区域之后；scalar 的记录在最终 barrier 后，使用
  bypass-DCache 写入。所有时间戳在 kernel 完成后 D2H，再输出日志。
- 原始 uint64 位模式通过 torch.int64 回传；解析时恢复 unsigned，不将负数误判为无效时钟。
- parser 校验每轮两个 rank、各 20 样本、时间戳单调、最终 barrier 开启，以及 scalar/SIMT
  记录的请求数量一致。日志合流可能没有换行，按完整 JSON 对象解析，不按一行一个 rank 假设。
- SIMT 与 scalar 有重叠，不能把表格逐行相加去解释先前 profiler 测到的约 2.54 μs 差值。
- 未修改测量以外的 Jetty 行为；这次没有把接口优化到原版实现，只执行用户提出的第二条路线：打点比较。

## 复现与原始数据

脚本位于 `tests/jetty/prepare_timing.py`、`run_timing.sh`、`summarize_timing.py`。
服务器目录：`/mnt/docker/o00939992/jetty-timing-v3/`。
准备脚本复制已有的 DeepEP-baseline 和 DeepEP-Ascend（均含已构建 Host 扩展），不修改原目录。

```bash
venv/bin/python prepare_timing.py /mnt/docker/o00939992
bash run_timing.sh
venv/bin/python summarize_timing.py /mnt/docker/o00939992/jetty-timing-v3/logs
```

[summary.json](summary.json) 保存每 rank/轮统计；`logs/*.log.gz` 是可无损解压的完整
原始时间戳日志，避免将约 15 MiB 未压缩重复文本直接提交。
本地可运行 `python3 tests/jetty/summarize_timing.py tests/jetty/results/2026-10-29/timing/logs` 重算。
