# 原版与去掉启动 Drain 后版本：分阶段差分

生产版本：`40fdb36`；原版为远程 `DeepEP-baseline`（原始设备 Jetty 实现，保留前次验证所需的相同环境适配）。本轮没有删除发送、等待或同步操作。后续单因素验证见 [init-study](../init-study/README.md)。

## 计时方法

复用 `prepare_timing.py --detail`、`run_timing.sh jetty-timing-detail` 和 `summarize_timing.py --detail`。
仅在远程实验副本打点，安装目录及生产 kernel 不改。

- 卡 2/3，1024 tokens，hidden 4096，top-k 4，8 experts，BF16，16 AI cores（kernel 使用 32 AIV）。
- 原版 → 新版 → 新版 → 原版；四次运行退出 0，原正确性检查全部保留。
- 每次 5 次预热、20 次采样；输入逻辑数据不变，分配尾部额外 132 KiB 保存诊断数据。
- SIMT 使用 `clock64()`，scalar 使用 `asc_get_system_cycle()`。只计算同一线程/时钟域的差值，不混用、不假设频率换算 μs。
- 每个 core 的初始化/构建用相关 warp 的最大区间；再平均各 core、采样、rank 和两轮。不是全局关键路径 max。
- peer/Jetty 加载记录实际执行分支的耗时，再在 warp 内取最大；不把没有加载的 lane 0 当成有效加载耗时。
- 每请求准备/发出时间为各 warp 累计值之和除以该 core 请求数，再平均。两版每 core 平均请求数均为 5.828125。
- 计时结束后才保存时间戳；不在计时区间 printf 或 D2H，不额外插入同步。时间戳和计数器仍会影响寄存器占用、指令调度；因此后续必须用无打点版本复测。
- 相邻读时钟约 2 tick，不等于整套插桩只有 2 tick 开销。

## 结果：原始 tick

| 区间 | 原版 | 新版 | 注意 |
|---|---:|---:|---|
| 初始化整体，SIMT | 9578 | 23957 | 两版均包含原有初始化 barrier |
| peer 描述加载，SIMT | 6339 | 14523 | 原版两次宽 EID 读取；新版逐字节拼接 |
| Jetty 描述加载，SIMT | 9145 | 6895 | 新版单线程构造本身并没有测得更慢，不能只凭“串行”断言 |
| 初始化 barrier 等待，SIMT | 9326 | 21600 | 是其他 warp 等初始化，不是可另加的一笔独立成本 |
| 请求构建整体，SIMT | 4000 | 7382 | 下面两个子区间划分不完全等价 |
| 每请求准备，SIMT | 653 | 2105 | 新版准备 SGE、shuffle、远端边界检查；原版含物理 slot 原子分配 |
| 每请求发出，SIMT | 3089 | 2874 | 原版拼 word+store；新版整次 Write API（包括预留与 fence） |
| 构建后批次同步，SIMT | 不在这里 | 247 | 新版 block barrier，到 thread 0 开始 AdvanceSq |
| AdvanceSq，SIMT | 不在这里 | 1887 | 原版在 scalar 更新 head，不能把原版理解成零成本 |
| scalar 等 VF 结束 | 213 | 5541 | 是等待 SIMT 完成，不是单纯 doorbell 开销 |
| scalar 发布区间 | 555 | 473 | 原版含 advance+doorbell，新版含 attach+doorbell |

这些数不能相加：不同 warp、不同流水、不同时间域有重叠。完整逐 rank、逐轮结果见 [summary.json](summary.json)。

## 具体差异和证据边界

### 1. 初始化失去跨 warp 重叠

原版 `dispatch.hpp`：peer 在 `thread_idx < kNumRanks` 上加载，Jetty 在 `warp_idx == 1` 上加载。
当前两卡配置下 peer 位于 warp 0；两项可以重叠。

迁移版把 Jetty 构造放在 `thread_idx == 0`，与 peer 位于同一 warp，并在源码里先后执行。
只看两项各自耗时会漏掉这个调度变化。新版 Jetty 加载区间并没有更慢，但整体初始化明显更长。

### 2. EID 加载方式不同

原版 `handle.hpp::HcommPeerInfo::load_peer_info` 读取两个 uint64。
当前安装头文件 `HcommPeer::Init` 循环读取 16 个 byte，再移位合成两个 uint64。
是否真影响关键路径，必须结合后续等价宽读取实验，而不能仅数源码行数。

### 3. 请求表示转换与批次完成处理

原版 lane 直接对应 WQE word，迁移版先形成每 lane 一个 `HcommSge`，再交给 API 重新 shuffle 为 word。
原版 slot 分配在调用者内，API 版分配在 Write 内，因此“准备”和“发出”两个子区间不能直接按函数名一一对齐。

新版 `AdvanceBatch` 会读回最后 WQE 的首 word，补齐 final flags、写回、fence、发布 packedHead。
原版在首次拼最后一条 WQE 时写 final flags，scalar 的 `advance_sq` 只更新 UB packedHead。
需要检验这次 GM 读回是否是额外延迟来源；不能将整个 AdvanceSq 时间一概归因于读回。

## 重现与失败记录

```bash
python3 tests/jetty/prepare_timing.py /mnt/docker/o00939992 --detail
bash tests/jetty/run_timing.sh jetty-timing-detail
python3 tests/jetty/summarize_timing.py tests/jetty/results/2026-10-29/timing-detail/logs --detail
```

生成器要求目标实验目录不存在，避免覆盖既有结果。两版完整打点 patch 在 [patches](patches)。
首次插桩编译失败：`__reduce_max(uint64_t)` 无重载；改成 uint32 归约并额外检测高 32 位溢出，解析器拒绝溢出样本。
该失败日志在 [failures](failures)，不计入测量。压缩日志为原始 JSON 时间戳的无损归档。
