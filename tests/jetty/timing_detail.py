"""Extra same-clock probes; retain all original operations and synchronization."""


def instrument(code, name, once):
    marker = "    // Load descriptors while" if name == "original" else "    // These constructors initialize"
    code = once(code, marker, "    uint64_t trace_peer_ticks = 0, trace_jetty_ticks = 0;\n" + marker)
    if name == "original":
        code = once(code, "    if (thread_idx < kNumRanks and thread_idx != rank_idx)\n        handle::", "    if (thread_idx < kNumRanks and thread_idx != rank_idx) {\n        const uint64_t trace_begin = clock64();\n        handle::")
        code = once(code, "    if (warp_idx == 1 and kNumRanks > 1) {", "        trace_peer_ticks = clock64() - trace_begin;\n    }\n    if (warp_idx == 1 and kNumRanks > 1) {\n        const uint64_t trace_begin = clock64();")
        marker = "        // Check SQ capacity"
    else:
        code = once(code, "    if (thread_idx < kNumRanks and thread_idx != rank_idx) {", "    if (thread_idx < kNumRanks and thread_idx != rank_idx) {\n        const uint64_t trace_begin = clock64();")
        code = once(code, "            thread_idx < rank_idx ? thread_idx : thread_idx - 1);\n    }", "            thread_idx < rank_idx ? thread_idx : thread_idx - 1);\n        trace_peer_ticks = clock64() - trace_begin;\n    }")
        code = once(code, "    if (thread_idx == 0 and kNumRanks > 1) {", "    if (thread_idx == 0 and kNumRanks > 1) {\n        const uint64_t trace_begin = clock64();")
        marker = "        if (static_cast<uint64_t>(ub_layout->lsqe_counter)"
    code = once(code, marker, "        trace_jetty_ticks = clock64() - trace_begin;\n" + marker)
    # Find the existing initialization barrier only, not other block barriers.
    start = code.index("    uint64_t trace_peer_ticks")
    pos = code.index("    asc_syncthreads();", start)
    code = code[:pos] + code[pos:].replace("    asc_syncthreads();", "    const uint64_t trace_wait0 = clock64();\n    asc_syncthreads();\n    const uint64_t trace_init_wait = clock64() - trace_wait0;", 1)
    marker = "    // Write SQEs" if name == "original" else "    // Each warp supplies token/metadata SGEs;"
    code = once(code, marker, "    uint64_t trace_prep_sum = 0, trace_emit_sum = 0, trace_requests = 0;\n    uint64_t trace_batch_wait = 0, trace_advance = 0;\n" + marker)
    loop = "    for (int i = start_lsqe_idx; i < end_lsqe_idx; ++ i) {" if name == "original" else "    for (int i = warp_idx; i < num_sqes; i += kNumWarps) {"
    code = once(code, loop, loop + "\n        const uint64_t trace_request0 = clock64();")
    marker = "        uint64_t sqe_word = 0;" if name == "original" else "        int result = -1;"
    code = once(code, marker, "        const uint64_t trace_request1 = clock64();\n" + marker)
    marker = "        // TODO(Kexing): `syncthreads`" if name == "original" else "        if (result != 0) __asc_simt_vf::__trap();"
    code = once(code, marker, "        const uint64_t trace_request2 = clock64();\n        trace_prep_sum += trace_request1 - trace_request0;\n        trace_emit_sum += trace_request2 - trace_request1;\n        ++trace_requests;\n" + marker)
    if name == "migrated":
        marker = "    asc_syncthreads();\n    if (thread_idx == 0 && num_sqes > 0) jetty.AdvanceSq();"
        # The broad-region instrumenter brackets these detailed timestamps.
        code = once(code, marker, "    const uint64_t trace_batch0 = clock64();\n" + marker)
        code = once(code, "    if (thread_idx == 0 && num_sqes > 0) jetty.AdvanceSq();", "    const uint64_t trace_advance0 = clock64();\n    if (thread_idx == 0 && num_sqes > 0) jetty.AdvanceSq();\n    trace_advance = clock64() - trace_advance0;\n    trace_batch_wait = trace_advance0 - trace_batch0;")
    return code
