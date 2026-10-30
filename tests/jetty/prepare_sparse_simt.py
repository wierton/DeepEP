"""Four outer SIMT timestamps per warp; no per-request instrumentation."""
import difflib
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]).resolve()
dest = root / "jetty-init-study"

def once(text, old, new):
    assert text.count(old) == 1, (old, text.count(old))
    return text.replace(old, new)

for name, source in (("sparse-original", root / "DeepEP-baseline"),
                     ("sparse-control", dest / "fixed-control"),
                     ("sparse-unchecked", dest / "unchecked-fixed-batch")):
    if len(sys.argv) > 2:
        name = name.replace("sparse-", "sparse-" + sys.argv[2] + "-", 1)
    target = dest / name
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git", "__pycache__", "build", "logs", "verified*"))
    for path in (target / "deep_ep/include").rglob("*"):
        if path.suffix in (".h", ".hpp"):
            path.write_text(path.read_text().replace(str(source), str(target)))
    path = target / "deep_ep/include/deep_ep/impls/ep/dispatch.hpp"
    original = text = path.read_text()
    markers = ["    // Load descriptors while" if name.endswith("original") else "    // These constructors initialize",
               "    if constexpr (not kCachedMode) {\n        // scatter rank counts",
               "    // Write SQEs" if name.endswith("original") else "    // Each warp supplies token/metadata SGEs;",
               "    while (ub_layout->metadata_ready != 1)"]
    for i, marker in enumerate(markers):
        text = once(text, marker, f"    const uint64_t sparse_{i} = clock64();\n" + marker)
    text = once(text, "    const uint64_t sparse_0 = clock64();",
                "    EP_STATIC_ASSERT(kNumVecCores <= 64 && kNumWarps <= 16);\n    const uint64_t sparse_0 = clock64();")
    marker = "    while (ub_layout->metadata_ready != 1)"
    record = """    if (lane_idx == 0) {
        auto* sparse = reinterpret_cast<__gm__ uint64_t*>(
            const_cast<__gm__ uint8_t*>(x) + static_cast<uint64_t>(num_tokens) * num_hidden_bytes);
        const int base = (vec_core_idx * 16 + warp_idx) * 6;
        sparse[base] = sparse_0;
        sparse[base + 1] = sparse_1;
        sparse[base + 2] = sparse_2;
        sparse[base + 3] = sparse_3;
        sparse[base + 4] = kNumVecCores;
        sparse[base + 5] = num_sqes;
    }
"""
    text = once(text, marker, record + marker)
    path.write_text(text)
    patch = "".join(difflib.unified_diff(original.splitlines(True), text.splitlines(True), fromfile="dispatch.hpp", tofile="dispatch.hpp"))
    path = target / "tests/ep/test_ep.py"
    original = text = path.read_text()
    marker = "        if use_fp8_dispatch:\n            x_data, sf = per_token_cast_to_fp8(x)"
    text = once(text, marker, """        assert not use_fp8_dispatch
        trace_words = 64 * 16 * 6
        traced_input = torch.empty(num_tokens * hidden + trace_words * 4, dtype=torch.bfloat16, device='npu')
        traced_input[:num_tokens * hidden].copy_(x.flatten())
        x = traced_input[:num_tokens * hidden].view(num_tokens, hidden)
        sparse_trace = traced_input[num_tokens * hidden:].view(torch.int64)
""" + marker)
    marker = "                updated_x = torch.randn((num_tokens, hidden), dtype=torch.bfloat16, device='npu') * (8 if send_weights else 0.125)"
    text = once(text, marker, marker + """
                updated_storage = torch.empty(num_tokens * hidden + trace_words * 4, dtype=torch.bfloat16, device='npu')
                updated_storage[:num_tokens * hidden].copy_(updated_x.flatten())
                updated_x = updated_storage[:num_tokens * hidden].view(num_tokens, hidden)
""")
    marker = "        if not args.skip_perf_test:\n            num_unaligned_expanded_tokens"
    text = once(text, marker, """        import json
        previous_barrier = deep_ep._C.set_barrier_in_prologue(False)
        trace_l2 = torch.empty(1 << 26, dtype=torch.int, device='npu')
        samples = []
        try:
            for sample in range(25):
                sparse_trace.zero_()
                trace_l2.zero_()
                torch.npu.synchronize()
                launch(buffer, 'dispatch', args.defer_epilogue, **dispatch_args)
                torch.npu.synchronize()
                if sample >= 5:
                    samples.append(sparse_trace.cpu().tolist())
        finally:
            deep_ep._C.set_barrier_in_prologue(previous_barrier)
        print('SPARSE_TIMING ' + json.dumps(dict(rank=buffer.rank_idx, samples=samples)), flush=True)
""" + marker)
    if "--cached" in sys.argv[3:]:
        text = once(text,
                    "                launch(buffer, 'dispatch', args.defer_epilogue, **dispatch_args)",
                    "                launch(buffer, 'dispatch', args.defer_epilogue, **cached_dispatch_args)")
    path.write_text(text)
    patch += "".join(difflib.unified_diff(original.splitlines(True), text.splitlines(True), fromfile="test_ep.py", tofile="test_ep.py"))
    (dest / (name + ".patch")).write_text(patch)
    print(target, flush=True)
