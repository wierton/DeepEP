"""Replay recorded JIT commands into isolated outputs, without touching an NPU.

Usage: python compile_jetty_experiment.py ROOT SOURCE_VARIANT TARGET_VARIANT dispatch
"""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

base, source_name, target_name, kernel = sys.argv[1:]
root = Path(base).resolve() / "jetty-init-study"
source, target = root / source_name, root / target_name
assert kernel in ("dispatch", "combine") and source.is_dir() and target.is_dir()
metas = sorted((root / f"cache-fixed-{source_name}/cache").glob(f"{kernel}.*/meta.json"))
assert metas
records = []
for meta in metas:
    output = root / f"{target_name}-offline" / meta.parent.name
    output.mkdir(parents=True, exist_ok=False)
    commands = []
    for command in json.loads(meta.read_text())["command"].split(" && "):
        args = [arg.replace(str(source), str(target)) for arg in shlex.split(command)]
        args = [str(meta.parent / "kernel.asc") if arg.endswith("/kernel.asc") else
                str(output / Path(arg).name) if arg.endswith(("/kernel.rel.o", "/kernel.o")) else arg
                for arg in args]
        commands.append(args)
    preprocess = commands[0].copy()
    index = preprocess.index("-o")
    del preprocess[index:index+2]
    result = subprocess.run(preprocess + ["-E"], capture_output=True, text=True, check=True)
    assert str(target / f"deep_ep/include/deep_ep/impls/ep/{kernel}.hpp") in result.stdout
    if target_name.startswith("sparse-safe-cached-"):
        assert "const uint64_t sparse_0 = clock64();" in result.stdout
        fixture = (target / "tests/ep/test_ep.py").read_text()
        assert "updated_storage" in fixture and "**cached_dispatch_args)" in fixture
    (output / "verified.ii").write_text(result.stdout)
    log = ""
    for args in commands:
        result = subprocess.run(args, capture_output=True, text=True)
        log += shlex.join(args) + "\n" + result.stdout + result.stderr
        (output / "build.log").write_text(log)
        result.check_returncode()
    records.append(dict(specialization=meta.parent.name, commands=commands,
                        sha256=hashlib.sha256((output / "kernel.o").read_bytes()).hexdigest(),
                        status="offline compile/link PASS; NOT executed"))
    print(target_name, meta.parent.name, "offline compile/link PASS", flush=True)
(root / f"{target_name}-offline/summary.json").write_text(json.dumps(records, indent=2) + "\n")
