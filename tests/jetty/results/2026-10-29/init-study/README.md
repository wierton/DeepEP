# Dispatch 差距定位：初始化调度、EID 读取、批次提交

## 先说已验证的结论

当前生产版本 `40fdb36` 已去掉启动 Drain。相同两卡测试中，原版 Dispatch 本体为 **24.111 μs**，当前版为 **26.122 μs**。
只恢复初始化重叠并改成宽 EID 读取后为 **25.513 μs**：缩小 **0.609 μs**，但没有消除全部差距。
这些修改暂时仅存在于实验副本，没有合入生产 kernel 或安装目录。

不能简单说“构造函数慢”：单独测 Jetty 构造并没有比原版加载慢。真正发现的是调度位置改变、peer 内部读取方式改变，以及后续请求/批次处理不同。

## 1. 为什么会失去初始化重叠

原版：[review/dispatch.hpp](../../../../../../DeepEP-Ascend-review/deep_ep/include/deep_ep/impls/ep/dispatch.hpp)。迁移版：[dispatch.hpp](../../../../../deep_ep/include/deep_ep/impls/ep/dispatch.hpp)。

```cpp
// 原版：当前两卡情况下，peer 加载在 warp 0，Jetty 加载在 warp 1。
if (thread_idx < kNumRanks && thread_idx != rank_idx)
    load_peer_info(...);
if (warp_idx == 1)
    load_jetty_info(..., lane_idx);
asc_syncthreads();

// 当前版：两项都在 warp 0，上下两个分支先后执行。
if (thread_idx < kNumRanks && thread_idx != rank_idx)
    HcommPeer(...);
if (thread_idx == 0)
    HcommJetty(...);
asc_syncthreads();

// overlap 实验：仍调用相同构造函数，改由 warp 1 的 lane 0 加载 Jetty。
if (warp_idx == 1 && lane_idx == 0)
    HcommJetty(...);
```

没有删除构造、没有构造空对象替代加载，也没有删除 block barrier。改变的是哪一个 warp 执行原有工作。
这个论证限定于当前两卡配置：更多 peer 可能占用 warp 1，不能把这项调度直接推广到任意 rank 数。

## 2. EID：16 次 byte load 与两次 uint64 load

当前安装的 SIMT 实现 `HcommPeer::Init`：

```cpp
for (uint32_t i = 0; i < 8; ++i) {
    low  |= uint64_t(remoteEID[i]) << (i * 8);
    high |= uint64_t(remoteEID[i + 8]) << (i * 8);
}
```

原版和 `wide-eid` 实验：

```cpp
const auto* eid = reinterpret_cast<__gm__ const uint64_t*>(remoteEID);
info->remoteEid[0] = eid[0];
info->remoteEid[1] = eid[1];
```

实验加了 `offsetof(SqContext, contextInfo.ubJfs.remoteEID) % 8 == 0` 的静态检查，在当前设备/布局上与原版一致。
仍加载完整 16 字节，仍调用 HcommPeer 构造函数，其他 token/base/size 读取不变。

不是源码层面的猜测：实际 kernel.o 中，control 有 **16 条 `LDG.C.NMFV.u8`**，wide-eid 对应区间改成 **两条 `LDG.C.NMFV.b64`**。
相同 `cached=false, final_barrier=true` 实例的摘录：[byte](instructions/peer-byte-load.asm)、[wide](instructions/peer-wide-load.asm)。
完整对象哈希与静态计数：[instruction-summary.json](timed/instruction-summary.json)。不要把静态条数当成整次 kernel 的动态执行次数。

## 3. AdvanceSq 额外读回最后一条 WQE

当前 API：`AdvanceSq → AdvanceBatch`。

```cpp
firstWord = GM[lastWqe];  // 先读回已经写出去的头
header = (firstWord & ~0xFFFFULL) | (end & 0xFFFFU) | FINAL_FLAGS;
GM[lastWqe] = header;
threadfence();
GM[sqHeadAddr] = packedHead;
```

原版首次写最后一条 WQE 时已经写入 final flags；scalar `advance_sq` 只更新 UB 的 packedHead，然后发布 doorbell。

`direct-header` 实验只把 firstWord 换成已知 Write 头常量：当前 Dispatch 只调用普通非 inline `Write`，FillSqWarpLane 写的是 `WRITE_HEADER | sqIdx`，而后续表达式恰好丢弃 sqIdx 的低 16 位，因此保留其他行为后等价。

保留：AdvanceSq 调用、末条头写回、完成标志、fence、packedHead 更新、doorbell，以及最终 Drain。
不适用于任意混合 opcode/inline/WriteWithNotify 批次；实验宏只对 Dispatch 翻译单元生效，不修改 Combine/通用安装库。

## 4. 分阶段数据：看得到局部差异，但不能直接相加

原版和当前版的详细边界见 [timing-detail](../timing-detail/README.md)。以下为等价改写实验的 SIMT 原始 tick；每项包含 2 轮、每轮 2 rank × 20 样本，control 为前后两批共 4 轮。

| 实验 | 初始化 | 请求构建 | 批次等待 | AdvanceSq | scalar 等 VF（另一时钟域） |
|---|---:|---:|---:|---:|---:|
| control | 23525 | 7624 | 279 | 1961 | 5429 |
| overlap | 12737 | 14696 | 832 | 3329 | 4226 |
| wide-eid | 12502 | 14769 | 1162 | 2990 | 3741 |
| both（前两项） | 10676 | 15762 | 1285 | 3148 | 3564 |
| direct-header | 23999 | 7430 | 249 | 1354 | 5550 |
| all（三项） | 10447 | 15955 | 1529 | 1729 | 3425 |

结论边界：

- 恢复重叠/宽读取明显缩短初始化，但请求构建区间同时变长。流水到达时刻、内存争用与编译器调度/插桩寄存器开销都可能影响后续区间；不能把初始化减少的 tick 当成等量 kernel 收益。
- direct-header 缩短 AdvanceSq，但 scalar 等待没有等量改善，不能据此宣称其单独解决了关键路径。
- 两类时钟不能互减，tick 未换算 μs。
- 相邻 clock64 的均值通常约 2 tick，但 direct-header 约 10 tick，进一步说明采样本身也受调度影响。
- 当前版“每请求准备”包括 SGE 构造、shuffle、远端边界检查；Write 内还包含容量检查、预留、word 构建和 fence。原版的分界不同，不能把 Write API 区间与旧版纯 store 区间做不加说明的一对一比较。

完整逐 rank/轮次数据：[timed/summary.json](timed/summary.json)。

## 5. 不带打点的完整 kernel 复测

使用原有 msprof 测量，10 次预热、50 次采样，正确性检查保留。每轮取两 rank 各自均值较大值，再平均两轮。不是逐迭代 rank max。
本体不包含末尾通信完成等待，但包含整个 kernel 的 SIMD/SIMT 工作，不是 Host launch。

第一组顺序：original:1 → control:1 → both:1 → both:2 → control:2 → original:2。

| 实验 | Dispatch 本体 μs | Cached Dispatch 本体 μs | Combine 本体 μs |
|---|---:|---:|---:|
| original | 24.111 | 20.571 | 34.688 |
| control | 26.122 | 21.665 | 38.804 |
| both | 25.513 | 21.850 | 38.675 |

初始化改写缩小普通 Dispatch 约 0.609 μs，但 cached 没有同样收益；Combine 没有被实验改写，列出作为波动参考，不能拿它的变化证明 Dispatch 优化。

第二组顺序：control:3 → direct-header:1 → all:1 → all:2 → direct-header:2 → control:4。
汇总全部 control 四轮、其他版本各两轮如下：

| 实验 | Dispatch 本体 μs | 含末尾通信等待 μs | Cached 本体 μs |
|---|---:|---:|---:|
| original | 24.111 | 222.222 | 20.571 |
| control | 26.080 | 223.713 | 21.671 |
| both | 25.513 | 224.509 | 21.850 |
| direct-header | 25.846 | 223.640 | 21.556 |
| all | 25.213 | 223.819 | 21.752 |

三项一起改后，本体较四轮 control 均值减少约 **0.867 μs**，但与原版仍差 **1.103 μs**。
不能声称已经解释全部差距，也没有证实含通信等待的整体加速。
对照波动、寄存器/调度改变和流水重叠使单因素收益不严格可加。

实际已确认到的层级是：初始化调度/peer 读取方式存在成本，最后 WQE 头读回存在成本；剩余差距仍包含请求准备、API 预留/填充/fence 与批次同步的耦合，不能凭当前数据把剩余 1.103 μs 指定给某一个函数或指令。
scalar 发布区间本身没有变慢，因此不支持“主要是 Host launch 或 scalar 按门铃慢”的说法。

18 次细分采样运行（原版/新版 4 次、等价改写 14 次）和 12 次无打点运行均退出 0。
四组/六组原始记录、两 rank 校验及正确性断言保留，没有用跳过调用的方法制造收益。
结果见 [stock/summary.json](stock/summary.json)；原始输出见 [stock/logs](stock/logs)。

## 6. 重现、文件和验证范围

实验均运行在 179.59.1.1 的 `/mnt/docker/o00939992`，卡 2/3、950PR、CANN 9.3.0 weekly 20261021.01。
1024 tokens，hidden 4096，top-k 4，8 experts，BF16，16 AI cores（32 AIV）；保留最终通信完成同步的细分采样。

```bash
# 先生成 timing-detail（命令见对应报告）；再生成以下独立副本。
python3 prepare_ablation.py /mnt/docker/o00939992 --init --timed
python3 prepare_ablation.py /mnt/docker/o00939992 --init --timed direct-header all
bash run_timing.sh jetty-init-timed 'control:1 overlap:1 wide-eid:1 both:1 both:2 wide-eid:2 overlap:2 control:2'
bash run_timing.sh jetty-init-timed 'control:3 direct-header:1 all:1 all:2 direct-header:2 control:4'

python3 prepare_ablation.py /mnt/docker/o00939992 --init
python3 prepare_ablation.py /mnt/docker/o00939992 --init direct-header all
# original 是原版 DeepEP-baseline 的独立副本。
bash run_no_start_drain.sh jetty-init-study 'original:1 control:1 both:1 both:2 control:2 original:2'
bash run_no_start_drain.sh jetty-init-study 'control:3 direct-header:1 all:1 all:2 direct-header:2 control:4'
```

每个副本独立 JIT 缓存。绝对头文件引用仅用于确保实验副本被编译器选中，对 control 同样处理；不是生产路径改动。
`verify_ablation_headers.py --init` 重放实际 JIT 命令预处理并反汇编；区分没有产物的 preflight 与已经编译的验证，不把两者混淆。

实验差异：[timed/patches](timed/patches)、[stock/patches](stock/patches)。生成器要求目标目录不存在，不覆盖既有结果。
安装头文件 SHA256 保持 `86175bfa9d9d92d18bd58c25fb8bd1f59f9813564a16cffee62a4b7ba58792e7`；远程生产 dispatch.hpp 与本地一致，没有被实验替换。

这是小规模两卡归因实验，不代表所有消息大小、rank 数、混合 opcode 或多机场景；不能据此直接合入通用 Jetty 优化。
