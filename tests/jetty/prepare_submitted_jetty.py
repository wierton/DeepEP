"""Stage the actual submitted sources against the existing remote test fixture.

Usage: python prepare_submitted_jetty.py ROOT UPLOAD_DIR
UPLOAD_DIR contains dispatch.hpp, combine.hpp and the four modified asc-comm
headers. This changes an isolated copy, never the installed toolkit.
"""
from pathlib import Path
import re
import shutil
import sys

root, upload = map(Path, sys.argv[1:])
study = root / "jetty-init-study"
source, target = study / "fixed-control", study / "submitted-jetty"
shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git", "__pycache__", "build", "logs"))
inc = target / "deep_ep/include"
files = {
    "dispatch.hpp": "deep_ep/impls/ep/dispatch.hpp",
    "combine.hpp": "deep_ep/impls/ep/combine.hpp",
    "hcomm_jetty_simt.h": "comm_api/aicore/jetty/hcomm_jetty_simt.h",
    "hcomm_jetty_simt_impl.h": "impl/comm_api/aicore/jetty/impl/hcomm_jetty_simt_impl.h",
    "hcomm_simt_urma_jetty.h": "impl/comm_api/aicore/jetty/impl/platform_v310/hcomm_simt_urma_jetty.h",
    "hcomm_simt_urma_jetty_def.h": "impl/comm_api/aicore/jetty/impl/platform_v310/hcomm_simt_urma_jetty_def.h",
}
for name, relative in files.items():
    shutil.copyfile(upload / name, inc / relative)
for path in inc.rglob("*"):
    if path.suffix not in (".h", ".hpp"):
        continue
    text = path.read_text().replace(str(source), str(target))
    for name in ("hcomm_jetty.h", "hcomm_jetty_simt.h"):
        text = text.replace(f"<comm_api/aicore/jetty/{name}>",
                            f'<{inc / "comm_api/aicore/jetty" / name}>')
    text = re.sub(r'"(?:\.\./)+impl/comm_api/([^"\n]+)"',
                  lambda m: '"' + str(inc / "impl/comm_api" / m[1]) + '"', text)
    path.write_text(text)
print(target)
