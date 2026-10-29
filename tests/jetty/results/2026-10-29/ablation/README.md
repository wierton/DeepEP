# Dispatch Jetty 单因素实验

目的：验证迁移后 Dispatch kernel 增加约 2.54 μs 的候选来源。这里的时间包含
整个 `dispatch_impl`，但关闭其末尾通信完成 barrier；不是 Host launch 时间，也不是
单独 WQE 填写时间。各段可能和 MTE 重叠，不能将单因素时间差直接相加。

正式 DeepEP 与安装目录均未修改。基准源码为 `d363366`；实验脚本只复制和修改
`/mnt/docker/o00939992/jetty-ablation-v2/` 中的副本。Host 扩展 `.so` 和建链库保持不变。

## 实验因素

| 副本 | 唯一语义改动 | 保留的操作 |
|---|---|---|
| control | 无 | 全部 |
| no-start-drain | 删除 Dispatch 初始单线程 `Drain` | 描述信息初始化、批次容量检查、最终完成等待协议 |
| no-entry-sync | 删除批量 warp Write 的入口 `coop.sync()` | Write 末尾 `asc_threadfence()`、末尾 `coop.sync()`、批次同步 |
| no-last-atomic | 不逐请求 atomic-max 更新最后槽位；批次结束用 `count - 4` 推导 | atomic-add 分配槽位、每请求容量检查、所有 fence、最后 WQE 头回写 |
| cached-tail-local | 初始 Drain 后，由 Dispatch 把已完成 SQ 进度缓存到 UB；每请求改读该快照 | 初始 Drain、容量检查、atomic-max、所有 fence |

最后一项**仅用于 Dispatch 固定 4 WQEBB 槽位的批次**，不能推广到混合长度请求。
库内改动通过实验宏限制在 Dispatch 翻译单元，不修改 Combine 的 Write 路径。
`no-start-drain` 只用于当前串行测试流程，不证明在允许未完成批次重叠的任意调用下安全。
`cached-tail-local` 为实验的 BatchState 增加一个 uint32 字段；仍放在原有足够大的 stage 区。
快照由 Dispatch 在初始 Drain 后更新，批次期间不允许其他生产者占用这条 SQ；不依赖后续完成进度来
扩大容量，因此容量判断可能保守，但不会虚增可用空间。不是省略容量检查。

`coop.sync()` 的实际调用链：`HcommCoopWarp::sync()` →
`single_warp_thread_block_tile_base<32>::sync()` → `asc_threadfence_block()`。
因此 `no-entry-sync` 删除的是入口内存 fence，不是一个跨 warp 的线程块 barrier。

## 避免“改了头文件但没有测到”的检查

最初仅将副本放入用户 `-I` 路径，预处理显示 ASC 仍选择了安装目录的头文件。
该批试跑不纳入本报告。随后所有版本（包括 control）统一显式引用副本头文件。
DeepJIT 的 include 哈希解析只接受尖括号 include，因此使用 `<绝对路径>`，没有修改 DeepJIT。

`verify_ablation_headers.py` 重放实际 JIT 编译命令进行预处理，检查最终实现路径；
检查 no-last-atomic 已消除 atomic-max、no-start-drain 已消除原条件；并反汇编真实
`kernel.o`。每个副本使用独立 JIT 缓存，不依赖 Jetty 头文件是否被 DeepJIT 的哈希器跟踪。

## 测量范围

- 与上一份报告相同的服务器、Toolkit、卡 2/3、1024 tokens、hidden 4096、top-k 4、8 experts、BF16。
- `--num-ai-cores 16 --test-first-only`；默认 CPU 同步和结果校验不关闭。
- 原测试包含普通 Dispatch、cached Dispatch、Combine；不是完整的 18 模式回归。
- 每段 10 次预热、50 次采样；每轮先取两个 rank 各自均值的最大值，再平均两轮。
- 顺序：control → no-start-drain → no-entry-sync → no-last-atomic → 反向再跑一遍。
- cached-tail-local 追加两轮运行；没有与前四组完整交错，因此解释小差异时更保守。
- 不代表逐迭代 rank max，不代表端到端耗时，不证明任意规模或多机下安全。

## 测量结果

单位 μs；每格是两轮的 rank-max-of-means 再平均。

| 版本 | Dispatch（无末尾等待） | Cached Dispatch（无末尾等待） | Combine（未修改，观察波动） |
|---|---:|---:|---:|
| control | 26.872 | 22.134 | 38.723 |
| no-start-drain | 26.014 | 21.641 | 38.758 |
| no-entry-sync | 26.686 | 22.153 | 38.629 |
| no-last-atomic | 26.603 | 21.928 | 38.471 |
| cached-tail-local | 26.534 | 22.206 | 39.004 |

四组共 8 次进程运行均退出 0，保留原测试正确性断言。
cached-tail-local 两次也退出 0。其后额外重跑 control（`guard/control-3.log`）退出 0，
普通 Dispatch 为 26.572 μs，cached 为 22.336 μs。该轮只用作末尾波动/恢复检查，
不混入上表前四组的两轮均值。

- 初始 Drain：普通 Dispatch 两次分别减少 0.861、0.855 μs；cached 减少
  0.599、0.387 μs。这是目前较清楚的单因素结果，支持“额外初始 Drain 增加成本”。
- 入口同步：普通 Dispatch 平均减少 0.186 μs，cached 反而增加 0.019 μs。
  不能据此说它是主要瓶颈。
- atomic-max：普通 Dispatch 平均减少 0.269 μs，但未修改的 Combine 也平均快了
  0.252 μs，说明这一量级不能忽略测量波动；不能宣称确定省下 0.269 μs。
- 带末尾通信等待的 Dispatch，control 为 222.794 μs，no-start-drain 为
  224.006 μs：无等待段变快**并不等于完整通信变快**，本次没有证实完整通信收益。
- 完成进度缓存：对最初两轮 control 平均看似减少 0.338 μs，但最后补跑的 control
  已为 26.572 μs，和 26.534 μs 很接近；cached 也没有收益。因此不能证明重复读取
  GM 完成进度是这组数据的主要瓶颈。

结论：这轮实验较明确支持“新增启动 Drain 有成本”；没有证实入口同步、atomic-max、
每请求读取完成进度能解释整个迁移差距。尤其不能把四个差值相加，声称可直接省下其总和。

### 指令证据

普通 Dispatch、`kDoBarrier=false`：control 的静态 `MEMBAR` 为 10 条，
no-entry-sync 也为 10 条，不能按源码调用数计算 fence 成本。实际反汇编差异中，
Write 入口附近同一条 LOP3 的 `wait:0b0001111` 变成 `wait:0b0000000`。
独立 MEMBAR 数不变，不意味着源码改动没有进入二进制。

control 有 6 个静态 `RED.S.MAX.u32.NMFV` 位置，对应不同 SGE 数特化分支；
不是每条请求执行 6 次。no-last-atomic 会移除这些静态位置。
完整对象哈希与静态计数见 `instruction-summary.json`，不是动态执行次数统计。

尚未隔离：描述信息串行加载、SGE→word 的额外 shuffle、最后 WQE 头的 GM
read-modify-write。**没有证明整个旧版→新版差距都来自某个单一操作**，也没有将
实验删除同步的补丁当作已验证的通用优化合入正式实现。

### 失败的快照实验也保留

初版 `cached-tail` 把更新快照加进通用 `Drain()`。第一轮在带末尾 barrier 的
`dispatch_impl<..., kDoBarrier=true>` 中失败，进程退出 1，没有有效性能结果。
plog 报错 340：`The address for VEC to access UB is not aligned`。
日志和补丁保留在 `failed/` 与 `patches/cached-tail.patch`。

后续 `cached-tail-local` 把快照写入移到 Dispatch 初始化段，恢复通用 Drain 原样。
失败日志只证明该实验版本在最终 barrier 路径报错，**尚不足以确定具体未对齐访问的
源代码位置，不能归咎于硬件或宣称发现编译器 bug**。两个版本均未进入正式 Jetty/DeepEP 实现。

## 复现

将 `tests/jetty/prepare_ablation.py`、`run_ablation.sh`、`verify_ablation_headers.py`、
`summarize_ablation.py` 复制到测试服务器的 `/mnt/docker/o00939992/`。
要求已有该目录下的 DeepEP-Ascend（包含已编译的 Host 扩展）、Ascend/cann 和 venv。

```bash
cd /mnt/docker/o00939992
venv/bin/python prepare_ablation.py /mnt/docker/o00939992
bash run_ablation.sh
source Ascend/cann/set_env.sh
venv/bin/python verify_ablation_headers.py /mnt/docker/o00939992/jetty-ablation-v2
venv/bin/python summarize_ablation.py /mnt/docker/o00939992/jetty-ablation-v2/logs
```

准备脚本遇到已存在的实验目录会报错，不覆盖之前的结果。
反汇编与预处理大文件仅保留在服务器，各实验的语义补丁和结果日志归档在本目录。
