# 全性能入口复测：本轮被设备初始化阻塞

本轮基于 `dd9b739` 的实验记录，2026-10-30 远端时间 14:18 执行。**没有生成新的性能数字，不能宣布全部测试通过。**

## 已完成的实际测试

原版 DeepEP 在物理卡 3/5 上进行两 rank EP 正确性预检，退出码 1。未开始 Jetty kernel，也未进入 profiler。

```bash
source /mnt/docker/o00939992/Ascend/cann/set_env.sh
cd /mnt/docker/o00939992/DeepEP-baseline
export PYTHONPATH="$PWD" ASCEND_RT_VISIBLE_DEVICES=3,5 MASTER_PORT=29863
export TMPDIR=/mnt/docker/o00939992/tmp
export EP_JIT_CACHE_DIR=/mnt/docker/o00939992/jetty-init-study/cache-fixed-original
export ASCEND_PROCESS_LOG_PATH=/mnt/docker/o00939992/retest-20261030-suite/preflight-ascend
timeout --kill-after=15s 120s /mnt/docker/o00939992/venv/bin/python -u \
  tests/ep/test_ep.py --num-processes 2 --num-ai-cores 16 \
  --num-tokens 128 --hidden 512 --num-topk 2 --num-experts 4 \
  --dispatch-dtype bf16 --test-first-only --skip-perf-test
```

[完整 stdout/stderr](preflight-original.log)、[两 rank 的 debug plog](plog)。

首发日志不是性能回归：

```text
WaitRsp: tsd client wait response fail ... check hdc service
ReportSocketCloseProc: Device 3 ... DEV_RUNNING_DOWN
TsdOpenNetService failed
```

上层在 `get_hccl_comm_name` / `create_config` 报 `507000`，随后另一 rank 出现 socket closed、RecvRankTableMsg、RootInfoDetect failed。不能仅凭这些日志确定底层根因，也不能把后续 socket 错误当成 Jetty 算法问题。

测试前只有 3、5 卡没有列出运行进程；0/1、2/3 等已有其他用户进程。失败后复查，5 卡也被其他进程使用，仅 3 卡空闲。没有终止其他用户进程、重置设备、修改驱动或持续向故障设备提交测试。

## 已核对的全部性能入口

| 入口（仓库根目录相对路径） | 测什么 / 口径 | 计划参数 | 本轮状态 |
|---|---|---|---|
| `tests/ep/test_ep.py` | Dispatch、cached Dispatch、Combine；分别报告发起、URMA、epilogue 的 profiler 时间 | 2 ranks，1024 tokens，hidden 4096，topk 4，8 experts，BF16，16 AI cores | 原版小规模预检失败；正式性能未执行 |
| `tests/pp/test_pp.py` | PP send/recv kernel，正确性及压力检查后 profiler | 2 ranks，其余默认：4096×7168，4 inflight，4 stress rounds，128 sends | 未执行：无可用测试卡对 |
| `tests/engram/test_engram.py` | fetch + wait，profiler；默认保留正确性校验 | 2 ranks，其余默认：两层 524288/524309 entries，hidden 256，512 tokens，24 entries/token | 未执行：同上 |
| `tests/bucket/test_all_gather.py` | 完整 all_gather.wait；NPU event，各 rank 均值取 max；同时测 HCCL | 2 ranks，默认每 rank 1 GiB，10 warmups、30 samples | 未执行：同上 |
| `tests/comm/test_barrier.py` | barrier kernel profiler；每次回调执行 1000 个 barrier | 2 ranks，不启用无限压力模式 | 未执行：同上 |

另外的 allocation、stream、gate、bucket session 脚本主要是功能测试，不冒充性能入口。jetty 目录下的 run_timing 等是以前的定位/插桩工具，本轮性能复测不使用有桩版本替代无桩结果。

## 已准备的执行脚本

[run_perf_suite.sh](../../../run_perf_suite.sh)，`bash -n` 检查通过；**完整矩阵尚未运行**。

每个入口正序/倒序执行四个独立副本：

1. original：原版，保留已有相同环境兼容修正。
2. fixed-control：未做本轮接口优化的迁移版，Dispatch 多余 startup Drain 已删除。
3. dynamic-sge：已验证的 Dispatch 组合优化，不含 Combine 已知头优化。
4. combine-known-header：独立 Combine 头重建优化，不含 Dispatch 组合优化。

没有擅自把两份优化拼成一个未经验证的新版本。脚本比较各版本测试脚本是否相同，保留全部结果校验、源文件哈希、实际命令、stdout/stderr、plog、退出码，使用独立无桩 cache。任一失败停止矩阵，避免继续产生不可比较数据。

拿到独占且互通的卡对后，在远端运行：

```bash
ASCEND_RT_VISIBLE_DEVICES=0,1 \
RESULT_DIR=/mnt/docker/o00939992/perf-suite-new-run \
bash /mnt/docker/o00939992/run_perf_suite.sh
```

上面的 0/1 只是配置示例，必须先确认卡已空闲。两 rank 不能覆盖所有规模、dtype 和拓扑。本轮不复用历史数字作为新测结果；历史收益仍见 [此前汇总](../final-comparison/README.md)。
