"""Test a batch-size API: set final flags during fill, retaining AdvanceSq/publication."""
import difflib
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]).resolve() / "jetty-init-study"
source = root / "unchecked-fixed-batch"
target = root / "known-batch"
shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "verified*", ".git"))
inc = target / "deep_ep/include"
for path in inc.rglob("*"):
    if path.suffix in (".h", ".hpp"):
        path.write_text(path.read_text().replace(str(source), str(target)))
impl = inc / "impl/comm_api/aicore/jetty/impl/platform_v310"
patches = []

def edit(path, old, new):
    text = path.read_text()
    assert text.count(old) == 1, (path, old, text.count(old))
    changed = text.replace(old, new)
    path.write_text(changed)
    patches.append("".join(difflib.unified_diff(text.splitlines(True), changed.splitlines(True),
                       fromfile=str(path.relative_to(target)), tofile=str(path.relative_to(target)))))

definition = impl / "hcomm_simt_urma_jetty_def.h"
edit(definition, "        uint32_t failed;", "        uint32_t failed;\n        uint32_t expectedWqebbs;")
edit(definition, "class JettyImpl {\npublic:", """class JettyImpl {
public:
    __simt_callee__ __forceinline__ void BeginBatch(uint32_t totalWqebbs) const {
        Batch()->expectedWqebbs = totalWqebbs;
    }""")
public = inc / "comm_api/aicore/jetty/hcomm_jetty_simt.h"
edit(public, "private:\n    JettyImpl impl_;", """    __simt_callee__ __forceinline__ void BeginBatch(uint32_t totalWqebbs) const {
        impl_.BeginBatch(totalWqebbs);
    }
private:
    JettyImpl impl_;""")
header = impl / "hcomm_simt_urma_jetty.h"
edit(header, "        batch->failed = 0U;\n    }\n    HCOMM_SIMT_DEBUG_TRAP_IF(channelAddr", "        batch->failed = 0U;\n        batch->expectedWqebbs = 0U;\n    }\n    HCOMM_SIMT_DEBUG_TRAP_IF(channelAddr")
edit(header, "                value = HCOMM_SIMT_WRITE_HEADER | sqIdx;", """                value = HCOMM_SIMT_WRITE_HEADER | sqIdx;
#ifdef EP_DISPATCH_ABLATION
                // Fixed-size ordinary Write batch: same final header as AdvanceBatch,
                // produced here rather than read/modified after all warp writes.
                const uint32_t batchEnd = static_cast<uint32_t>(info->packedHead) + Batch()->expectedWqebbs;
                if (head + HCOMM_SIMT_SLOT_WQEBBS == batchEnd)
                    value = (value & ~0xFFFFULL) | (batchEnd & 0xFFFFU) | HCOMM_SIMT_FINAL_FLAGS;
#endif""")
text = header.read_text()
begin = text.index("#ifdef EP_DISPATCH_ABLATION\n    const uint32_t last", text.index("JettyImpl::AdvanceBatch()"))
end = text.index("    asc_threadfence();", begin)
edit(header, text[begin:end], "#ifndef EP_DISPATCH_ABLATION\n" + text[begin:end] + "#endif\n")
dispatch = inc / "deep_ep/impls/ep/dispatch.hpp"
marker = "            const_cast<__gm__ uint64_t*>(jetty_ptrs), vec_core_idx);"
edit(dispatch, marker, marker + "\n        jetty.BeginBatch(ub_layout->lsqe_counter * kNumWQEBBsPerSQESlot);")
(root / "known-batch.patch").write_text("".join(patches))
print(target)
