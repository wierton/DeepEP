# Combine 初始化实验与中断记录

## 改动

从无桩 `fixed-control` 复制生成 `combine-init`。只有 Combine 的初始化改变：

1. Jetty 初始化与 peer 初始化分到不同 warp，VF 增加一个 warp。原先 Jetty 在 thread 0，与 peer 初始化处于同一 warp。
2. API 内 EID 加载由逐 byte 改为两次对齐的 uint64 加载，保留字段语义，并以 static_assert 检查来源对齐。

**仍然调用 Jetty 构造函数和显式 startup Drain；没有删除 Write、AdvanceSq、发布、等待、结果校验，没有插桩。** task 主循环及完成协议未改。

改动限定实验头文件，见 [combine-init.patch](combine-init.patch)。不是生产源码变更，也没有修改系统安装头文件。

## 已完成的检查

`compile_combine_init.py` 重放已成功运行的 Combine 打点对照的三个 kernel 实例编译命令，替换 include 为无桩初始化实验副本。预处理确认选中修改后的文件、宽 EID 加载、不同 warp 初始化、startup Drain 保留且没有 `probe0`。

三个实例的设备编译及链接全部通过，包括 final barrier 和 topk weights 的实例差异。命令、实例源码与产物哈希见 [offline-summary.json](offline-summary.json)。这不是 NPU 功能验证。

```bash
python prepare_combine_init.py /mnt/docker/o00939992
source /mnt/docker/o00939992/Ascend/cann/set_env.sh
python compile_combine_init.py /mnt/docker/o00939992
```

生成器要求目标目录尚不存在，防止覆盖已有实验。

## 无桩测量尚未完成

计划顺序：original:81 → fixed-control:81 → combine-init:81 → combine-init:82 → fixed-control:82 → original:82。

前两项通过。第三项在 `EPBuffer` 初始化、交换 listen port 的 `HcclAllGather` 返回 HCCL error 15：

```text
csrc/kernels/comm/hccl.hpp:292
HcclAllGather(local_port, device_ports, 1, HCCL_DATA_TYPE_UINT16, comm, stream)
```

错误发生在实验 Combine kernel 编译/执行之前。底层日志包含 device 3 的 `halMemAlloc` 失败、drvRetCode=62，以及 HCCL AICPU cache-eviction kernel 的加载失败。见 [进程错误](interrupted-cohort/combine-init-81.log)、[驱动错误摘录](interrupted-cohort/driver-errors.log)。这不能证明最初根因，仅证明此次失败没有测到实验 kernel。

同时观察到其他人的 `ain_all_gather_matmul` 测试占用设备 0–3，随后设备 2、3 又出现其他测试进程。没有停止其他人的任务、重置设备或继续争用这些卡。任务冲突与驱动错误同时出现，但尚未确认二者的因果关系。

因此本实验**没有有效的 NPU 正确性/性能结果，不能声称省掉多少微秒**。保留 [未完成对照组](interrupted-cohort/)；不要将其中两个成功样本拼成三版本对比。需要协调空闲双卡时段，以新的轮号重新跑完整的正序/倒序对照。
