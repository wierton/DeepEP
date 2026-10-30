"""Time one public Write subregion per build, without removing any operation.

Uses disposable overlays prepared by prepare_ablation.py. Extra timestamps stay
in the per-thread Jetty object and are exported after each measured Write.
"""
import difflib
import pathlib
import subprocess
import sys

root = pathlib.Path(sys.argv[1]).resolve()
variants = sys.argv[2:] or ["api-entry", "api-reserve", "api-fill", "api-fence"]
subprocess.run([sys.executable, str(pathlib.Path(__file__).with_name("prepare_ablation.py")),
                str(root), "--init", "--timed", *variants], check=True)


def once(text, old, new):
    assert text.count(old) == 1, (old, text.count(old))
    return text.replace(old, new)


for variant in variants:
    repo = root / "jetty-init-timed" / variant
    inc = repo / "deep_ep/include"
    impl = inc / "impl/comm_api/aicore/jetty/impl/platform_v310"
    paths = [impl / "hcomm_simt_urma_jetty_def.h", impl / "hcomm_simt_urma_jetty.h",
             inc / "comm_api/aicore/jetty/hcomm_jetty_simt.h",
             inc / "deep_ep/impls/ep/dispatch.hpp", repo / "tests/ep/test_ep.py"]
    old = {p: p.read_text() for p in paths}
    text = old[paths[0]]
    text = once(text, "class JettyImpl {\npublic:", """class JettyImpl {
public:
#ifdef EP_DISPATCH_API_TIMING
    mutable uint64_t traceTicks_ = 0;
#endif""")
    paths[0].write_text(text)
    text = old[paths[2]]
    text = once(text, "private:\n    JettyImpl impl_;", """#ifdef EP_DISPATCH_API_TIMING
    __simt_callee__ __forceinline__ uint64_t TraceTicks() const { return impl_.traceTicks_; }
#endif
private:
    JettyImpl impl_;""")
    paths[2].write_text(text)
    text = old[paths[1]]
    begin = text.index("JettyImpl::PostSend(")
    end = text.index("JettyImpl::AdvanceBatch()", begin)
    body = text[begin:end]
    markers = {
        "api-entry": ("    const uint32_t rank =", "    constexpr uint32_t kStaticPayloadWqebbs"),
        "api-reserve": ("    // Reserve once per warp;", "    FillSqWarpLane<"),
        "api-fill": ("    FillSqWarpLane<", "    asc_threadfence();"),
        "api-fence": ("    asc_threadfence();", "    if constexpr (kDoCommit) {\n        AdvanceSq(coop);")}
    if variant in ("api-admission", "api-tracking"):
        reserve_begin = text.index("JettyImpl::ReserveBatchSq(")
        reserve = text[reserve_begin:begin]
        if variant == "api-admission":
            marker = "    const uint32_t offset = asc_atomic_add"
            reserve = once(reserve, marker, "#ifdef EP_DISPATCH_API_TIMING\n    const uint64_t api_trace_begin = clock64();\n#endif\n" + marker)
            marker = "        asc_atomic_max(&batch->lastOffset, offset);"
            reserve = once(reserve, marker, "#ifdef EP_DISPATCH_API_TIMING\n        traceTicks_ = clock64() - api_trace_begin;\n#endif\n" + marker)
        else:
            marker = "        asc_atomic_max(&batch->lastOffset, offset);"
            reserve = once(reserve, marker, "#ifdef EP_DISPATCH_API_TIMING\n        traceTicks_ = clock64();\n#endif\n" + marker)
            marker = "    FillSqWarpLane<"
            # Only lane 0 has the start timestamp; lane 0 is also the lane
            # exported by the existing probe collector. Other lanes are ignored.
            body = once(body, marker, "#ifdef EP_DISPATCH_API_TIMING\n    if (laneRank == 0U) traceTicks_ = clock64() - traceTicks_;\n#endif\n" + marker)
        paths[1].write_text(text[:reserve_begin] + reserve + body + text[end:])
    else:
        start, finish = markers[variant]
        body = once(body, start, "#ifdef EP_DISPATCH_API_TIMING\n    const uint64_t api_trace_begin = clock64();\n#endif\n" + start)
        body = once(body, finish, "#ifdef EP_DISPATCH_API_TIMING\n    traceTicks_ = clock64() - api_trace_begin;\n#endif\n" + finish)
        paths[1].write_text(text[:begin] + body + text[end:])
    text = old[paths[3]]
    text = "#define EP_DISPATCH_API_TIMING 1\n" + text
    text = once(text, "    uint64_t trace_prep_sum =", "    uint64_t trace_api_sum = 0;\n    uint64_t trace_prep_sum =")
    text = once(text, "        const uint64_t trace_request2 = clock64();", "        const uint64_t trace_request2 = clock64();\n        trace_api_sum += jetty.TraceTicks();")
    text = once(text, "        trace[base + 15] = trace_advance;", "        trace[base + 15] = trace_advance;\n        trace[base + 16] = trace_api_sum;")
    text = once(text, "(vec_core_idx * 16 + warp_idx) * 16", "(vec_core_idx * 16 + warp_idx) * 24")
    text = once(text, "64 * 16 * 16 + vec_core_idx", "64 * 16 * 24 + vec_core_idx")
    paths[3].write_text(text)
    paths[4].write_text(once(old[paths[4]], "trace_words = 64 * 16 * 16 + 64 * 8", "trace_words = 64 * 16 * 24 + 64 * 8"))
    patch = "".join("".join(difflib.unified_diff(old[p].splitlines(True), p.read_text().splitlines(True),
                    fromfile=str(p.relative_to(repo)), tofile=str(p.relative_to(repo)))) for p in paths)
    (repo.parent / (variant + ".patch")).write_text(patch)
    print("Instrumented", variant, flush=True)
