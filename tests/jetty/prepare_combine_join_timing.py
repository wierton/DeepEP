"""Split VF join/Advance on warp zero; no other warp writes timing records."""
import difflib
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]).resolve() / "jetty-init-study"
for variant in ("original", "control"):
    source = root / f"combine-vf-{variant}"
    target = root / f"combine-join-{variant}"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git", "__pycache__", "verified*"))
    for path in (target / "deep_ep/include").rglob("*"):
        if path.suffix in (".h", ".hpp"):
            path.write_text(path.read_text().replace(str(source), str(target)))
    path = target / "deep_ep/include/deep_ep/impls/ep/combine.hpp"
    before = text = path.read_text()
    if variant == "control":
        marker = "    asc_syncthreads();"
        assert text.count(marker) == 1
        text = text.replace(marker, marker + "\n    const uint64_t vf_join = clock64();")
    else:
        text = text.replace("    const uint64_t vf3 = clock64();", "    const uint64_t vf_join = clock64();\n    const uint64_t vf3 = clock64();")
    marker = "if (lane_idx == 0) {\n        auto* record = vf_trace"
    assert text.count(marker) == 1
    text = text.replace(marker, "if (thread_idx == 0) {\n        auto* record = vf_trace")
    text = text.replace("record[6] = task.num_tokens;", "record[6] = vf_join;")
    path.write_text(text)
    (root / f"combine-join-{variant}.patch").write_text("".join(difflib.unified_diff(
        before.splitlines(True), text.splitlines(True), fromfile="combine-vf/combine.hpp", tofile="combine-join/combine.hpp")))
    # Same oversized dedicated prefix; only warp zero's eight words are written.
    path = target / "tests/ep/test_ep.py"
    path.write_text(path.read_text().replace("COMBINE_VF ", "COMBINE_JOIN "))
    print(target, flush=True)
