"""Summarize warp-zero-only VF clocks, separating block join from AdvanceSq."""
import gzip
import json
from pathlib import Path
import statistics
import sys

rows = []
names = ("metadata", "request", "join", "advance")
for path in sorted(Path(sys.argv[1]).glob("combine-join-*.log*")):
    text = gzip.decompress(path.read_bytes()).decode() if path.suffix == ".gz" else path.read_text()
    variant, trial = path.name.removesuffix(".gz").removesuffix(".log").rsplit("-", 1)
    pos, records = 0, []
    while (pos := text.find("COMBINE_JOIN ", pos)) >= 0:
        record, pos = json.JSONDecoder().raw_decode(text, pos + len("COMBINE_JOIN "))
        records.append(record)
    assert len(records) == 2 and {r["rank"] for r in records} == {0, 1}, path
    for record in records:
        assert len(record["samples"]) == 20
        groups = {"local": [], "remote": []}
        starts = {}
        counts = set()
        for sample in record["samples"]:
            count = sample["scalar"][8]
            assert 0 < count <= 64 and len(sample["vf"]) == count
            for core, tasks in enumerate(sample["vf"]):
                remote = 0
                for task_idx, task in enumerate(tasks):
                    assert len(task) == 8 and all(all(v == 0 for v in w) for w in task[1:])
                    r = task[0]
                    assert r[4] in (0, 1) and r[5] == 1 and r[7] == task_idx % 2
                    times = [r[i] & ((1 << 64) - 1) for i in (0, 1, 2, 6, 3)]
                    assert times[0] > starts.get(core, 0) and times == sorted(times)
                    starts[core] = times[0]
                    ticks = {name: times[i+1] - times[i] for i, name in enumerate(names)}
                    ticks["whole"] = times[-1] - times[0]
                    groups["remote" if r[4] else "local"].append(ticks)
                    remote += r[4]
                counts.add((len(tasks), remote))
        rows.append(dict(variant=variant, trial=int(trial), rank=record["rank"], task_counts=sorted(counts),
                         ticks={kind: {key: statistics.mean(v[key] for v in values) for key in (*names, "whole")}
                                for kind, values in groups.items()}))
summary = {}
for variant in sorted({r["variant"] for r in rows}):
    selected = [r for r in rows if r["variant"] == variant]
    summary[variant] = {kind: {key: statistics.mean(r["ticks"][kind][key] for r in selected)
                              for key in (*names, "whole")} for kind in ("local", "remote")}
print(json.dumps(dict(rows=rows, summary=summary), indent=2))
