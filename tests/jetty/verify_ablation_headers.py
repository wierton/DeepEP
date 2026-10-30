"""Replay the recorded JIT command as preprocessing and verify header selection."""
import json
import hashlib
import pathlib
import re
import shlex
import subprocess
import sys

root = pathlib.Path(sys.argv[1]).resolve()
records = []
variants = ("control", "overlap", "wide-eid", "both", "direct-header", "all") if "--init" in sys.argv else ("control", "no-start-drain", "no-entry-sync", "no-last-atomic", "cached-tail", "cached-tail-local")
selected = [v for v in sys.argv[2:] if not v.startswith("--")]
if selected:
    variants = selected
for variant in variants:
    if not (root / variant).exists():
        continue
    metas = sorted((root / f"cache-{variant}/cache").glob("dispatch.*/meta.json"))
    if not metas:
        metas = sorted((root / f"cache-fixed-{variant}/cache").glob("dispatch.*/meta.json"))
    preflight = not metas
    if preflight:
        metas = sorted((root.parent / "jetty-ablation/cache-control/cache").glob("dispatch.*/meta.json"))[:1]
    assert metas, "No recorded compiler command available"
    for meta in metas:
        command = json.loads(meta.read_text())["command"].split(" && ")[0]
        args = shlex.split(command)
        if preflight:
            args = ["-I" + str(root / variant / "deep_ep/include")
                    if arg.startswith("-I") and arg.endswith("/deep_ep/include") else arg for arg in args]
        args = [str(meta.parent / "kernel.asc") if arg.endswith("/kernel.asc") else arg for arg in args]
        out_index = args.index("-o")
        output = root / variant / "verified-preflight.ii" if preflight else meta.parent / "verified.ii"
        del args[out_index:out_index + 2]
        args.append("-E")
        result = subprocess.run(args, check=True, capture_output=True, text=True)
        text = result.stdout
        output.write_text(text)
        expected = str(root / variant / "deep_ep/include/impl/comm_api/aicore/jetty/impl/platform_v310/hcomm_simt_urma_jetty.h")
        assert expected in text, (variant, "overlay not selected")
        if variant == "unchecked-fixed-batch":
            start = text.index("uint32_t JettyImpl::ReserveBatchSq(")
            end = text.index("\n}", start)
            reserve = text[start:end]
            assert "return base + offset;" in reserve
            assert "completionTailAddr" not in reserve
            assert "asc_atomic_max" not in reserve
            assert "requiredFreeWqebbs" not in reserve
            assert "begin + count - HCOMM_SIMT_SLOT_WQEBBS" in text
        if variant in ("wide-eid", "both") or variant.startswith("all"):
            assert "info->remoteEid[0] = eid[0];" in text
        if variant in ("overlap", "both") or variant.startswith("all"):
            assert "warp_idx == 1 and lane_idx == 0 and kNumRanks > 1" in text
        if variant == "direct-header" or variant.startswith("all"):
            assert "const uint64_t firstWord = HCOMM_SIMT_WRITE_HEADER;" in text
        if variant.startswith("api-"):
            expected_probe = ("traceTicks_ = clock64() - traceStart_;" if variant == "api-joint" else
                              "traceTicks_ = clock64() - traceTicks_;" if variant == "api-tracking" else
                              "traceTicks_ = clock64() - api_trace_begin;")
            assert expected_probe in text
            assert "trace_api_sum += jetty.TraceTicks();" in text
        if "uniform" in variant:
            assert "begin + count - HCOMM_SIMT_SLOT_WQEBBS" in text
            assert "asc_atomic_max(&batch->lastOffset, offset)" not in text
        if variant.startswith("all") and "tail" in variant:
            assert "const uint32_t consumed = base - batch->tailSnapshot;" in text
        # Marker proof is stronger than checking object hashes, which can differ
        # merely because paths/symbol names changed.
        if variant == "no-last-atomic":
            assert "begin + count - HCOMM_SIMT_SLOT_WQEBBS" in text
            assert "asc_atomic_max(&batch->lastOffset, offset)" not in text
        if variant == "no-start-drain":
            assert "if (jetty.template Drain<kNumTimeoutCycles>() != 0 ||" not in text
        if variant in ("cached-tail", "cached-tail-local"):
            assert "const uint32_t consumed = base - batch->tailSnapshot;" in text
        if variant == "no-entry-sync":
            assert "if constexpr (kDoCommit || !HcommCoopTraits<Coop>::kWarpCapable)" in text
        print(variant, meta.parent.name, "overlay preflight only" if preflight else "compiled overlay verified", flush=True)
        if not preflight:
            objdump = root.parent / "Ascend/cann/x86_64-linux/ccec_compiler/bin/llvm-objdump"
            result = subprocess.run([str(objdump), "-d", str(meta.parent / "kernel.o")],
                                    check=True, capture_output=True, text=True)
            (meta.parent / "verified.asm").write_text(result.stdout)
            source = (meta.parent / "kernel.asc").read_text()
            params = re.search(r"dispatch_impl<([^>]+)>", source)[1].split(",")
            records.append(dict(variant=variant, cached=params[4].strip(),
                final_barrier=params[12].strip(), kernel=meta.parent.name,
                sha256=hashlib.sha256((meta.parent / "kernel.o").read_bytes()).hexdigest(),
                static_membar=len(re.findall(r"\tMEMBAR\s", result.stdout)),
                static_cached_byte_loads=len(re.findall(r"\tLDG\.C\.NMFV\.u8\s", result.stdout)),
                static_simt_clocks=len(re.findall(r"\tS2RL\s+R\d+, CLOCK", result.stdout)),
                static_atomic_max=len(re.findall(r"\tRED\.S\.MAX\.u32\.NMFV\s", result.stdout))))
(root / "instruction-summary.json").write_text(json.dumps(records, indent=2) + "\n")
