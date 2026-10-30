"""Reuse the exact api-reserve probes; only change fixed-batch reservation."""
import difflib
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1]).resolve() / "jetty-init-timed"
source = root / "api-reserve"
target = root / "api-reserve-unchecked"
shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "verified*", ".git"))
# The fixture uses absolute includes to override ASC's preferred toolkit paths.
for path in (target / "deep_ep/include").rglob("*"):
    if path.suffix in (".h", ".hpp"):
        text = path.read_text()
        path.write_text(text.replace(str(source), str(target)))
header = target / "deep_ep/include/impl/comm_api/aicore/jetty/impl/platform_v310/hcomm_simt_urma_jetty.h"
old = header.read_text()
start = old.index("    const uint32_t consumed =", old.index("JettyImpl::ReserveBatchSq("))
end = old.index("\n}", start)
text = old[:start] + "    return base + offset;" + old[end:]
marker = "    const uint32_t last = begin + batch->lastOffset;"
assert text.count(marker) == 1
text = text.replace(marker, "    const uint32_t last = begin + count - HCOMM_SIMT_SLOT_WQEBBS;")
header.write_text(text)
(root / "api-reserve-unchecked.patch").write_text("".join(difflib.unified_diff(
    old.splitlines(True), text.splitlines(True), fromfile="api-reserve/hcomm_simt_urma_jetty.h",
    tofile="api-reserve-unchecked/hcomm_simt_urma_jetty.h")))
print(target)
