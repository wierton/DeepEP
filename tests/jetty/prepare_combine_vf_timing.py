"""Paired per-task/per-warp VF clocks; preserve all original synchronization."""
import difflib
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]).resolve() / "jetty-init-study"
for variant in ("original", "control"):
    source, target = root / f"combine-{variant}", root / f"combine-vf-{variant}"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git", "__pycache__", "verified*"))
    for path in (target / "deep_ep/include").rglob("*"):
        if path.suffix in (".h", ".hpp"):
            path.write_text(path.read_text().replace(str(source), str(target)))
    path = target / "deep_ep/include/deep_ep/impls/ep/combine.hpp"
    original = text = path.read_text()

    def replace(old, new):
        global text
        assert text.count(old) == 1, (old, text.count(old))
        text = text.replace(old, new)

    op = "issue_wqe" if variant == "original" else "issue_write"
    replace(f"const int stage_idx, const int rank_idx, const bool {op}) {{",
            f"const int stage_idx, const int rank_idx, const bool {op}, __gm__ uint64_t* vf_trace) {{")
    marker = "    const auto thread_idx = static_cast<int>(threadIdx.x);"
    replace(marker, "    const uint64_t vf0 = clock64();\n" + marker)
    marker = "    const auto first_slot_idx = asc_shfl(slot_idx, first_slot_lane_idx);"
    replace(marker, marker + "\n    const uint64_t vf1 = clock64();")
    if variant == "control":
        replace("    asc_syncthreads();", "    const uint64_t vf2 = clock64();\n    asc_syncthreads();")
    marker = "\n}\n\ntemplate <int kNumRanks" + (">" if variant == "original" else ", int64_t kNumTimeoutCycles>")
    record = ("\n    const uint64_t vf2 = clock64();" if variant == "original" else "")
    record += """
    const uint64_t vf3 = clock64();
    if (lane_idx == 0) {
        auto* record = vf_trace + token_idx * 8;
        record[0] = vf0;
        record[1] = vf1;
        record[2] = vf2;
        record[3] = vf3;
"""
    record += f"        record[4] = {op};\n"
    record += """        record[5] = valid_token;
        record[6] = task.num_tokens;
        record[7] = stage_idx;
    }
"""
    replace(marker, record + marker)
    # Each core has at most 32 tasks, each task has 8 warps, each warp 8 words.
    replace(f"stage_idx, rank_idx, {op}\n", f"""stage_idx, rank_idx, {op},
            reinterpret_cast<__gm__ uint64_t*>(x) - 64 * 32 * 8 * 8 +
                (vec_core_idx * 32 + task_idx) * 8 * 8
""")
    replace("    constexpr auto kNumTasksPerDoorbell = 8;", """    constexpr auto kNumTasksPerDoorbell = 8;
    EP_STATIC_ASSERT(kNumVecCores <= 64);
    EP_STATIC_ASSERT(kNumRanks * math::ceil_div(math::ceil_div(kNumMaxTokensPerRank, kNumVecCores), kNumTokensPerTask) <= 32);
""")
    replace("reinterpret_cast<__gm__ uint64_t*>(x) - 64 * 10;",
            "reinterpret_cast<__gm__ uint64_t*>(x) - (64 * 10 + 64 * 32 * 8 * 8);")
    path.write_text(text)
    patch = "".join(difflib.unified_diff(original.splitlines(True), text.splitlines(True), fromfile="combine.hpp", tofile="combine.hpp"))
    path = target / "tests/ep/test_ep.py"
    original = text = path.read_text()
    assert text.count("64 * 10 * 4") == 3
    text = text.replace("64 * 10 * 4", "(64 * 10 + 64 * 32 * 8 * 8) * 4")
    replace("                    samples.append(combine_trace.cpu().tolist())", """                    captured = combine_trace.cpu()
                    scalar = captured[:64 * 10].tolist()
                    count = scalar[8]
                    assert 0 < count <= 64
                    vf = captured[64 * 10:].view(64, 32, 8, 8)
                    tasks = []
                    for core in range(count):
                        rows = vf[core].tolist()
                        active = [row for row in rows if row[0][0] != 0]
                        assert active and rows[:len(active)] == active
                        tasks.append(active)
                    samples.append(dict(scalar=scalar, vf=tasks))""")
    text = text.replace("COMBINE_TIMING ", "COMBINE_VF ")
    path.write_text(text)
    patch += "".join(difflib.unified_diff(original.splitlines(True), text.splitlines(True), fromfile="test_ep.py", tofile="test_ep.py"))
    (root / f"combine-vf-{variant}.patch").write_text(patch)
    print(target, flush=True)
