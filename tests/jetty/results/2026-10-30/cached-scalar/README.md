# Cached Dispatch：安全 scalar 分段对照

生成器：`prepare_scalar_timing.py /mnt/docker/o00939992 safe-cached --cached`。
使用已有 cached handle 调用 dispatch；初始 x 和 correctness 测试中的 updated_x 均分配额外 trace 尾部。
不沿用旧的越界风险 fixture。解析检查每核时间递增、区间单调、核数一致及每 rank 20 次采样。
两边四个实际 Dispatch JIT 实例的头文件路径和编译对象均核验，记录包含 cached/final_barrier 模板参数。

## 测试

设备 0、1，2 ranks，1024 tokens，hidden 4096，topk 4，8 experts，BF16，16 AI cores。
original161 → control161 → control162 → original162 四次均退出 0，保留 correctness。
每次选择 post-entry-barrier scalar 区间最长的 core，同一 core 的各段可以相加；再平均样本/rank/轮次。
不是将不同 core 的各段最大值相加，也不是无桩总 kernel 耗时。

| scalar 区间（tick） | 原版 | 迁移版 |
|---|---:|---:|
| VF 提交 | 52.19 | 51.69 |
| metadata | 508.21 | 548.79 |
| 等待本地槽位 | 1295.74 | 1643.90 |
| 本地 payload 循环 | 18290.39 | 17177.75 |
| 等待 SIMT 完成 | 190.30 | 2625.09 |
| 发布 | 554.63 | 487.15 |
| post-barrier 合计 | 20891.45 | 22534.36 |

等待 SIMT 增加约 **2435 tick**，本地 payload 区间反而缩短约 1113 tick，合计增加约 **1643 tick**。
这指向 SIMT 尾部作为下一层排查方向，不能归因于单一 ReserveBatchSq 或单纯本地搬运带宽。
入口 barrier 均值原版约 18455、迁移版约 178465 tick，波动大，包含 rank 到达/同步影响，不把它作为 Jetty API 开销。
共享服务器上无法据此确认全部背景干扰，结论限定于阶段定位；后续仍需等价无桩验证。

## 下一步

`prepare_sparse_simt.py ... safe-cached --cached` 准备原版/迁移版/固定槽位精简 reserve 的 SIMT 外层分段。
保持实际通信，比较初始化、槽位相关中间阶段、请求构建与收尾。
下一组改用当时空闲的 1、7 卡，独立配对，不与本组直接合算收益。
