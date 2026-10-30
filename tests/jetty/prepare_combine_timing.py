"""Paired coarse Combine probes; keep every transfer, drain and correctness check."""
import difflib
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]).resolve()
dest = root / "jetty-init-study"

def once(text, old, new):
    assert text.count(old) == 1, (old, text.count(old))
    return text.replace(old, new)

for name, source in (("combine-original", root / "DeepEP-baseline"),
                     ("combine-control", dest / "fixed-control")):
    target = dest / name
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git", "__pycache__", "build", "logs", "verified*"))
    for path in (target / "deep_ep/include").rglob("*"):
        if path.suffix in (".h", ".hpp"):
            path.write_text(path.read_text().replace(str(source), str(target)))
    path = target / "deep_ep/include/deep_ep/impls/ep/combine.hpp"
    original = text = path.read_text()
    marker = "    comm::scalar::barrier<kNumRanks, kNumTimeoutCycles, false>(signals.common_signals, vec_core_idx);"
    text = once(text, marker, "    const uint64_t probe0 = asc_get_system_cycle();\n" + marker +
                "\n    const uint64_t probe1 = asc_get_system_cycle();")
    for i, marker in ((2, "    Bitset<kNumRanks> active_ranks = {};"),
                      (3, "    #pragma unroll 1\n"),
                      (4, "    int num_issued_remote_tasks = 0;"),
                      (5, "    asc_sync_notify(PIPE_MTE3, PIPE_S, EVENT_ID0);"),
                      (6, "    if constexpr (kDoBarrier)\n")):
        text = once(text, marker, f"    const uint64_t probe{i} = asc_get_system_cycle();\n" + marker)
    store = "\n    const uint64_t probe7 = asc_get_system_cycle();\n"
    # Private prefix, allocated by the instrumented test for EVERY Combine call.
    # No kernel arguments, communication buffers or registered windows change.
    store += "    auto* trace = reinterpret_cast<__gm__ uint64_t*>(x) - 64 * 10;\n"
    for i in range(8):
        store += f"    AscendC::WriteGmByPassDCache(trace + vec_core_idx * 10 + {i}, probe{i});\n"
    store += "    AscendC::WriteGmByPassDCache(trace + vec_core_idx * 10 + 8, static_cast<uint64_t>(kNumVecCores));\n"
    store += "    AscendC::WriteGmByPassDCache(trace + vec_core_idx * 10 + 9, static_cast<uint64_t>(kDoBarrier));\n"
    text = once(text, "\n}\n\n} // namespace deep_ep", store + "\n}\n\n} // namespace deep_ep")
    path.write_text(text)
    patch = "".join(difflib.unified_diff(original.splitlines(True), text.splitlines(True), fromfile="combine.hpp", tofile="combine.hpp"))
    path = target / "tests/ep/test_ep.py"
    original = text = path.read_text()
    marker = "        input_for_combine = torch.empty(\n            (expanded_recv_x_data.size(0) + 1, hidden), dtype=torch.bfloat16, device='npu')"
    text = once(text, marker, """        combine_storage = torch.empty(64 * 10 * 4 + (expanded_recv_x_data.size(0) + 1) * hidden,
                                      dtype=torch.bfloat16, device='npu')
        combine_trace = combine_storage[:64 * 10 * 4].view(torch.int64)
        input_for_combine = combine_storage[64 * 10 * 4:].view(-1, hidden)""")
    marker = "        if not args.skip_perf_test:\n            num_unaligned_expanded_tokens"
    text = once(text, marker, """        import json
        previous_barrier = deep_ep._C.set_barrier_in_prologue(False)
        trace_l2 = torch.empty(1 << 26, dtype=torch.int, device='npu')
        samples = []
        try:
            for sample in range(25):
                combine_trace.zero_()
                trace_l2.zero_()
                torch.npu.synchronize()
                measured_x, measured_weights, _ = launch(
                    buffer, 'combine', args.defer_epilogue,
                    x=input_for_combine, handle=handle,
                    topk_weights=expanded_recv_topk_weights, bias=bias,
                    num_sms=num_ai_cores, async_with_compute_stream=True)
                torch.npu.synchronize()
                assert torch.equal(measured_x, ref_combined_x)
                assert torch.equal(measured_weights, topk_weights)
                if sample >= 5:
                    samples.append(combine_trace.cpu().tolist())
        finally:
            deep_ep._C.set_barrier_in_prologue(previous_barrier)
        print('COMBINE_TIMING ' + json.dumps(dict(rank=buffer.rank_idx, samples=samples)), flush=True)
""" + marker)
    path.write_text(text)
    patch += "".join(difflib.unified_diff(original.splitlines(True), text.splitlines(True), fromfile="test_ep.py", tofile="test_ep.py"))
    (dest / (name + ".patch")).write_text(patch)
    print(target, flush=True)
