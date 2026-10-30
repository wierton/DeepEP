"""Test runtime SGE count through Jetty API, replacing six Write specializations."""
import difflib
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]).resolve() / "jetty-init-study"
source = root / "known-batch-init"
target = root / "dynamic-sge"
shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "verified*", ".git"))
inc = target / "deep_ep/include"
for path in inc.rglob("*"):
    if path.suffix in (".h", ".hpp"):
        path.write_text(path.read_text().replace(str(source), str(target)))
impl = inc / "impl/comm_api/aicore/jetty/impl/platform_v310"
patches = []
def edit(path, old, new):
    text = path.read_text()
    assert text.count(old) == 1, (old, text.count(old))
    changed = text.replace(old, new)
    path.write_text(changed)
    patches.append("".join(difflib.unified_diff(text.splitlines(True), changed.splitlines(True),
                       fromfile=str(path.relative_to(target)), tofile=str(path.relative_to(target)))))

definition = impl / "hcomm_simt_urma_jetty_def.h"
edit(definition, "struct PostSendArgs {\n    __gm__ void* dst;\n    const JettySge* sges;",
     "struct PostSendArgs {\n    __gm__ void* dst;\n    const JettySge* sges;\n    uint32_t dynamicSges = 0U;")
edit(definition, "class JettyImpl {\npublic:", """class JettyImpl {
public:
    template <typename Coop>
    __simt_callee__ __forceinline__ int32_t WriteDynamic(
        const HcommPeer& peer, __gm__ void* dst, const JettySge* sges, uint32_t count, const Coop& coop) const {
        static_assert(HcommCoopTraits<Coop>::kWarpCapable);
        if (count == 0U || count > HCOMM_SIMT_MAX_SGES) return -1;
        const JettySge laneSge = sges[0];
        return PostSend<HCOMM_SIMT_MAX_SGES, HcommPostOpcode::kWrite, false>(
            coop, peer, detail::PostSendArgs<>{dst, &laneSge, count});
    }""")
public = inc / "comm_api/aicore/jetty/hcomm_jetty_simt.h"
edit(public, "private:\n    JettyImpl impl_;", """    template <typename Coop>
    __simt_callee__ __forceinline__ int32_t WriteDynamic(
        const Coop& coop, const HcommPeer& peer, __gm__ void* dst, const HcommSge& sge, uint32_t count) const {
        return impl_.WriteDynamic(peer, dst, &sge, count, coop);
    }
private:
    JettyImpl impl_;""")
header = impl / "hcomm_simt_urma_jetty.h"
edit(header, "        activeSges = kSgeNum;", """        activeSges = kSgeNum;
        if constexpr (kOpcode == HcommPostOpcode::kWrite) {
            if (args.dynamicSges != 0U) activeSges = args.dynamicSges;
        }""")
edit(header, "const uint32_t activePayloadWords = kWarpLane ? kWarpPayloadWords : HCOMM_SIMT_SQE_HEADER_WORDS + activeSges * 2U;",
     "const uint32_t activePayloadWords = HCOMM_SIMT_SQE_HEADER_WORDS + activeSges * 2U;")
edit(header, "kWqebbCount > (kWarpPayloadWords + HCOMM_SIMT_WQEBB_LANES - 1U) / HCOMM_SIMT_WQEBB_LANES",
     "kWqebbCount > (activePayloadWords + HCOMM_SIMT_WQEBB_LANES - 1U) / HCOMM_SIMT_WQEBB_LANES")
dispatch = inc / "deep_ep/impls/ep/dispatch.hpp"
text = dispatch.read_text()
start = text.index("        int result = -1;\n        switch (pairs)")
end = text.index("        if (result != 0)", start)
edit(dispatch, text[start:end], "        const int result = jetty.WriteDynamic(warp, peer, dst, sge, pairs * 2U);\n")
(root / "dynamic-sge.patch").write_text("".join(patches))
print(target)
