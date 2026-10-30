"""Match original warp-cooperative descriptor loading inside the Jetty API."""
import difflib
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]).resolve() / "jetty-init-study"
source = root / "dynamic-sge"
target = root / "warp-init"
shutil.copytree(source, target, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "verified*", ".git"))
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
edit(definition, "class JettyImpl {\npublic:", """class JettyImpl {
public:
    __simt_callee__ __forceinline__ void LoadWarp(__gm__ void* table, uint32_t index, uint32_t lane) const;
""")
public = inc / "comm_api/aicore/jetty/hcomm_jetty_simt.h"
edit(public, "private:\n    JettyImpl impl_;", """    __simt_callee__ __forceinline__ void LoadWarp(__gm__ void* table, uint32_t index, uint32_t lane) const {
        impl_.LoadWarp(table, index, lane);
    }
private:
    JettyImpl impl_;""")
header = impl / "hcomm_simt_urma_jetty.h"
marker = "__simt_callee__ __forceinline__ void JettyImpl::Init("
edit(header, marker, """__simt_callee__ __forceinline__ void JettyImpl::LoadWarp(
    __gm__ void* table, uint32_t index, uint32_t lane) const
{
    if (lane >= 12U) return;
    const uint64_t channel = reinterpret_cast<__gm__ const uint64_t*>(table)[index];
    const uint64_t sqAddr = detail::Load<uint64_t>(channel, offsetof(ChannelEntity, sqContextAddr));
    const uint64_t cqAddr = detail::Load<uint64_t>(channel, offsetof(ChannelEntity, cqContextAddr));
    auto* sq = reinterpret_cast<__gm__ SqContext*>(sqAddr) + HCOMM_URMA_DEFAULT_QP_IDX;
    auto* cq = reinterpret_cast<__gm__ CqContext*>(cqAddr) + HCOMM_URMA_DEFAULT_QP_IDX;
    auto* info = Info();
    if (lane < 8U) {
        __gm__ uint64_t* src = nullptr;
        switch (lane) {
            case 0: src = &sq->contextInfo.ubJfs.sqVa; break;
            case 1: src = &sq->contextInfo.ubJfs.headAddr; break;
            case 2: src = &sq->contextInfo.ubJfs.tailAddr; break;
            case 3: src = &sq->contextInfo.ubJfs.dbVa; break;
            case 4: src = &cq->contextInfo.ubJfc.scqVa; break;
            case 5: src = &cq->contextInfo.ubJfc.tailAddr; break;
            case 6: src = &cq->contextInfo.ubJfc.dbVa; break;
            case 7: src = reinterpret_cast<__gm__ uint64_t*>(sq->contextInfo.ubJfs.headAddr); break;
        }
        reinterpret_cast<__ubuf__ uint64_t*>(info)[lane] = asc_ldcg(src);
    } else {
        __gm__ uint32_t* src = nullptr;
        switch (lane) {
            case 8: src = &sq->contextInfo.ubJfs.wqeSize; break;
            case 9: src = &cq->contextInfo.ubJfc.cqeSize; break;
            case 10: src = &sq->contextInfo.ubJfs.sqDepth; break;
            case 11: src = &cq->contextInfo.ubJfc.cqDepth; break;
        }
        reinterpret_cast<__ubuf__ uint32_t*>(info)[16U + lane - 8U] = *src;
    }
    if (lane == 0U) {
        auto* batch = Batch();
        batch->wqebbCount = 0U;
        batch->lastOffset = 0U;
        batch->failed = 0U;
        batch->expectedWqebbs = 0U;
    }
}

""" + marker)
dispatch = inc / "deep_ep/impls/ep/dispatch.hpp"
text = dispatch.read_text()
start = text.index("    if (warp_idx == 1 and lane_idx == 0 and kNumRanks > 1) {")
end = text.index("    asc_syncthreads();", start)
edit(dispatch, text[start:end], """    if (warp_idx == 1 and kNumRanks > 1) {
        AscendC::simt::jetty::HcommJetty jetty(&ub_layout->jetty_info, ub_layout->jetty_stage, uint64_t{0});
        jetty.LoadWarp(const_cast<__gm__ uint64_t*>(jetty_ptrs), vec_core_idx, lane_idx);
        AscendC::simt::HcommCoopWarp{}.sync();
        if (lane_idx == 0) {
            jetty.BeginBatch(ub_layout->lsqe_counter * kNumWQEBBsPerSQESlot);
            if (static_cast<uint64_t>(ub_layout->lsqe_counter) * kNumWQEBBsPerSQESlot + 2 > ub_layout->jetty_info.sqDepth)
                __asc_simt_vf::__trap();
        }
    }
""")
(root / "warp-init.patch").write_text("".join(patches))
print(target)
