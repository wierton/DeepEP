"""Sparse scalar-only probes: do not instrument SIMT workers or per-request APIs."""
import difflib
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]).resolve()
dest = root / "jetty-init-study"

def once(text, old, new):
    assert text.count(old) == 1, (old, text.count(old))
    return text.replace(old, new)

for name, source in (("scalar-original", root / "DeepEP-baseline"),
                     ("scalar-control", dest / "fixed-control"),
                     ("scalar-unchecked", dest / "unchecked-fixed-batch")):
    target = dest / name
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git", "__pycache__", "build", "logs", "verified*"))
    # Preserve isolated overlay selection for the two migrated versions.
    for path in (target / "deep_ep/include").rglob("*"):
        if path.suffix in (".h", ".hpp"):
            text = path.read_text()
            path.write_text(text.replace(str(source), str(target)))
    path = target / "deep_ep/include/deep_ep/impls/ep/dispatch.hpp"
    original = text = path.read_text()
    markers = [
        "    // Barrier before dispatch",
        "    // Do notify, SQE writes and local copy pipeline",
        "    auto preload_metadata =",
        "    ub_layout->metadata_ready = 1;",
        "    for (int stage_idx = 0; stage_idx < kNumHiddenMTEStages; ++ stage_idx)\n        asc_sync_notify",
        "    // Wait until SIMT finishes routing and count reduction",
    ]
    for i, marker in enumerate(markers):
        text = once(text, marker, f"    const uint64_t scalar_probe_{i} = asc_get_system_cycle();\n" + marker)
    marker = "    asc_sync_wait(PIPE_V, PIPE_S, EVENT_ID0);\n"
    text = once(text, marker, marker + "    const uint64_t scalar_probe_6 = asc_get_system_cycle();\n")
    marker = "    // benchmark only: wait for all urma traffic to measure URMA latency"
    text = once(text, marker, "    const uint64_t scalar_probe_7 = asc_get_system_cycle();\n" + marker)
    store = """
    auto* scalar_trace = reinterpret_cast<__gm__ uint64_t*>(
        static_cast<__gm__ uint8_t*>(x) + static_cast<uint64_t>(num_tokens) * kNumHiddenBytes);
"""
    for i in range(8):
        store += f"    AscendC::WriteGmByPassDCache(scalar_trace + vec_core_idx * 10 + {i}, scalar_probe_{i});\n"
    store += "    AscendC::WriteGmByPassDCache(scalar_trace + vec_core_idx * 10 + 8, static_cast<uint64_t>(kNumVecCores));\n"
    store += "    AscendC::WriteGmByPassDCache(scalar_trace + vec_core_idx * 10 + 9, static_cast<uint64_t>(kDoBarrier));\n"
    text = once(text, "\n}\n\n}  // namespace deep_ep", store + "\n}\n\n}  // namespace deep_ep")
    path.write_text(text)
    patch = "".join(difflib.unified_diff(original.splitlines(True), text.splitlines(True), fromfile="dispatch.hpp", tofile="dispatch.hpp"))
    path = target / "tests/ep/test_ep.py"
    original = text = path.read_text()
    marker = "        if use_fp8_dispatch:\n            x_data, sf = per_token_cast_to_fp8(x)"
    text = once(text, marker, """        assert not use_fp8_dispatch
        trace_words = 64 * 10
        traced_input = torch.empty(num_tokens * hidden + trace_words * 4, dtype=torch.bfloat16, device='npu')
        traced_input[:num_tokens * hidden].copy_(x.flatten())
        x = traced_input[:num_tokens * hidden].view(num_tokens, hidden)
        scalar_trace = traced_input[num_tokens * hidden:].view(torch.int64)
""" + marker)
    marker = "        if not args.skip_perf_test:\n            num_unaligned_expanded_tokens"
    text = once(text, marker, """        import json
        previous_barrier = deep_ep._C.set_barrier_in_prologue(False)
        trace_l2 = torch.empty(1 << 26, dtype=torch.int, device='npu')
        samples = []
        try:
            for sample in range(25):
                scalar_trace.zero_()
                trace_l2.zero_()
                torch.npu.synchronize()
                launch(buffer, 'dispatch', args.defer_epilogue, **dispatch_args)
                torch.npu.synchronize()
                if sample >= 5:
                    samples.append(scalar_trace.cpu().tolist())
        finally:
            deep_ep._C.set_barrier_in_prologue(previous_barrier)
        print('SCALAR_TIMING ' + json.dumps(dict(rank=buffer.rank_idx, samples=samples)), flush=True)
""" + marker)
    path.write_text(text)
    patch += "".join(difflib.unified_diff(original.splitlines(True), text.splitlines(True), fromfile="test_ep.py", tofile="test_ep.py"))
    (dest / (name + ".patch")).write_text(patch)
    print(target, flush=True)
