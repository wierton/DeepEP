"""Restore original peer/Jetty initialization overlap and wide EID loads through APIs."""
import difflib
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]).resolve() / "jetty-init-study"
source = root / "known-batch"
target = root / "known-batch-init"
shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "verified*", ".git"))
inc = target / "deep_ep/include"
for path in inc.rglob("*"):
    if path.suffix in (".h", ".hpp"):
        path.write_text(path.read_text().replace(str(source), str(target)))
patches = []
def edit(path, old, new):
    text = path.read_text()
    assert text.count(old) == 1
    changed = text.replace(old, new)
    path.write_text(changed)
    patches.append("".join(difflib.unified_diff(text.splitlines(True), changed.splitlines(True),
                       fromfile=str(path.relative_to(target)), tofile=str(path.relative_to(target)))))
dispatch = inc / "deep_ep/impls/ep/dispatch.hpp"
edit(dispatch, "    if (thread_idx == 0 and kNumRanks > 1) {",
     "    if (warp_idx == 1 and lane_idx == 0 and kNumRanks > 1) {")
header = inc / "impl/comm_api/aicore/jetty/impl/platform_v310/hcomm_simt_urma_jetty.h"
text = header.read_text()
begin = text.index("        uint64_t remoteEidL = 0U;")
end = text.index("        info->remoteTokenValueUdf", begin)
edit(header, text[begin:end], """#ifdef EP_DISPATCH_ABLATION
        static_assert(offsetof(SqContext, contextInfo.ubJfs.remoteEID) % alignof(uint64_t) == 0);
        const auto* eid = reinterpret_cast<__gm__ const uint64_t*>(sq->contextInfo.ubJfs.remoteEID);
        info->remoteEid[0] = eid[0];
        info->remoteEid[1] = eid[1];
#else
""" + text[begin:end] + "#endif\n")
(root / "known-batch-init.patch").write_text("".join(patches))
print(target)
