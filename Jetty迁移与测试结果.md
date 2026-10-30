# DeepEP 通信接口迁移及 EP 性能测试报告

本项目将 DeepEP 自维护的 Jetty 硬件请求实现迁移至 asc-comm 公开接口，在保留 token 路由、数据分组和计算通信流水的基础上，统一通信请求构造、提交及完成处理。改造范围涵盖通用接口复用、接口语义适配、请求提交改造及构建环境集成。

## 1. EP 性能测试结果

测试环境：950PR，2 ranks。耗时单位为 **μs**，统计口径为各 rank 平均耗时的最大值。数据来源：[2026-10-30 测试记录](tests/jetty/results/2026-10-30/full-suite/README.md)。

| 测试项 | 原版 DeepEP | Jetty 优化版 |
|---|---:|---:|
| EP Dispatch 发起 | 24.081 | 24.302 |
| Dispatch URMA 等待 | 222.099 | 220.246 |
| Dispatch 收尾 | 42.168 | 42.195 |
| cached Dispatch URMA 等待 | 219.577 | 215.766 |
| cached Dispatch 收尾 | 35.633 | 35.682 |
| Combine URMA 等待 | 283.489 | 233.457 |
| Combine 收尾 | 19.525 | 19.467 |

EP 配置为 1024 tokens、hidden 4096、topk 4、8 experts、BF16、16 AI cores。

发起阶段覆盖 kernel 内的路由、请求构造与发布。各阶段分别采样，表中数据为阶段性指标，不作为端到端耗时累加。

## 2. 迁移范围与改造分类

以DeepEP原版 `3b25377` 为对照，迁移改动分为四类：通用封装直接替换、接口语义适配、算法内请求提交改造、构建与运行环境适配。

### 2.1 通用通信接口复用

| 改动点 | 原版实现 | 实施内容 | 算法影响 |
|---|---|---|---|
| Peer / Jetty 描述符 | `comm/handle.hpp` 自带 `HcommPeerInfo`、`HcommJettyInfo` | 使用公开 `HcommJettyPeerInfo`、`HcommJettyInfo`，调整字段名、类型和 UB 空间计算 | 保持数据组织，适配完成记账 |
| Peer / Jetty 初始化 | 本地构造函数和元数据加载函数解析 Channel | 调用公开 `HcommPeer`、`HcommJetty` 构造函数，继续使用调用者分配的 UB | 保持初始化同步关系 |
| SIMD 数据写入 | 本地 `put` | 调用公开 `Write` | 保持传输分工，适配远端地址参数 |
| SIMD 数据加通知 | 本地 `put_with_notify` | 调用公开 `WriteWithNotify` | 通知的目的和时机不变 |
| SIMD 完成等待、门铃 | 本地 `drain`、`ring_doorbell` | 调用公开 `Drain`、`RingDoorbell` | SIMD 路径对应替换，SIMT 路径单独适配 |
| 重复实现清理 | 本地维护 WQE 编码、CQ owner 轮询、SQ 环回及相关常量 | 删除 `comm/handle.hpp`、无效 include 和 `kSQEBBAlignment` 等旧实现依赖 | 硬件队列维护职责归入通信库 |

涉及源码：[PP](deep_ep/include/deep_ep/impls/pp/pp_send_recv.hpp)、[Bucket](deep_ep/include/deep_ep/impls/bucket/all_gather/urma.hpp)、[barrier](deep_ep/include/deep_ep/comm/barrier.hpp)。该类改造消除重复硬件封装，降低应用侧对 WQE 格式和队列维护细节的依赖。

### 2.2 接口语义适配

| 改动点与源码 | 接口差异 | 实施内容 |
|---|---|---|
| [PP / Bucket 的远端地址](deep_ep/include/deep_ep/impls/pp/pp_send_recv.hpp) | 原 `put` 接受本地基址和对称地址，内部换算；公开 Write 接受实际远端地址 | 调用前计算 `remoteBaseAddr + offset`，并保留注册窗口的范围约束 |
| [Bucket 空闲 block](deep_ep/include/deep_ep/impls/bucket/all_gather/urma.hpp) | 原构造函数允许空句柄表，公开构造要求有效资源 | 无任务 block 在此前同步之后退出，避免用空句柄表构造对象 |
| [SIMT 完成等待](deep_ep/include/deep_ep/comm/barrier.hpp) | 原版 SIMT 构造请求、scalar 使用本地 Drain；公开 SIMD/SIMT 采用不同完成记账 | EP 采用配套 SIMT Drain，scalar 通过 VF_CALL 调用 |
| [Barrier 类型传递](csrc/kernels/comm/barrier.hpp) | 完成处理方式取决于请求生产接口 | EP context 设置 `use_simt_jetty`，经 Host 启动模板传到设备 barrier；其他 SIMD 路径保留原选择 |
| [Dispatch 收尾](deep_ep/include/deep_ep/impls/ep/dispatch_copy_epilogue.hpp)、[Combine 收尾](deep_ep/include/deep_ep/impls/ep/combine_reduce_epilogue.hpp) | epilogue 与主 kernel 共享请求完成状态 | 统一指定 SIMT 完成路径 |

### 2.3 通信请求生成与提交改造

| 改动点与源码 | 原版实现 | 实施内容 |
|---|---|---|
| [Dispatch 请求生成](deep_ep/include/deep_ep/impls/ep/dispatch.hpp) | warp 的各 lane 直接生成 WQE header、peer 字段和 SGE word，再写 SQ | DeepEP 继续算路由、逻辑 slot 和数据分组；改为每 lane 提供 SGE，公开 SIMT Write 路径负责槽位申请和编码 |
| 同文件 Dispatch 发布 | 按请求数推进物理 SQ head，再写门铃 | Jetty 负责队列收尾，DeepEP 在 payload 搬运和 SIMT 请求生成完成后统一发布 |
| [Combine 请求生成](deep_ep/include/deep_ep/impls/ep/combine.hpp) | `load_meta_issue_wqe` 处理 metadata 的同时逐 word 写 WQE | 保留 metadata 与计算流程，用 `Write<2, false>` 描述 hidden 和 weight 两段源数据 |
| 同文件 Combine 流水发布 | 原版自行计算每个 task 对应的物理 SQ 结束位置 | 保存每个 stage 的 `task_heads`，分开生产侧 `jetty_info` 与发布侧 `publish_info`，只发布 payload 已完成的任务前缀 |

该类改造将硬件请求编码从算法中剥离，保留原有数据依赖、流水组织和发布时机，是 EP 迁移的主要适配工作。

### 2.4 构建与运行环境适配

| 改动点与源码 | 实施内容 | 适配目标 |
|---|---|---|
| [JIT include](csrc/runtime/jit.hpp) | 在 Config 中加入安装目录的公开 Jetty 及依赖头文件路径 | 设备 kernel JIT 编译时需要找到库接口，仍由 DeepJIT 使用 Ascend 编译器 |
| [Host 扩展构建](setup.py) | 补公开头路径，并按 `platform.machine()` 选择架构目录 | 兼容安装布局及 Host 架构 |
| [网络层选择](csrc/kernels/comm/hccl.hpp) | 从原来的 `layers[1]` 改为检查并使用 netlayer ID 0 | 适配当前测试环境的实际链路 |
| [Host 计数回传](csrc/buffers/ep.hpp) | kernel 写 Device workspace，必要时异步 D2H 拷回 Host，再等待该拷贝完成 | 替代 mapped Host workspace，适配当前运行环境 |

### 2.5 模块职责划分

原有通信实现分为两类：

- **EP Dispatch / Combine**：SIMT 线程直接拼 WQE 的各个 word，写入硬件 SQ，再由 scalar 路径发布。
- **PP / Bucket**：调用 DeepEP 自带的 `handle::HcommJetty`，由这套本地封装构造 WQE、按门铃和处理 CQ。

迁移后的模块职责如下：

```text
DeepEP 保留：token 路由、目标 slot、数据分组、计算流水、发送时机
    ↓ 提供 peer、远端地址、源数据段 SGE
asc-comm Jetty：申请 SQ 槽位、编码 WQE、提交和完成记账
    ↓
通信硬件：搬运数据、产生完成信息
```

通信请求继续在设备 kernel 内直接生成和提交，保留设备侧直通路径。硬件格式维护归入 asc-comm，DeepEP 专注于算法和数据组织。

## 3. Dispatch 多 SGE 通信接口迁移

源码：[dispatch.hpp](deep_ep/include/deep_ep/impls/ep/dispatch.hpp)。

原实现根据 peer 的 token 数分配逻辑请求区间，每个 warp 处理一条请求，各 lane 编码 header、EID、token、源地址和长度并写入 SQ。

迁移保留 histogram、prefix sum、peer 去重和逻辑 slot 分配，将 lane 的职责由硬件 word 编码调整为 SGE 数据描述，由 Jetty 完成请求编码。

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

scalar 侧在 payload 搬运及 SIMT 请求生成完成后调用 `publisher.RingDoorbell()`，保持数据准备与请求发布之间的同步关系。

构建依赖包含 `BeginBatch`、`WriteDynamic` 的配套 asc-comm 头文件。

## 4. Combine 通信接口迁移与流水适配

源码：[combine.hpp](deep_ep/include/deep_ep/impls/ep/combine.hpp)，涉及 `load_meta_issue_wqe` 和 `combine_impl`。

原实现中，`load_meta_issue_wqe` 在 SIMT 中处理 metadata 并生成 token 的 WQE；scalar 侧负责规约、MTE 搬运和分批发布。

迁移保留 metadata、规约和流水结构，将 WQE 构造替换为双 SGE Write 调用：

```cpp
// lane 0：hidden 数据；lane 1：topk weight。
jetty.Write<2, false>(AscendC::simt::HcommCoopWarp{}, peer, dst, sge);

asc_syncthreads();
if (thread_idx == 0 && issue_write) {
    jetty.AdvanceWriteSq();
    ub->task_heads[stage_idx] = ub->jetty_info.packedHead;
}
```

普通 Write 批次通过配套接口 `AdvanceWriteSq` 完成队列收尾。

针对请求预取与 payload 准备进度不同的问题，采用生产状态与发布状态分离的实现：

- `jetty_info`：生产侧状态，可能包含预取任务。
- `task_heads[stage_idx]`：每个流水任务结束处的队列位置。
- `publish_info`：发布侧描述符，只把当前已准备好数据的前缀交给硬件。

scalar 在相应 MTE3 完成后通过 `publish_info` 发布；完成回收限定在已发布的任务范围内，保持原有流水依赖。

## 5. PP 与 Bucket 通信接口替换

| 位置 | 原版接口 | 迁移接口 |
|---|---|---|
| [PP send](deep_ep/include/deep_ep/impls/pp/pp_send_recv.hpp) | 本地 `put_with_notify` 发送数据并通知对端 | `AscendC::HcommJetty::WriteWithNotify`，随后 `Drain` |
| 同文件 PP recv 控制路径 | 本地 `put` 回写 release 信息 | `Write` 回写，随后 `Drain` |
| [Bucket AllGather](deep_ep/include/deep_ep/impls/bucket/all_gather/urma.hpp) | 本地 `put` 按 peer、按 chunk 推送数据 | 公开 `Write`，保留原来的分块及 peer 分工 |

原有封装内部完成对称地址换算，公开 Write 使用实际远端地址。迁移后在调用侧统一转换：

```text
remoteDst = peerInfo.remoteBaseAddr + 数据在注册窗口中的 offset
```

对端地址和权限信息复用 Channel 元数据。PP、Bucket 的接口改造纳入迁移范围，性能结果不纳入本报告的 EP 数据表；两条路径的现存验证问题见[完整测试记录](tests/jetty/results/2026-10-30/full-suite/README.md)。

## 6. 通信完成与同步机制适配

源码：[设备 barrier](deep_ep/include/deep_ep/comm/barrier.hpp)、[Host barrier 启动](csrc/kernels/comm/barrier.hpp)。

原版由 scalar barrier 遍历分配给当前 AIV 的 Jetty，调用本地 `drain`。迁移后，EP 用公开 SIMT API 生产请求，就采用相应的 SIMT `Drain` 消费完成；PP/Bucket 的 SIMD 路径仍用 SIMD `Drain`。

```text
EP 的 SIMT 请求 → scalar barrier → VF_CALL → SIMT Drain
SIMD 请求      → scalar barrier → SIMD Drain
```

通过 `use_simt_jetty` 传递完成处理类型，EP 路径由 scalar 经 VF_CALL 调用 SIMT Drain。队列完成后，继续复用 DeepEP 原有信号机制执行 rank 间同步。

## 7. 资源初始化与构建集成

Host 建连复用 [hccl.hpp](csrc/kernels/comm/hccl.hpp) 的 `acquire_jetties`：创建 endpoint、注册 buffer、建立 Channel，并将 ChannelHandle 表传入 Device，存放于 `jetty_device_ptrs`。

设备侧通过 `HcommPeer`、`HcommJetty` 构造函数将 Channel 描述符加载到调用者分配的 UB，复用于后续请求构造与发布。

构建侧在 [jit.hpp](csrc/runtime/jit.hpp) 扩展公开头文件搜索路径，保留 DeepJIT 与 Ascend 编译器链路。Jetty 设备热路径通过头文件展开，保持设备侧直接调用方式。

DeepEP 原先重复维护的 Jetty 实现已删除；算法层保留在 DeepEP，通用硬件请求实现归 asc-comm。联动源码入口及依赖见 [源码提交说明](tests/jetty/results/2026-10-30/submitted-source/README.md)。
