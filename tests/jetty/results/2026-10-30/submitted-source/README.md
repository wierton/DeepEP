# Jetty 联动优化的源码提交与验证

## 源码在哪里

| 仓库 | 分支 / commit | 改动 |
|---|---|---|
| [xfouhw/asc-comm](https://gitcode.com/xfouhw/asc-comm) | `perf/jetty-known-write-batch-20261030` / `9c02462b` | 4 个 Jetty 头文件；新增批次/动态 SGE/普通 Write 收尾接口 |
| [wierton/DeepEP](https://github.com/wierton/DeepEP) | `migrate/asc-comm-jetty` / `a50ce74` | Dispatch、Combine 两个调用文件 |

这是实际源码修改，不再仅为实验 patch。asc-comm 分支基于 `origin/master` 的 `c9ec6c96`，没有带入本地 CCU 分支的提交，也未包含原工作树尚未提交的 `AdvanceImplicitSq` 修改。两个优化 commit 都不含报告和日志。

## asc-comm 接口及边界

`include/aicore/jetty/hcomm_jetty_simt.h` 增加：

```cpp
int32_t BeginBatch(uint32_t totalWqebbs) const;
template <typename Coop>
int32_t WriteDynamic(const Coop&, const HcommPeer&, __gm__ void* dst,
                     const HcommSge& laneSge, uint32_t count) const;
void AdvanceWriteSq() const;
```

实现分别位于：

- `src/aicore/jetty/impl/hcomm_jetty_simt_impl.h`：公开接口转发。
- `src/aicore/jetty/impl/platform_v310/hcomm_simt_urma_jetty_def.h`：声明、`dynamicSges` 和私有批次状态。
- `src/aicore/jetty/impl/platform_v310/hcomm_simt_urma_jetty.h`：容量预检、槽位申请、WQE 构建、收尾和宽 EID 加载。

### 已知批次：BeginBatch → 多 warp WriteDynamic → AdvanceSq

只允许独占 SQ 的普通 warp Write，每条固定占 4 WQEBB。BeginBatch 返回非零时不得继续提交；total 必须为 4 的整数倍，0 是空批次。它先检查当前未消费量和整个批次容量，包含发布用的 2 WQEBB，然后存下 expectedWqebbs。

后续每个 warp 仍通过 atomic_add 申请槽位，但不再逐请求读 GM 完成指针、重复检查 SQ 容量或 atomic_max 最后槽位。保留申请越界检查；不支持的 producer 会置失败状态。最后一个固定槽位直接在第一次写 word0 时设置最终 flags。

调用方必须先同步所有 producer，再调用 AdvanceSq。实际 count 不等于声明总量或存在失败时，不发布不完整批次；成功时保留 fence、packedHead/GM head、完成计数更新，清空批次状态。PublishSq 和后续 Drain 没有删掉。

与之前实验的区别：**没有无条件删除通用 ReserveBatchSq 的容量检查**。快速路径仅在 BeginBatch 预检成功后启用；未知大小批次仍走原逻辑。

### 动态 SGE

count 必须在 1..12，所有协作 lane 一致；各 lane 提供自己的 SGE。header、有效 payload 字数和 NOP 起点使用实际 count，仍保留固定四-WQEBB 槽位及完整的写请求。调用默认不发布，由调用方同步后收尾。

### 普通 Write 收尾

AdvanceWriteSq 用于 Combine 当前仅由 `Write<2, false>` 组成的批次。它明确重建普通 Write 头，省掉 GM readback，但仍写最终头、fence、更新完成计数与 head。inline/notify/混合批次必须继续使用 AdvanceSq，不能套用这个专用入口。

## DeepEP 调用侧

- `deep_ep/include/deep_ep/impls/ep/dispatch.hpp`：在存在第二个 warp 时由其 lane0 初始化 Jetty（单 warp 时回退到 thread0）；原 block 同步保留。调用 BeginBatch 后，用 WriteDynamic 替代六路 `Write<2/4/6/8/10/12>` 分支。
- `deep_ep/include/deep_ep/impls/ep/combine.hpp`：所有普通 Write producer 同步后调用 AdvanceWriteSq；task head 保存、最终发布和等待不变。

DeepEP 不自己编码 Jetty word，也没有复制一套 Jetty 实现。使用该 DeepEP commit 前，必须更新到上述 asc-comm 头文件；旧 CANN 头文件没有这些 API，会编译失败。该变更为头文件实现，不是新增 Host `.so` 符号；不能只更新 DeepEP 而继续用旧头文件。

## 验证结果

- 4 个 Dispatch 实例：预处理确认使用新源码，编译/链接通过，见 [对象哈希与命令](dispatch-summary.json)。
- 3 个 Combine 实例：同样通过，见 [对象哈希与命令](combine-summary.json)。
- `make -C tests/jetty compile CANN_HOME=...`：通过，覆盖综合模板编译，存在原有 deprecated/loop-unroll warnings。
- 以上不修改系统安装目录；[准备脚本](../../../prepare_submitted_jetty.py) 把提交源码放进独立 `submitted-jetty` 测试副本，临时绝对 include 只用于确保验证实际选到新头文件，不是生产代码改动。
- 真机 6/7 卡的本轮测试打印到 `Running expand dispatch/combine test cases:` 后，约 4 分半未继续。该打印之后 `dist_print` 会调用 HCCL `dist.barrier()`；当时独立 JIT cache 尚无新 Dispatch kernel。主动终止本测试进程组，退出状态 143，**不能算新源码真机通过，也不能认定是新 Jetty kernel 报错**。见 [日志](ep-submitted-jetty-1.log)、[退出码](status.tsv)。未重置设备或终止其他用户进程。

此前独立实验的性能见 [完整报告](../full-suite/README.md)。新的合并源码增加了已知批次容量预检，因此不能把此前独立实验的耗时直接当作这两个提交的实测结果；仍需在可用真机环境完成合并后的校验与性能测量。PP/Bucket 的既有失败未在此修复。
