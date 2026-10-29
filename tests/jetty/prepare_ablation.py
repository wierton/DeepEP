"""Create disposable, independent source/header copies; never edit installed CANN.

Run on the test host: python prepare_ablation.py /mnt/docker/o00939992
"""
import difflib
import pathlib
import shutil
import sys

root = pathlib.Path(sys.argv[1]).resolve()
init_study = "--init" in sys.argv[2:]
timed = "--timed" in sys.argv[2:]
source = root / ("jetty-timing-detail/migrated" if timed else "DeepEP-Ascend")
toolkit = root / "Ascend/cann/asc"
dest = root / ("jetty-init-timed" if timed else "jetty-init-study" if init_study else "jetty-ablation-v2")
dispatch_rel = pathlib.Path("deep_ep/include/deep_ep/impls/ep/dispatch.hpp")
header_rel = pathlib.Path("impl/comm_api/aicore/jetty/impl/platform_v310/hcomm_simt_urma_jetty.h")


def replace_once(text, old, new):
    assert text.count(old) == 1, (old, text.count(old))
    return text.replace(old, new)


requested = [v for v in sys.argv[2:] if not v.startswith("--")]
defaults = ("control", "overlap", "wide-eid", "both") if init_study else ("control", "no-start-drain", "no-entry-sync", "no-last-atomic", "cached-tail-local")
for variant in (requested or defaults):
    target = dest / variant
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git", "__pycache__", ".jit-cache", "logs", "build"))
    inc = target / "deep_ep/include"
    shutil.copytree(toolkit / "include/comm_api", inc / "comm_api")
    shutil.copytree(toolkit / "impl/comm_api", inc / "impl/comm_api")
    dispatch = (target / dispatch_rel).read_text()
    header = (inc / header_rel).read_text()
    old_dispatch, old_header = dispatch, header
    # Scope library experiments to dispatch translation units, not Combine/barrier.
    if variant in ("no-entry-sync", "no-last-atomic", "cached-tail", "cached-tail-local"):
        dispatch = "#define EP_DISPATCH_ABLATION 1\n" + dispatch
    extra_patch = ""
    if init_study and variant in ("overlap", "both", "all"):
        # Same API and metadata writes, but use warp 1 lane 0 so peer loads
        # on warp 0 can overlap. The existing block barrier still joins both.
        dispatch = replace_once(dispatch, "    if (thread_idx == 0 and kNumRanks > 1) {",
                                "    if (warp_idx == 1 and lane_idx == 0 and kNumRanks > 1) {")
    if init_study and variant in ("wide-eid", "both", "all"):
        begin = header.index("        uint64_t remoteEidL = 0U;")
        end = header.index("        info->remoteTokenValueUdf", begin)
        old = header[begin:end]
        header = replace_once(header, old, """#ifdef EP_DISPATCH_ABLATION
        // Match the original DeepEP's two aligned 64-bit EID loads.
        static_assert(offsetof(SqContext, contextInfo.ubJfs.remoteEID) % alignof(uint64_t) == 0);
        const auto* eid = reinterpret_cast<__gm__ const uint64_t*>(sq->contextInfo.ubJfs.remoteEID);
        info->remoteEid[0] = eid[0];
        info->remoteEid[1] = eid[1];
#else
""" + old + "#endif\n")
        dispatch = "#define EP_DISPATCH_ABLATION 1\n" + dispatch
    if init_study and variant in ("direct-header", "all"):
        if variant == "direct-header":
            dispatch = "#define EP_DISPATCH_ABLATION 1\n" + dispatch
        # This fixture submits only non-inline Write requests. FillSqWarpLane
        # writes WRITE_HEADER | sqIdx; the next expression discards sqIdx.
        # Keep all final flags, stores, fences, counter updates, and API calls.
        header = replace_once(header,
            "    const uint64_t firstWord =\n        *reinterpret_cast<__gm__ uint64_t*>(sq + static_cast<uint64_t>(idx) * HCOMM_SIMT_WQEBB_LANES);",
            "#ifdef EP_DISPATCH_ABLATION\n    const uint64_t firstWord = HCOMM_SIMT_WRITE_HEADER;\n#else\n    const uint64_t firstWord =\n        *reinterpret_cast<__gm__ uint64_t*>(sq + static_cast<uint64_t>(idx) * HCOMM_SIMT_WQEBB_LANES);\n#endif")
    if variant == "no-start-drain":
        dispatch = replace_once(dispatch, "if (jetty.template Drain<kNumTimeoutCycles>() != 0 ||\n            static_cast", "if (static_cast")
    if variant == "no-entry-sync":
        header = replace_once(header,
            "    if (coop.size() != 1) {\n        coop.sync();\n    }\n\n    constexpr uint32_t kStaticPayloadWqebbs",
            "    if (coop.size() != 1) {\n#ifdef EP_DISPATCH_ABLATION\n        if constexpr (kDoCommit || !HcommCoopTraits<Coop>::kWarpCapable)\n#endif\n            coop.sync();\n    }\n\n    constexpr uint32_t kStaticPayloadWqebbs")
    if variant == "no-last-atomic":
        # Dispatch reserves uniform four-WQEBB slots; after block synchronization
        # the final reservation offset is count - 4. Keep capacity checks/fences.
        header = replace_once(header, "        asc_atomic_max(&batch->lastOffset, offset);",
            "#ifndef EP_DISPATCH_ABLATION\n        asc_atomic_max(&batch->lastOffset, offset);\n#endif")
        header = replace_once(header, "    const uint32_t last = begin + batch->lastOffset;",
            "#ifdef EP_DISPATCH_ABLATION\n    const uint32_t last = begin + count - HCOMM_SIMT_SLOT_WQEBBS;\n#else\n    const uint32_t last = begin + batch->lastOffset;\n#endif")
    if variant in ("cached-tail", "cached-tail-local"):
        # Snapshot released SQ progress after the existing initial Drain. No
        # other producer may own this SQ during the dispatch batch. Subsequent
        # hardware progress only makes this capacity bound conservative.
        definition = inc / header_rel.with_name("hcomm_simt_urma_jetty_def.h")
        original = definition.read_text()
        modified = replace_once(original, "        uint32_t failed;",
            "        uint32_t failed;\n#ifdef EP_DISPATCH_ABLATION\n        uint32_t tailSnapshot;\n#endif")
        definition.write_text(modified)
        extra_patch = "".join(difflib.unified_diff(original.splitlines(True), modified.splitlines(True),
            fromfile=str(definition.relative_to(inc)), tofile=str(definition.relative_to(inc))))
        if variant == "cached-tail":
            header = replace_once(header, "    *sqTail = released;\n    return 0;",
                "    *sqTail = released;\n#ifdef EP_DISPATCH_ABLATION\n    if (stageAddr_ != 0U) Batch()->tailSnapshot = released;\n#endif\n    return 0;")
        else:
            dispatch = replace_once(dispatch,
                "            __asc_simt_vf::__trap();\n    }\n    asc_syncthreads();\n\n    if constexpr (not kCachedMode)",
                "            __asc_simt_vf::__trap();\n"
                "        // Experiment-only BatchState: three original uint32 fields, then tailSnapshot.\n"
                "        reinterpret_cast<__ubuf__ uint32_t*>(ub_layout->jetty_stage)[3] =\n"
                "            *reinterpret_cast<__gm__ uint32_t*>(ub_layout->jetty_info.completionTailAddr);\n"
                "    }\n    asc_syncthreads();\n\n    if constexpr (not kCachedMode)")
        header = replace_once(header,
            "    const uint32_t consumed = base - *reinterpret_cast<__gm__ uint32_t*>(info->completionTailAddr);",
            "#ifdef EP_DISPATCH_ABLATION\n    const uint32_t consumed = base - batch->tailSnapshot;\n#else\n    const uint32_t consumed = base - *reinterpret_cast<__gm__ uint32_t*>(info->completionTailAddr);\n#endif")
    (target / dispatch_rel).write_text(dispatch)
    (inc / header_rel).write_text(header)
    # ASC injects toolkit include directories ahead of user -I. Force public
    # header selection, then force every impl/comm_api include to this copy.
    # Identical selection changes are applied to the control as well.
    for path in (inc / "deep_ep").rglob("*.hpp"):
        text = path.read_text()
        for name in ("hcomm_jetty.h", "hcomm_jetty_simt.h"):
            text = text.replace(f"<comm_api/aicore/jetty/{name}>",
                                '<' + str(inc / "comm_api/aicore/jetty" / name) + '>')
        path.write_text(text)
    import re
    for path in (inc / "comm_api").rglob("*.h"):
        text = path.read_text()
        text = re.sub(r'"(?:\.\./)+impl/comm_api/([^"\n]+)"',
                      lambda m: '"' + str(inc / "impl/comm_api" / m[1]) + '"', text)
        path.write_text(text)
    patch = "".join(difflib.unified_diff(old_dispatch.splitlines(True), dispatch.splitlines(True), fromfile=str(dispatch_rel), tofile=str(dispatch_rel)))
    patch += "".join(difflib.unified_diff(old_header.splitlines(True), header.splitlines(True), fromfile=str(header_rel), tofile=str(header_rel)))
    (dest / (variant + ".patch")).write_text(patch + extra_patch)
    print(variant, target, flush=True)
