"""Summarize stock per-rank profiler means; not per-iteration rank maxima."""
import json
import pathlib
import re
import statistics
import sys

pattern = re.compile(r"EP:\s*(\d+)/\s*2\s*\|\s*(dispatch|cached|combine)\s*:\s*([\d.]+) us \| urma:\s*([\d.]+) us")
rows = []
for log in sorted(pathlib.Path(sys.argv[1]).glob("*-*.log")):
    variant, round_id = log.stem.rsplit("-", 1)
    if not round_id.isdigit():
        continue
    matches = pattern.findall(log.read_text())
    assert len(matches) == 6, (log, len(matches))
    for op in ("dispatch", "cached", "combine"):
        values = [m for m in matches if m[1] == op]
        assert {v[0] for v in values} == {"0", "1"}
        rows.append(dict(variant=variant, round=int(round_id), op=op,
                         kernel_us=max(float(v[2]) for v in values),
                         with_wait_us=max(float(v[3]) for v in values)))
summary = []
for variant in sorted({row["variant"] for row in rows}):
    for op in ("dispatch", "cached", "combine"):
        values = [r["kernel_us"] for r in rows if r["variant"] == variant and r["op"] == op]
        summary.append(dict(variant=variant, op=op, mean_us=statistics.mean(values), rounds_us=values))
print(json.dumps(dict(rounds=rows, summary=summary), indent=2))
