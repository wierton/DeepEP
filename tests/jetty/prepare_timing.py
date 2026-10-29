"""Instrument complete original/migrated Dispatch without deleting operations.

The input allocation has a private diagnostic tail outside the logical tensor.
No kernel ABI or Host extension changes; both variants use identical probes.
"""
import difflib
import pathlib
import re
import shutil
import sys

root = pathlib.Path(sys.argv[1]).resolve()
detail = "--detail" in sys.argv[2:]
destination = root / ("jetty-timing-detail" if detail else "jetty-timing-v3")
rel = pathlib.Path("deep_ep/include/deep_ep/impls/ep/dispatch.hpp")


def once(text, old, new):
    assert text.count(old) == 1, (old, text.count(old))
    return text.replace(old, new)


for name, src in (("original", "DeepEP-baseline"), ("migrated", "DeepEP-Ascend")):
    out = destination / name
    shutil.copytree(root / src, out, ignore=shutil.ignore_patterns(".git", "__pycache__", "build", "logs", ".jit-cache", "extra-info"))
    original = code = (out / rel).read_text()
    if detail:
        from timing_detail import instrument
        code = instrument(code, name, once)
    init_marker = "    // Load descriptors while" if name == "original" else "    // These constructors initialize"
    code = once(code, init_marker, "    const uint64_t trace_init0 = clock64();\n" + init_marker)
    marker = "    if constexpr (not kCachedMode) {\n        // scatter rank counts"
    code = once(code, marker, "    const uint64_t trace_init1 = clock64();\n" + marker)
    marker = "    // Write SQEs" if name == "original" else "    // Each warp supplies token/metadata SGEs;"
    code = once(code, marker, "    const uint64_t trace_build0 = clock64();\n" + marker)
    if name == "migrated":
        if detail:
            marker = "    const uint64_t trace_batch0 = clock64();"
            code = once(code, marker, "    const uint64_t trace_build1 = clock64();\n" + marker)
            marker = "    trace_batch_wait = trace_advance0 - trace_batch0;"
            code = once(code, marker, marker + "\n    const uint64_t trace_finish1 = clock64();")
        else:
            marker = "    asc_syncthreads();\n    if (thread_idx == 0 && num_sqes > 0) jetty.AdvanceSq();"
            code = once(code, marker, "    const uint64_t trace_build1 = clock64();\n" + marker +
                        "\n    const uint64_t trace_finish1 = clock64();")
    else:
        marker = "    while (ub_layout->metadata_ready != 1)"
        code = once(code, marker, "    const uint64_t trace_build1 = clock64();\n"
                    "    const uint64_t trace_finish1 = trace_build1; // Original finalizes on scalar.\n" + marker)
    # Emit after the measured regions; include cached-mode early exit as well.
    marker = "    while (ub_layout->metadata_ready != 1)"
    record = """    const uint64_t trace_clock0 = clock64();
    const uint64_t trace_clock1 = clock64();
    if (lane_idx == 0) {
        auto* trace = reinterpret_cast<__gm__ uint64_t*>(
            const_cast<__gm__ uint8_t*>(x) + static_cast<uint64_t>(num_tokens) * num_hidden_bytes);
        const int base = (vec_core_idx * 16 + warp_idx) * 8;
        trace[base + 0] = trace_init0;
        trace[base + 1] = trace_init1;
        trace[base + 2] = trace_build0;
        trace[base + 3] = trace_build1;
        trace[base + 4] = trace_finish1;
        trace[base + 5] = num_sqes;
        trace[base + 6] = trace_clock0;
        trace[base + 7] = trace_clock1;
    }
"""
    code = once(code, marker, record + marker)
    if detail:
        extra = """        trace[base + 8] = trace_peer_max;
        trace[base + 9] = trace_jetty_max;
        trace[base + 10] = trace_init_wait;
        trace[base + 11] = trace_prep_sum;
        trace[base + 12] = trace_emit_sum;
        trace[base + 13] = trace_requests;
        trace[base + 14] = trace_batch_wait;
        trace[base + 15] = trace_advance;
"""
        code = once(code, "        trace[base + 7] = trace_clock1;\n", "        trace[base + 7] = trace_clock1;\n" + extra)
        code = once(code, "    if (lane_idx == 0) {\n        auto* trace", """    // reduce_max has only 32-bit integer overloads; flag overflow for the parser.
    const uint64_t trace_peer_max = __reduce_max(static_cast<uint32_t>(trace_peer_ticks >> 32)) ?
        UINT64_MAX : __reduce_max(static_cast<uint32_t>(trace_peer_ticks));
    const uint64_t trace_jetty_max = __reduce_max(static_cast<uint32_t>(trace_jetty_ticks >> 32)) ?
        UINT64_MAX : __reduce_max(static_cast<uint32_t>(trace_jetty_ticks));
    if (lane_idx == 0) {
        auto* trace""")
        code = code.replace("(vec_core_idx * 16 + warp_idx) * 8", "(vec_core_idx * 16 + warp_idx) * 16")
    marker = "    // Wait until SIMT finishes routing and count reduction"
    code = once(code, marker, "    const uint64_t trace_join0 = asc_get_system_cycle();\n" + marker)
    marker = "    asc_sync_wait(PIPE_V, PIPE_S, EVENT_ID0);\n"
    # Only final VF join uses this exact event in this kernel source.
    code = once(code, marker, marker + "    const uint64_t trace_join1 = asc_get_system_cycle();\n")
    marker = "    // benchmark only: wait for all urma traffic to measure URMA latency"
    code = once(code, marker, "    const uint64_t trace_publish1 = asc_get_system_cycle();\n    const uint64_t trace_sqes = ub_layout->lsqe_counter;\n" + marker)
    marker = "\n}\n\n}  // namespace deep_ep"
    code = once(code, marker, """
    const uint64_t trace_barrier1 = asc_get_system_cycle();
    auto* trace = reinterpret_cast<__gm__ uint64_t*>(
        static_cast<__gm__ uint8_t*>(x) + static_cast<uint64_t>(num_tokens) * kNumHiddenBytes);
    const int base = 64 * 16 * 8 + vec_core_idx * 8;
    trace[base + 0] = trace_join0;
    trace[base + 1] = trace_join1;
    trace[base + 2] = trace_publish1;
    trace[base + 3] = trace_barrier1;
    trace[base + 4] = kDoBarrier;
    trace[base + 5] = trace_sqes;
    trace[base + 6] = kNumVecCores;
""" + marker)
    code = re.sub(r"^    trace\[base \+ (\d+)\] = (.*);$",
                  lambda m: f"    AscendC::WriteGmByPassDCache(trace + base + {m[1]}, static_cast<uint64_t>({m[2]}));", code, flags=re.M)
    if detail:
        code = code.replace("64 * 16 * 8 + vec_core_idx", "64 * 16 * 16 + vec_core_idx")
    (out / rel).write_text(code)
    patch = "".join(difflib.unified_diff(original.splitlines(True), code.splitlines(True), fromfile=str(rel), tofile=str(rel)))
    testfile = out / "tests/ep/test_ep.py"
    test_original = test = testfile.read_text()
    marker = "        if use_fp8_dispatch:\n            x_data, sf = per_token_cast_to_fp8(x)"
    test = once(test, marker, """        assert not use_fp8_dispatch, 'Timing fixture is BF16-only'
        trace_words = 64 * 16 * 8 + 64 * 8
        traced_input = torch.empty(num_tokens * hidden + trace_words * 4, dtype=torch.bfloat16, device='npu')
        traced_input[:num_tokens * hidden].copy_(x.flatten())
        x = traced_input[:num_tokens * hidden].view(num_tokens, hidden)
        jetty_trace = traced_input[num_tokens * hidden:].view(torch.int64)
""" + marker)
    marker = "        if not args.skip_perf_test:\n            num_unaligned_expanded_tokens"
    test = once(test, marker, """        # Full algorithm and original correctness checks above are retained.
        # Collect AFTER completion; no printf, extra fence, or trace copy inside a timed region.
        import json
        previous_barrier = deep_ep._C.set_barrier_in_prologue(True)
        trace_l2 = torch.empty(1 << 26, dtype=torch.int, device='npu')
        samples = []
        try:
            for sample in range(25):
                jetty_trace.zero_()
                trace_l2.zero_()
                torch.npu.synchronize()
                launch(buffer, 'dispatch', args.defer_epilogue, **dispatch_args)
                torch.npu.synchronize()
                if sample >= 5:
                    samples.append(jetty_trace.cpu().tolist())
        finally:
            deep_ep._C.set_barrier_in_prologue(previous_barrier)
        print('JETTY_TIMING ' + json.dumps(dict(rank=buffer.rank_idx, samples=samples)), flush=True)
""" + marker)
    if detail:
        test = test.replace("trace_words = 64 * 16 * 8 + 64 * 8", "trace_words = 64 * 16 * 16 + 64 * 8")
    testfile.write_text(test)
    patch += "".join(difflib.unified_diff(test_original.splitlines(True), test.splitlines(True), fromfile="tests/ep/test_ep.py", tofile="tests/ep/test_ep.py"))
    (destination / (name + ".patch")).write_text(patch)
    print(out, flush=True)
