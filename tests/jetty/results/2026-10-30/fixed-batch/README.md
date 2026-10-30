# ReserveBatchSq 精简：真机对照结果

## 结论

2026-10-30，在远端 950PR 的物理卡 2、3 上，三种版本各跑两轮，六次运行全部 exit=0，保留原测试的结果校验。删除逐条容量检查和 `atomic_max` 后，**没有观察到 Dispatch 整体性能收益**。

| 版本 | Dispatch 第1轮 / μs | 第2轮 / μs | 两轮均值 / μs | 带通信等待的两轮均值 / μs |
|---|---:|---:|---:|---:|
| 原版 DeepEP | 24.083 | 24.041 | 24.062 | 222.502 |
| 迁移版修改前 | 25.990 | 26.139 | 26.065 | 223.839 |
| 精简 ReserveBatchSq | 26.239 | 26.194 | 26.217 | 224.227 |

精简后 Dispatch 比修改前多 0.152 μs（约 0.58%）；两轮不足以证明这种小幅变化具有统计显著性，但不能宣称优化有效。cached Dispatch 均值从 21.671 到 21.602 μs，也只有小幅变化。不能将之前单条请求插桩得到的 tick 差值等同于整段 kernel 的收益。

口径：每轮分别取两个 rank 的 profiler 均值中的较大者，再对两轮求平均；**不是每次迭代跨 rank 取最大值后平均**。`dispatch` 是测试中不带最终等待的 kernel 计时，`urma` 是带等待路径；两者不是 CPU launch API 耗时。

## 具体改动

### 同边界局部打点复测

随后复用了之前 `native-reserve` / `api-reserve` 的相同插桩，只将后者复制成 `api-reserve-unchecked` 并实施上述精简；未移动计时边界。顺序为原版、修改前、修改后、修改后、修改前、原版，仍用卡 2、3。六次均 exit=0。

| 申请区间 | 每请求平均 SIMT tick |
|---|---:|
| 原版 DeepEP | 368.270 |
| asc-comm 修改前 | 2367.575 |
| asc-comm 精简后 | 329.630 |

修改前比原版多 1999.305 tick；精简后比修改前少 2037.945 tick，已经不再存在该区间约 2000 tick 的差距（本次比原版少 38.640 tick）。这验证了局部收益，但不改变上面无额外插桩的整体 Dispatch 未见收益的结论。

边界：原版从物理 SQE 申请之前到 psqe_idx 广播之后；API 从 PostSend 的 Reserve 注释之前到 FillSqWarpLane 之前，包含 lane 0 申请、广播和失败判断。每个版本 2 轮 × 2 rank × 20 样本；每个 core 将各 warp 的区间累计时间除以请求数，再按 core、样本、rank/轮次平均。不是 kernel 关键路径或所有请求串行总时间。

插桩版本还观察到请求构建区间从约 13696 降至 8719 tick，而初始化区间从约 16638 增至 21071 tick。编译后的寄存器和调度可能受影响；不能未经进一步差分验证就解释成确定的时间转移机制，也不能对不同编译版本的各段 tick 简单求和。

原始数据：[timing/logs](timing/logs)。补丁：[api-reserve-unchecked.patch](timing/api-reserve-unchecked.patch)。实际 JIT 头文件选择和对象哈希：[timing/instruction-summary.json](timing/instruction-summary.json)。分析命令：

```bash
python3 tests/jetty/summarize_timing.py tests/jetty/results/2026-10-30/fixed-batch/timing/logs --api
```

生成脚本 `prepare_unchecked_timing.py` 复用已有 `jetty-init-timed/api-reserve`，重写副本内部的绝对 include 路径，保证仍使用各自独立的头文件和 JIT cache。

只在 Dispatch 翻译单元的独立头文件副本中启用：

```cpp
uint32_t JettyImpl::ReserveBatchSq(uint32_t reserveWqebbs) const {
    if (stageAddr_ == 0U) return 0xFFFFFFFFU;
    auto* info = Info();
    auto* batch = Batch();
    uint32_t offset = asc_atomic_add(&batch->wqebbCount, reserveWqebbs);
    uint32_t base = static_cast<uint32_t>(info->packedHead);
    return base + offset;
}

// AdvanceBatch：所有生产者完成后，用固定槽位大小定位最后一条。
const uint32_t last = begin + count - HCOMM_SIMT_SLOT_WQEBBS;
```

去掉了完成进度 GM 读取、逐条容量检查、lastOffset 的 atomic_max 和该检查对应的失败分支。保留原有请求构建、API 调用、协作同步、最后一条头部回读/更新、fence、SQ head 发布、doorbell、完成等待。没有同时合入先前 EID、初始化调度、直接构造 final header 等实验。

适用范围：当前 Dispatch warp Write 的请求固定占 4 个 WQEBB；`PostSend` 的 `kDeepEpWarpWrite` 分支明确选择这个固定槽位大小。本实验不推广到变长/混合请求，不证明删除容量检查对任意输入均安全。Combine 和独立 Barrier 不启用该宏。系统安装文件和生产源码未覆盖。

完整补丁：[unchecked-fixed-batch.patch](patches/unchecked-fixed-batch.patch)。其中绝对 include 路径仅用于强制实验副本参与 JIT；control 同样使用独立副本，避免编译器优先选中安装头文件。

## 编译与校验依据

实际 JIT 命令重放预处理，确认 ReserveBatchSq 中无 completionTailAddr、requiredFreeWqebbs、atomic_max，AdvanceBatch 使用 count - SLOT_WQEBBS。四个 Dispatch 实例（cached true/false × final barrier true/false）均验证成功。

[instruction-summary.json](instruction-summary.json) 保存对象哈希和反汇编静态计数：修改前每个实例有 6 处所统计的 `RED.S.MAX.u32.NMFV`，修改后为 0。各对应实例的 MEMBAR 数保持一致。静态指令数不等于动态执行次数。

安装头文件测试前 SHA256：`86175bfa9d9d92d18bd58c25fb8bd1f59f9813564a16cffee62a4b7ba58792e7`。

## 测试配置和复现

- 远端工作根目录：`/mnt/docker/o00939992`。
- 环境：该目录下 `Ascend/cann/set_env.sh`、`venv/bin/python`。
- 2 ranks、16 AI cores、1024 tokens、hidden 4096、topk 4、8 experts、BF16。
- 原测试 `--test-first-only`，10 warmups、50 次性能采样；这不是全参数正确性覆盖。
- 顺序：原版 → 修改前 → 修改后 → 修改后 → 修改前 → 原版。
- 独立 JIT cache；原始 stdout/stderr 在 [logs](logs)，六次均正常退出。

在新实验目录尚不存在的远端运行：

```bash
python3 prepare_ablation.py /mnt/docker/o00939992 --init fixed-control unchecked-fixed-batch
ASCEND_RT_VISIBLE_DEVICES=2,3 bash run_fixed_batch.sh
```

本地汇总：

```bash
python3 tests/jetty/summarize_ablation.py tests/jetty/results/2026-10-30/fixed-batch/logs
```

服务器上其他卡同时有其他任务，因此不能排除共享主机环境的影响。本次结果只支持“这一改动在当前配置没有缩小整体差距”，不直接判定剩余开销的根因。
