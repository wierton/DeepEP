# Dispatch 打点存储复查

2026-10-30 复查发现：早期 Dispatch 打点脚本只给最初的 `x` 分配 trace 尾部，但测试文件的 cached correctness 分支还会生成 `updated_x`，随后同样调用已插桩的 Dispatch。

kernel 将 trace 写到 `x + num_tokens * hidden_bytes`。对于未补尾部的 `updated_x`，这超出了该张量的分配范围。即使正确性检查曾通过，也不能据此认定此处内存访问合法。

## 哪些证据受影响

- `prepare_timing.py` 派生的逐请求 API / native 打点，以及使用这些旧副本的 `api-reserve-unchecked`。
- `prepare_scalar_timing.py` 的旧 scalar 阶段记录。
- `prepare_sparse_simt.py` 的旧 SIMT 外层记录。

这些旧日志保留供追溯，**不再作为可靠的定量依据，须修复后重新测量**。特别是“Reserve 区间减少约 2000 tick”的精确量化也需要重新确认，不能继续据此推算 kernel 性能。

## 哪些证据不受此问题影响

- 不含 trace 写入的原版、fixed-control、unchecked-fixed-batch、known-batch、known-batch-init、dynamic-sge、warp-init 的性能对照。
- 本次 Combine 打点：所有 Combine 调用共用预留了私有前缀的 input；trace 不写 Dispatch 输入。

因此无桩实验确认的 Dispatch 整体性能变化仍可作为证据；旧插桩不能用于解释具体微架构原因。无桩统计本身的样本数与环境限制仍照各组报告保留。

## 修复

三个基础生成器都为 cached correctness 使用的 `updated_x` 额外分配同样大小的 trace 尾部，并将原张量内容复制到新分配的数据部分。数据 shape、数值和 collective 操作不变，trace 空间不包含在输入 shape 内。

API/native 派生生成器拒绝从未修复的旧副本继续生成。不会悄悄修改历史实验目录；scalar/sparse 生成器支持额外 tag，便于另建目录后重测。

```bash
python prepare_scalar_timing.py /mnt/docker/o00939992 storagefix
python prepare_sparse_simt.py /mnt/docker/o00939992 storagefix
```

修复脚本通过 Python 语法检查；本次提交尚未完成这两组 Dispatch 的真机复测。不要把脚本修复写成测量结果已重新确认。
