# Cached SIMT 分段：建连失败与离线检查

## 失败不属于待测 kernel

2026-10-30，选择当时空闲的物理设备 1、7，运行 `sparse-safe-cached-original:171`。
在 EPBuffer 构造期间退出 1：

```text
HCCLContext::build_peer_descs
hccl.hpp:165: desc.has_value() and err
```

`make_peer_desc(layer, peer, accept)` 调用 `HcclRankGraphGetLinks`，从返回 links 中找协议匹配项；
找不到则返回 nullopt。构造器明确选择 layer 0，先建 UBMEM channel，再建 Jetty。
本条断言只能证明某项所需 link 未匹配，不能仅凭它认定整个 rank graph 没有边或网络已经断开。
HCCL 日志有 rank graph 构建记录，但不够确定具体缺失的协议/边。

原版尚未执行测试 kernel，也没有生成有效性能样本。批处理在这里停止，后续 control/unchecked 未运行。
保留 stdout/stderr 和 rank 0 plog 于 `failed-init`；不将这组失败混入性能平均值。
没有修改系统拓扑或重置设备。随后已知通过的 0/1、2/3 组合被其他进程占用，等待可用测试窗口。

## 离线编译

通过 `compile_jetty_experiment.py` 重放前一组记录的真实 JIT 编译/链接命令，
仅将 include 指向新的分段 fixture，输出放入独立 `*-offline` 目录。
预处理核验目标 Dispatch 源码、clock64 打点与安全 cached 输入准备。
原版、control 各四个实例已通过，记录见 `offline-original.json` 与 `offline-control.json`。
unchecked 的四个实例也通过，记录见 `offline-unchecked.json`。

**离线编译不等于运行成功，也不是性能证据。** 本目录暂时没有可用的 SIMT 分段结果。
新 fixture 保留全部 Write、发布及同步；原始输入和 updated_x 都有独立 trace 尾部。
解析器区分活动 warp 与未写记录，并拒绝跨样本复用的旧时间戳。
