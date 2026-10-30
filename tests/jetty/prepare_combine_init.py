"""Combine-only API experiment: overlap Jetty/peer init, retain startup Drain."""
import difflib
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]).resolve() / "jetty-init-study"
source, target = root / "fixed-control", root / "combine-init"
shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git", "__pycache__", "build", "logs", "verified*"))
inc = target / "deep_ep/include"
for path in inc.rglob("*"):
    if path.suffix in (".h", ".hpp"):
        path.write_text(path.read_text().replace(str(source), str(target)))
patches = []

def edit(path, old, new):
    text = path.read_text()
    assert text.count(old) == 1, (old, text.count(old))
    changed = text.replace(old, new)
    path.write_text(changed)
    patches.append("".join(difflib.unified_diff(text.splitlines(True), changed.splitlines(True),
        fromfile=str(path.relative_to(target)), tofile=str(path.relative_to(target)))))

path = inc / "deep_ep/impls/ep/combine.hpp"
edit(path, "#pragma once", "#pragma once\n#define EP_COMBINE_INIT_EXPERIMENT 1")
edit(path, "__launch_bounds__(math::align(kNumRanks, warpSize))",
     "__launch_bounds__(math::align(kNumRanks, warpSize) + warpSize)")
edit(path, "    if (peer_idx == 0) {", "    if (peer_idx == math::align(kNumRanks, warpSize)) {")
edit(path, "AscendC::Simt::Dim3(math::align(kNumRanks, warpSize), 1, 1)",
     "AscendC::Simt::Dim3(math::align(kNumRanks, warpSize) + warpSize, 1, 1)")
header = inc / "impl/comm_api/aicore/jetty/impl/platform_v310/hcomm_simt_urma_jetty.h"
text = header.read_text()
begin = text.index("        uint64_t remoteEidL = 0U;")
end = text.index("        info->remoteTokenValueUdf", begin)
edit(header, text[begin:end], """#ifdef EP_COMBINE_INIT_EXPERIMENT
        static_assert(offsetof(SqContext, contextInfo.ubJfs.remoteEID) % alignof(uint64_t) == 0);
        const auto* eid = reinterpret_cast<__gm__ const uint64_t*>(sq->contextInfo.ubJfs.remoteEID);
        info->remoteEid[0] = eid[0];
        info->remoteEid[1] = eid[1];
#else
""" + text[begin:end] + "#endif\n")
(root / "combine-init.patch").write_text("".join(patches))
print(target)
