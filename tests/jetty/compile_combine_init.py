"""Replay all recorded Combine specializations offline; this is NOT an NPU test."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

root = Path(sys.argv[1]).resolve() / "jetty-init-study"
source, target = root / "combine-control", root / "combine-init"
metas = sorted((root / "cache-fixed-combine-control/cache").glob("combine.*/meta.json"))
assert metas
records = []
for meta in metas:
    output = root / "combine-init-offline" / meta.parent.name
    output.mkdir(parents=True, exist_ok=True)
    commands = []
    for command in json.loads(meta.read_text())["command"].split(" && "):
        args = shlex.split(command)
        args = [arg.replace(str(source), str(target)) for arg in args]
        args = [str(meta.parent / "kernel.asc") if arg.endswith("/kernel.asc") else
                str(output / Path(arg).name) if arg.endswith(("/kernel.rel.o", "/kernel.o")) else arg
                for arg in args]
        commands.append(args)
    preprocess = commands[0].copy()
    index = preprocess.index("-o")
    del preprocess[index:index + 2]
    result = subprocess.run(preprocess + ["-E"], capture_output=True, text=True, check=True)
    text = result.stdout
    assert str(target / "deep_ep/include/deep_ep/impls/ep/combine.hpp") in text
    assert "peer_idx == math::align(kNumRanks, warpSize)" in text
    assert "info->remoteEid[0] = eid[0];" in text
    assert "if (jetty.template Drain<kNumTimeoutCycles>() != 0)" in text
    assert "probe0" not in text
    (output / "verified.ii").write_text(text)
    log = ""
    for args in commands:
        result = subprocess.run(args, capture_output=True, text=True)
        log += shlex.join(args) + "\n" + result.stdout + result.stderr
        (output / "build.log").write_text(log)
        result.check_returncode()
    records.append(dict(specialization=meta.parent.name, source=(meta.parent / "kernel.asc").read_text(),
                        commands=commands, sha256=hashlib.sha256((output / "kernel.o").read_bytes()).hexdigest(),
                        status="compiled and linked offline; NOT executed"))
    print(meta.parent.name, "offline compile/link PASS", flush=True)
(root / "combine-init-offline/summary.json").write_text(json.dumps(records, indent=2) + "\n")
