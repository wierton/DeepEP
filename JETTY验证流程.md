# asc-comm Jetty 迁移：验证流程与改动摘要

## 1. 需要哪些仓库和环境

只需克隆本分支，并拉取固定版本的 DeepJIT 子模块：

```bash
git clone --branch migrate/asc-comm-jetty git@github.com:wierton/DeepEP.git DeepEP-Ascend
cd DeepEP-Ascend
git submodule update --init third-party/deep_jit
```

DeepJIT 指向 `wierton/DeepJIT`，包含 x86 环境头文件路径修复。
编译、运行不需要 clangd 子模块，也不需要另行编译 asc-comm/hcomm：
使用已安装 CANN 的公开 Jetty 头文件及 `libhcomm`、`libhccl`。

本次实测：x86_64、Ascend950PR、CANN Toolkit + 950 ops
`9.3.0~weekly.20261021.01`、驱动 `25.7.rc1.6`，Python 3.12.3、
torch 2.7.1+cpu、torch_npu 2.7.1.post4。Python 环境需有 setuptools、
ninja 等构建依赖；系统需 C++20 编译器、`libdw-dev`、`libelf-dev`。

## 2. 构建和运行

以下在仓库根目录运行；先激活已有的 Python 环境，再修改 CANN 路径：

```bash
export CANN_HOME=/mnt/docker/o00939992/Ascend/cann
source "$CANN_HOME/set_env.sh"
python setup.py build_ext --inplace
python -c 'import deep_ep'
make -C tests/jetty compile CANN_HOME="$CANN_HOME"
python tests/jetty/test_combine_schedule.py

mkdir -p logs
export PYTHONPATH="$PWD"
export ASCEND_RT_VISIBLE_DEVICES=0,1
export MASTER_PORT=29671
export EP_JIT_CACHE_DIR="$PWD/.jit-cache"
export ASCEND_PROCESS_LOG_PATH="$PWD/logs/ascend"

# 两卡，全部 18 种模式；默认 CPU 同步、结果校验均开启。
timeout --kill-after=15s 300s python -u tests/ep/test_ep.py \
  --num-processes 2 --num-ai-cores 16 \
  --num-tokens 1024 --hidden 4096 --num-topk 4 --num-experts 8 \
  --skip-perf-test > logs/ep-all-modes.log 2>&1
echo "exit=$?"
```

`--skip-perf-test` 仅跳过性能测试，不跳过正确性校验。
再加 `--defer-epilogue 0`、换一个输出日志名，可以验证非延迟 epilogue。
不要加 `--do-cpu-sync 0` 或 `--skip-check` 来代替默认路径验证。

## 3. 实际跑过什么

| 测试 | 结果 |
|---|---|
| Host 扩展构建、import、Device kernel 模板编译 | 通过 |
| Combine 调度 CPU 模型：4 个测试 | 通过，不等同于硬件验证 |
| 两卡 BF16：128 tokens / hidden 512 / top-k 2 / 4 experts | 默认 CPU 同步下通过 |
| 两卡完整 18 模式：1024 tokens / hidden 4096 / top-k 4 / 8 experts | 全部通过，退出码 0 |
| 同一 18 模式，非延迟 epilogue | 全部通过，退出码 0 |
| 原版通信 kernel + 相同环境兼容修改，同一 18 模式 | 全部通过，退出码 0 |

18 模式包括 alignment 128/1、BF16/FP8、无 bias/单 bias/双 bias、
FP8 scale 行/列布局；测试内部也校验 cached handle 路径。

原版对照来自 `deepseek-ai/DeepEP-Ascend` 的 `3b25377`，不是 NVIDIA DeepEP。
如需单独查看：

```bash
git clone git@github.com:deepseek-ai/DeepEP-Ascend.git DeepEP-baseline
git -C DeepEP-baseline checkout 3b25377
```

**对照并非完全未修改**：保留原版通信 kernel，但补了构建 include/DeepJIT
路径适配、netlayer 0 和下述计数回拷修复。只有 netlayer 0、没有计数修复的
原版与迁移版都会报 334，不能声称原版原封不动即可跑通。

## 4. 大概改了哪些代码

| 文件/模块 | 改动及原因 |
|---|---|
| [setup.py](setup.py)、[JIT 配置](csrc/runtime/jit.hpp)、DeepJIT 子模块 | 增加公开 Jetty include 路径、使用 CANN 通信库；修复 DeepJIT 硬编码 AArch64 头文件路径 |
| [Dispatch](deep_ep/include/deep_ep/impls/ep/dispatch.hpp)、[Combine](deep_ep/include/deep_ep/impls/ep/combine.hpp) | 从手拼 WQE/SQ 改为公开 SIMT Jetty 接口；保留数据路由，适配 SGE、请求槽大小、ready-prefix 发布和队列容量回收，不是一对一改名 |
| [Barrier](deep_ep/include/deep_ep/comm/barrier.hpp) 及 Host/kernel 包装 | EP 的 SIMT producer 配套 SIMT Drain；PP/Bucket 保留 SIMD Drain，避免混用完成进度的解释 |
| PP、Bucket 及旧 `comm/handle.hpp` | 调用改为公开 SIMD Jetty，删除复制进来的私有 Jetty 实现；这两类仅做了编译验证，不承诺真机正确性 |
| [HCCL 建连](csrc/kernels/comm/hccl.hpp) | 由第二个网络层改为显式 netlayer ID 0，并检查层存在；仍要求真实 UB_MEM/UBC_CTP 链路，未伪造拓扑 |
| [EPBuffer](csrc/buffers/ep.hpp) | Device workspace 接收计数，同 stream D2H，再用 event 保证 Host 读到完成的数据；移除直接写 Host 映射地址的依赖 |

最后一项是**环境兼容修改**：独立、不含 Jetty 的小程序也复现了 Host 映射
写入失败。计数回拷保留默认 CPU 同步和精确结果，但增加拷贝/事件开销，
失去原先提前通知 Host 的重叠收益；没有修复底层映射问题，也不代表性能等价。

本次不涵盖多机/CLOS、其他卡数、长期队列压力、PP/Bucket 真机和性能对比。
完整过程及远端日志路径见 [REMOTE_VALIDATION.md](tests/jetty/REMOTE_VALIDATION.md)。
