# DeepEP 的 asc-comm Jetty 迁移与测试结果

## 1. 测试数据

测试环境：950PR，2 ranks。耗时单位为 **μs**；取各 rank 平均耗时中的最大值，不是每次迭代先跨 rank 取最大值。数据来自 [2026-10-30 测试记录](tests/jetty/results/2026-10-30/full-suite/README.md)。

| 测试项 | 原版 DeepEP | Jetty 优化版 | 测试状态 |
|---|---:|---:|---|
| EP Dispatch 发起 | 24.081 | 24.302 | 正确性通过 |
| Dispatch URMA 等待 | 222.099 | 220.246 | 正确性通过 |
| Dispatch 收尾 | 42.168 | 42.195 | 正确性通过 |
| cached Dispatch URMA 等待 | 219.577 | 215.766 | 正确性通过 |
| cached Dispatch 收尾 | 35.633 | 35.682 | 正确性通过 |
| Combine URMA 等待 | 283.489 | 233.457 | 正确性通过 |
| Combine 收尾 | 19.525 | 19.467 | 正确性通过 |
| Engram fetch | 203.4 | 203.6 | 正确性通过 |
| Barrier，首轮 / 重测 | 10.742 / 8.189 | 8.531 / 9.003 | 正确性通过，存在轮次波动 |
| PP send | 1306.762 | — | 迁移版 Drain 断言失败，暂无有效性能结果 |
| PP recv | 67.699 | — | 同上，未到性能阶段 |
| Bucket AllGather | 23753.8 | — | 迁移版结果校验失败，暂无有效性能结果 |

EP 配置为 1024 tokens、hidden 4096、topk 4、8 experts、BF16、16 AI cores；PP 为 4096×7168 BF16，Bucket 为每 rank 1 GiB。

“发起”包括 kernel 内的路由、请求构造与发布，不是仅测 Host launch API。EP 的发起、URMA 等待和收尾来自不同 profiling pass，不能直接相加作为端到端时间。Bucket 计时包含 wait。

## 2. 改动分类：哪些直接替换，哪些必须适配

下面按原版 `3b25377` 到当前迁移代码的改动分类。这里的“直接替换”指原来的硬件封装职责已由 asc-comm 提供；不能仅凭函数相似就认定代码的复制来源。

### A. 原有重复封装：直接复用 asc-comm，不重写算法

| 改动点 | 原版实现 | 迁移方式 | 是否需要改变算法 |
|---|---|---|---|
| Peer / Jetty 描述符 | `comm/handle.hpp` 自带 `HcommPeerInfo`、`HcommJettyInfo` | 使用公开 `HcommJettyPeerInfo`、`HcommJettyInfo`，调整字段名、类型和 UB 空间计算 | 不需要；但不能假定所有字段的记账语义完全一致 |
| Peer / Jetty 初始化 | 本地构造函数和元数据加载函数解析 Channel | 调用公开 `HcommPeer`、`HcommJetty` 构造函数，继续使用调用者分配的 UB | 不需要；保留初始化完成后的同步 |
| SIMD 数据写入 | 本地 `put` | 调用公开 `Write` | 传输分工不变；地址参数转换另见 B 类 |
| SIMD 数据加通知 | 本地 `put_with_notify` | 调用公开 `WriteWithNotify` | 通知的目的和时机不变 |
| SIMD 完成等待、门铃 | 本地 `drain`、`ring_doorbell` | 调用公开 `Drain`、`RingDoorbell` | SIMD 生产路径可对应替换；SIMT 生产路径不能机械套用 SIMD Drain |
| 重复实现清理 | 本地维护 WQE 编码、CQ owner 轮询、SQ 环回及相关常量 | 删除 `comm/handle.hpp`、无效 include 和 `kSQEBBAlignment` 等旧实现依赖 | 不需要；这些职责转由库承担 |

这类就是“已有实现不要再维护一份，直接换成库调用”。主要落在 [PP](deep_ep/include/deep_ep/impls/pp/pp_send_recv.hpp)、[Bucket](deep_ep/include/deep_ep/impls/bucket/all_gather/urma.hpp)、[barrier](deep_ep/include/deep_ep/comm/barrier.hpp)；被删除的旧实现可用 `git show 3b25377:deep_ep/include/deep_ep/comm/handle.hpp` 查看。

### B. 接口语义不同：不能只改函数名

| 改动点与源码 | 为什么不能直接替换 | 实际适配 |
|---|---|---|
| [PP / Bucket 的远端地址](deep_ep/include/deep_ep/impls/pp/pp_send_recv.hpp) | 原 `put` 接受本地基址和对称地址，内部换算；公开 Write 接受实际远端地址 | 调用前计算 `remoteBaseAddr + offset`，并保留注册窗口的范围约束 |
| [Bucket 空闲 block](deep_ep/include/deep_ep/impls/bucket/all_gather/urma.hpp) | 原构造函数允许传空表；公开构造不能照搬这个约定 | 无任务 block 在此前同步之后退出，避免用空句柄表构造对象 |
| [SIMT 完成等待](deep_ep/include/deep_ep/comm/barrier.hpp) | 原版 SIMT 自己填请求、scalar 用本地 Drain 收尾；公开 SIMD/SIMT 的完成记账不能随意混用 | EP 的请求使用 SIMT Jetty，相应完成等待也使用 SIMT Drain；scalar 通过 VF_CALL 调用 |
| [Barrier 类型传递](csrc/kernels/comm/barrier.hpp) | 单看 scalar barrier 调用点，无法知道此前哪套 API 生产了请求 | EP context 设置 `use_simt_jetty`，经 Host 启动模板传到设备 barrier；其他 SIMD 路径保留原选择 |
| [Dispatch 收尾](deep_ep/include/deep_ep/impls/ep/dispatch_copy_epilogue.hpp)、[Combine 收尾](deep_ep/include/deep_ep/impls/ep/combine_reduce_epilogue.hpp) | epilogue 同样会等待此前 EP 的请求，不能只修改主 kernel 的 barrier | 两处实例化也指定 SIMT 完成路径 |

### C. 算法里直接写硬件请求：保留数据组织，重写提交部分

| 改动点与源码 | 原版如何实现 | 迁移后由谁负责什么 |
|---|---|---|
| [Dispatch 请求生成](deep_ep/include/deep_ep/impls/ep/dispatch.hpp) | warp 的各 lane 直接生成 WQE header、peer 字段和 SGE word，再写 SQ | DeepEP 继续算路由、逻辑 slot 和数据分组；改为每 lane 提供 SGE，公开 SIMT Write 路径负责槽位申请和编码 |
| 同文件 Dispatch 发布 | 原版按请求数自己推进物理 SQ head，再按门铃 | 由 Jetty 收尾；DeepEP 仍在 payload 搬运和 SIMT 请求生成完成后发布，不能直接默认每条 Write 立即发布 |
| [Combine 请求生成](deep_ep/include/deep_ep/impls/ep/combine.hpp) | `load_meta_issue_wqe` 处理 metadata 的同时逐 word 写 WQE | 保留 metadata 与计算流程，用 `Write<2, false>` 描述 hidden 和 weight 两段源数据 |
| 同文件 Combine 流水发布 | 原版自行计算每个 task 对应的物理 SQ 结束位置 | 保存每个 stage 的 `task_heads`，分开生产侧 `jetty_info` 与发布侧 `publish_info`，只发布 payload 已完成的任务前缀 |

这类改动不是“把一个函数名替换成另一个”：需要把原来混在算法里的硬件编码拆出去，同时保持原来的数据依赖、流水与发布时机。第 3、4 节分别展开。

### D. 构建和运行环境适配：不是 Jetty 算法改写

| 改动点与源码 | 改了什么 | 原因 |
|---|---|---|
| [JIT include](csrc/runtime/jit.hpp) | 在 Config 中加入安装目录的公开 Jetty 及依赖头文件路径 | 设备 kernel JIT 编译时需要找到库接口，仍由 DeepJIT 使用 Ascend 编译器 |
| [Host 扩展构建](setup.py) | 补公开头路径，并按 `platform.machine()` 选择架构目录 | 处理安装布局及 Host 架构，不是新加一套通信实现 |
| [网络层选择](csrc/kernels/comm/hccl.hpp) | 从原来的 `layers[1]` 改为检查并使用 netlayer ID 0 | 对应测试环境的实际链路；不意味着所有机器都应固定为 0 |
| [Host 计数回传](csrc/buffers/ep.hpp) | kernel 写 Device workspace，必要时异步 D2H 拷回 Host，再等待该拷贝完成 | 替代原来的 mapped Host workspace 路径；不是 Jetty API 替换，也不是网络传输算法变化 |

因此，迁移工作可以概括为：**重复硬件封装直接删除复用；参数与完成语义差异做适配；算法内裸 WQE 生成改成数据描述调用；环境兼容改动单独标识。**

### 迁移边界

DeepEP 原来有两种使用 Jetty 的方式：

- **EP Dispatch / Combine**：SIMT 线程直接拼 WQE 的各个 word，写入硬件 SQ，再由 scalar 路径发布。
- **PP / Bucket**：调用 DeepEP 自带的 `handle::HcommJetty`，由这套本地封装构造 WQE、按门铃和处理 CQ。

迁移后统一使用 asc-comm 的公开 Jetty API：

```text
DeepEP 保留：token 路由、目标 slot、数据分组、计算流水、发送时机
    ↓ 提供 peer、远端地址、源数据段 SGE
asc-comm Jetty：申请 SQ 槽位、编码 WQE、提交和完成记账
    ↓
通信硬件：搬运数据、产生完成信息
```

这不是把通信转交给 Host 或 AICPU。请求仍在设备 kernel 中生成，只是不再由 DeepEP 维护一份硬件 WQE 编码实现。

## 3. Dispatch：由按 word 拼 WQE，改为按 lane 提供 SGE

源码：[dispatch.hpp](deep_ep/include/deep_ep/impls/ep/dispatch.hpp)。

原来 DeepEP 先统计每个 peer 的 token 数，分配逻辑请求区间；随后每个 warp 处理一条请求，各 lane 根据编号生成 header、EID、token、源地址和长度等 word，直接写 SQ。

现在仍保留 histogram、prefix sum、peer 去重和逻辑 slot 分配。改变的是最后一层：**lane 提供数据描述，Jetty 负责硬件编码。**

```text
lane 0：token 0 的 payload SGE
lane 1：token 0 的 metadata SGE
lane 2：token 1 的 payload SGE
lane 3：token 1 的 metadata SGE
……
一个 warp 合作提交一条多 SGE Write
```

当前调用主干如下，省略边界检查和地址计算：

```cpp
// 已知本批请求总量，统一预检所需 SQ 空间。
jetty.BeginBatch(ub_layout->lsqe_counter * kNumWQEBBsPerSQESlot);

// 每个 lane 提供一个源数据段，而不是一个 WQE word。
AscendC::simt::jetty::HcommSge sge;
sge.addr = /* 本 lane 对应 payload 或 metadata 地址 */;
sge.len  = /* 对应字节数 */;
jetty.WriteDynamic(warp, peer, dst, sge, pairs * 2U);

// 全部生产者结束后，再由一个线程收尾。
asc_syncthreads();
if (thread_idx == 0 && num_sqes > 0) jetty.AdvanceSq();
```

scalar 侧等待 payload 搬运及 SIMT 请求生成完成，再调用 `publisher.RingDoorbell()`。没有在每个 warp 构造完请求后立即发布，避免硬件提前读取尚未准备好的 payload。

上述代码使用配套 asc-comm 头文件中的 `BeginBatch`、`WriteDynamic`；它们不是所有已发布版本都有的接口。

## 4. Combine：保留计算流水，替换请求生成与收尾

源码：[combine.hpp](deep_ep/include/deep_ep/impls/ep/combine.hpp)，重点看 `load_meta_issue_wqe` 和 `combine_impl`。

原来 `load_meta_issue_wqe` 在 SIMT 中处理 metadata，并直接生成每个 token 的 WQE；scalar 侧负责规约、MTE 搬运和分批发布。

现在 metadata、规约和流水结构保留，WQE 构造改为两个 SGE 的公开 Write：

```cpp
// lane 0：hidden 数据；lane 1：topk weight。
jetty.Write<2, false>(AscendC::simt::HcommCoopWarp{}, peer, dst, sge);

asc_syncthreads();
if (thread_idx == 0 && issue_write) {
    jetty.AdvanceWriteSq();
    ub->task_heads[stage_idx] = ub->jetty_info.packedHead;
}
```

`AdvanceWriteSq` 是配套头文件中用于普通 Write 批次收尾的接口；不能用于任意 inline、notify 混合批次。

这里不能简单把“构造请求”和“立即发布”合并：流水预取可能已经生成下一任务的请求，但下一任务的 payload 尚未完成。因此保留：

- `jetty_info`：生产侧状态，可能包含预取任务。
- `task_heads[stage_idx]`：每个流水任务结束处的队列位置。
- `publish_info`：发布侧描述符，只把当前已准备好数据的前缀交给硬件。

scalar 等待相应 MTE3 完成后，用 `publish_info` 按门铃；需要回收时也只等待已发布的前缀，不能等待尚未发布的未来任务。

## 5. PP 与 Bucket：替换原有封装调用

| 位置 | 原来怎么用 | 迁移后怎么用 |
|---|---|---|
| [PP send](deep_ep/include/deep_ep/impls/pp/pp_send_recv.hpp) | 本地 `put_with_notify` 发送数据并通知对端 | `AscendC::HcommJetty::WriteWithNotify`，随后 `Drain` |
| 同文件 PP recv 控制路径 | 本地 `put` 回写 release 信息 | `Write` 回写，然后 `Drain`；并不是改成硬件 Receive 请求 |
| [Bucket AllGather](deep_ep/include/deep_ep/impls/bucket/all_gather/urma.hpp) | 本地 `put` 按 peer、按 chunk 推送数据 | 公开 `Write`，保留原来的分块及 peer 分工 |

地址参数也有差别：原有封装接受本地对称地址和本地基址，在内部换算远端地址；公开 Write 接受实际的远端地址。因此迁移后显式计算：

```text
remoteDst = peerInfo.remoteBaseAddr + 数据在注册窗口中的 offset
```

对端地址和权限信息来自 Channel 元数据，不是把本地指针原样发给远端，也不是由应用自行拼 token 字段。PP、Bucket 当前仍有上表列出的验证失败，接口替换完成不等于这两条路径已经验收通过。

## 6. 完成等待：与生产请求所用的接口配套

源码：[设备 barrier](deep_ep/include/deep_ep/comm/barrier.hpp)、[Host barrier 启动](csrc/kernels/comm/barrier.hpp)。

原版由 scalar barrier 遍历分配给当前 AIV 的 Jetty，调用本地 `drain`。迁移后，EP 用公开 SIMT API 生产请求，就采用相应的 SIMT `Drain` 消费完成；PP/Bucket 的 SIMD 路径仍用 SIMD `Drain`。

```text
EP 的 SIMT 请求 → scalar barrier → VF_CALL → SIMT Drain
SIMD 请求      → scalar barrier → SIMD Drain
```

`use_simt_jetty` 表示选择哪套完成处理方式，**不是说 barrier 一定由 SIMT 调用**。scalar 本身不能直接执行 SIMT 函数，所以前一条路径用 VF_CALL 桥接。Jetty 完成等待之后，仍由 DeepEP 原有信号逻辑完成 rank 间 barrier。

## 7. 初始化、编译依赖与代码归属

Host 建连入口为 [hccl.hpp](csrc/kernels/comm/hccl.hpp) 的 `acquire_jetties`：创建 endpoint、注册 buffer、建立 Channel，再把 `channel_handles` 拷到 Device。名字虽然叫 `jetty_device_ptrs`，其中实际存放的是设备可访问的 ChannelHandle 表。

kernel 中 `HcommPeer`、`HcommJetty` 的资源绑定构造函数从这些 Channel 加载描述符到调用者的 UB。**构造临时 C++ 对象并非什么都没干：UB 中初始化好的队列、对端元数据会继续被后续代码使用。**

JIT 配置位于 [jit.hpp](csrc/runtime/jit.hpp)：仍由 DeepJIT 调用 Ascend 编译器，只扩展 include 搜索路径以使用公开 Jetty 头文件。设备侧 Jetty 热路径主要在头文件内展开，不是每发一次 Write 都调用 Host 动态库。

DeepEP 原先重复维护的 Jetty 实现已删除；算法层保留在 DeepEP，通用硬件请求实现归 asc-comm。联动源码入口及依赖见 [源码提交说明](tests/jetty/results/2026-10-30/submitted-source/README.md)。
