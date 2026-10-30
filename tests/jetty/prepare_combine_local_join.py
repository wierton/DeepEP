"""Skip the batch join only when no request was posted; retain the remote join."""
import difflib
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]).resolve() / "jetty-init-study"
source, target = root / "fixed-control", root / "combine-local-join"
shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git", "__pycache__", "verified*"))
for path in (target / "deep_ep/include").rglob("*"):
    if path.suffix in (".h", ".hpp"):
        path.write_text(path.read_text().replace(str(source), str(target)))
path = target / "deep_ep/include/deep_ep/impls/ep/combine.hpp"
old = path.read_text()
before = """    asc_syncthreads();
    if (thread_idx == 0 && issue_write) {
        jetty.AdvanceSq();
        ub->task_heads[stage_idx] = ub->jetty_info.packedHead;
    }"""
after = """    if (issue_write) {
        // Only a remote task publishes a batch: join all WQE writers before
        // thread 0 advances it. For a local task, the caller's VF-completion
        // event already waits for all metadata producers, as in original DeepEP.
        asc_syncthreads();
        if (thread_idx == 0) {
            jetty.AdvanceSq();
            ub->task_heads[stage_idx] = ub->jetty_info.packedHead;
        }
    }"""
assert old.count(before) == 1
changed = old.replace(before, after)
path.write_text(changed)
(root / "combine-local-join.patch").write_text("".join(difflib.unified_diff(
    old.splitlines(True), changed.splitlines(True), fromfile="combine.hpp", tofile="combine.hpp")))
print(target, flush=True)
