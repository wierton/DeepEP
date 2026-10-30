# Combine 初始化与头重建组合：待上机

生成：`prepare_combine_header.py /mnt/docker/o00939992 --with-init`。
以 `combine-init` 为基础增加普通 Write 已知头重建；本目录 patch 是相对该基础的增量，
初始化改动见 [combine-init](../combine-init/README.md)。

保留全部通信和 startup Drain：peer 加载与 Jetty 初始化改为不同 warp，EID 用两个对齐 64 位读取；
AdvanceBatch 保留最终标志、fence、完成计数、SQ head 发布，只重建普通 Write 已知头。
不是删除初始化或发布操作。

已重放 `combine-known-header` 三个真实 JIT 实例，编译、链接全部通过。
三个预处理输出都确认包含额外 warp 初始化、宽 EID 读取、已知 WQE 头重建。
`offline-summary.json` 保存具体命令、实例名和最终对象哈希。

**没有上机结果，不能将两个单项收益相加。**
目前已验证可建连的 0/1、2/3 被其他任务占用；需要独立无桩对照后才能判断组合收益。
待运行 original → combine-known-header → combine-init-header → 逆序，保持相同设备和参数，保留 correctness。
不要用不同设备组或旧轮次的 original 来补齐比较。
