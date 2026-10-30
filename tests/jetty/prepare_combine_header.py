"""Ordinary-Write-only AdvanceBatch header reconstruction, preserving publication."""
import difflib
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]).resolve() / "jetty-init-study"
timed = "--timed" in sys.argv
with_init = "--with-init" in sys.argv
assert not (timed and with_init), "Init combination is a separate probe-free experiment"
source = root / ("combine-init" if with_init else "combine-join-control" if timed else "fixed-control")
target = root / ("combine-init-header" if with_init else "combine-join-known-header" if timed else "combine-known-header")
shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git", "__pycache__", "verified*"))
for path in (target / "deep_ep/include").rglob("*"):
    if path.suffix in (".h", ".hpp"):
        path.write_text(path.read_text().replace(str(source), str(target)))
patches = []
def edit(path, old, new):
    before = path.read_text()
    assert before.count(old) == 1, (path, old)
    after = before.replace(old, new)
    path.write_text(after)
    patches.append("".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
        fromfile=str(path.relative_to(target)), tofile=str(path.relative_to(target)))))

inc = target / "deep_ep/include"
edit(inc / "deep_ep/impls/ep/combine.hpp", "#pragma once\n", """#pragma once
// This experiment's SIMT batches contain only ordinary Write<2, false>.
// Do not apply the header reconstruction to inline/notify/mixed batches.
#define EP_COMBINE_KNOWN_WRITE_HEADER 1
""")
header = inc / "impl/comm_api/aicore/jetty/impl/platform_v310/hcomm_simt_urma_jetty.h"
old = """    const uint64_t firstWord =
        *reinterpret_cast<__gm__ uint64_t*>(sq + static_cast<uint64_t>(idx) * HCOMM_SIMT_WQEBB_LANES);"""
edit(header, old, """#ifdef EP_COMBINE_KNOWN_WRITE_HEADER
    // FillSqWarpLane emits exactly WRITE_HEADER | sqIdx for an ordinary Write.
    // Preserve any index bits above bit 15 too; the update below replaces only PI.
    const uint64_t firstWord = HCOMM_SIMT_WRITE_HEADER | idx;
#else
""" + old + "\n#endif")
(root / f"{target.name}.patch").write_text("".join(patches))
print(target)
