"""Split the paired Combine task loop without removing or changing its work."""
import difflib
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]).resolve() / "jetty-init-study"
for variant in ("original", "control"):
    source, target = root / f"combine-{variant}", root / f"combine-detail-{variant}"
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

    replace("    int num_issued_remote_tasks = 0;", """    uint64_t loop_wait_ticks = 0, loop_payload_ticks = 0;
    uint64_t loop_issue_ticks = 0, loop_publish_ticks = 0;
    int num_issued_remote_tasks = 0;""")
    marker = "        asc_sync_wait(PIPE_V, PIPE_S, static_cast<event_t>(stage_idx));"
    replace(marker, "        const uint64_t loop0 = asc_get_system_cycle();\n" + marker)
    marker = "        for (int token_idx = 0; token_idx < num_stage_tokens; ++ token_idx) {"
    replace(marker, "        const uint64_t loop1 = asc_get_system_cycle();\n" + marker)
    name = "issue_load_meta_wqe" if variant == "original" else "issue_load_meta_write"
    marker = f"        {name}(stage_idx, task_iter_idx + kNumStages);"
    replace(marker, "        const uint64_t loop2 = asc_get_system_cycle();\n" + marker +
            "\n        const uint64_t loop3 = asc_get_system_cycle();")
    marker = "        stage_idx = stage_idx + 1 == kNumStages ? 0 : stage_idx + 1;"
    replace(marker, """        const uint64_t loop4 = asc_get_system_cycle();
        loop_wait_ticks += loop1 - loop0;
        loop_payload_ticks += loop2 - loop1;
        loop_issue_ticks += loop3 - loop2;
        loop_publish_ticks += loop4 - loop3;
""" + marker)
    text = text.replace("64 * 10", "64 * 16").replace("vec_core_idx * 10", "vec_core_idx * 16")
    stores = ""
    for i, value in enumerate(("loop_wait_ticks", "loop_payload_ticks", "loop_issue_ticks",
                               "loop_publish_ticks", "num_total_tasks", "num_remote_tasks"), 10):
        stores += f"    AscendC::WriteGmByPassDCache(trace + vec_core_idx * 16 + {i}, static_cast<uint64_t>({value}));\n"
    replace("\n}\n\n} // namespace deep_ep", "\n" + stores + "}\n\n} // namespace deep_ep")
    path.write_text(text)
    patch = "".join(difflib.unified_diff(original.splitlines(True), text.splitlines(True), fromfile="combine.hpp", tofile="combine.hpp"))
    path = target / "tests/ep/test_ep.py"
    original = text = path.read_text()
    assert text.count("64 * 10 * 4") == 3
    text = text.replace("64 * 10 * 4", "64 * 16 * 4").replace("COMBINE_TIMING ", "COMBINE_DETAIL ")
    path.write_text(text)
    patch += "".join(difflib.unified_diff(original.splitlines(True), text.splitlines(True), fromfile="test_ep.py", tofile="test_ep.py"))
    (root / f"combine-detail-{variant}.patch").write_text(patch)
    print(target, flush=True)
