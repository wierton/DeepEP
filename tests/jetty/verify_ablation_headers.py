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
for variant in ("control", "no-start-drain", "no-entry-sync", "no-last-atomic", "cached-tail", "cached-tail-local"):
    if not (root / variant).exists():
        continue
    metas = sorted((root / f"cache-{variant}/cache").glob("dispatch.*/meta.json"))
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
        print(variant, meta.parent.name, "overlay verified", flush=True)
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
                static_atomic_max=len(re.findall(r"\tRED\.S\.MAX\.u32\.NMFV\s", result.stdout))))
(root / "instruction-summary.json").write_text(json.dumps(records, indent=2) + "\n")
