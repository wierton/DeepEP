"""Compare same-clock VF intervals; never subtract scalar and SIMT timestamps."""
import gzip
import json
from pathlib import Path
import statistics
import sys

rows = []
names = ("metadata", "request", "sync_advance")

def intervals(record):
    times = [v & ((1 << 64) - 1) for v in record[:4]]
    assert times[0] and times == sorted(times), times
    result = {name: times[i + 1] - times[i] for i, name in enumerate(names)}
    result["whole"] = times[3] - times[0]
    assert sum(result[n] for n in names) == result["whole"]
    return result

def average(values):
    assert values
    return {key: statistics.mean(v[key] for v in values) for key in values[0]}

for path in sorted(Path(sys.argv[1]).glob("combine-vf-*.log*")):
    text = gzip.decompress(path.read_bytes()).decode() if path.suffix == ".gz" else path.read_text()
    variant, trial = path.name.removesuffix(".gz").removesuffix(".log").rsplit("-", 1)
    pos, records = 0, []
    while (pos := text.find("COMBINE_VF ", pos)) >= 0:
        pos += len("COMBINE_VF ")
        record, pos = json.JSONDecoder().raw_decode(text, pos)
        records.append(record)
    assert len(records) == 2 and {r["rank"] for r in records} == {0, 1}, path
    for record in records:
        assert len(record["samples"]) == 20
        groups = {kind: {view: [] for view in ("warp0", "longest_warp", "all_warps")}
                  for kind in ("local", "remote")}
        task_counts = set()
        last_starts = {}
        for sample in record["samples"]:
            count = sample["scalar"][8]
            assert 0 < count <= 64 and len(sample["vf"]) == count
            assert all(sample["scalar"][i*10+8:i*10+10] == [count, 0] for i in range(count))
            for core, tasks in enumerate(sample["vf"]):
                assert 0 < len(tasks) <= 32
                start = tasks[0][0][0] & ((1 << 64) - 1)
                assert start > last_starts.get(core, 0), "stale VF trace across samples"
                last_starts[core] = start
                remote = 0
                for task_idx, task in enumerate(tasks):
                    assert len(task) == 8
                    is_remote, tokens, stage = task[0][4], task[0][6], task[0][7]
                    assert is_remote in (0, 1) and 0 < tokens <= 8 and stage == task_idx % 2
                    remote += is_remote
                    for warp, entry in enumerate(task):
                        assert entry[4:] == [is_remote, int(warp < tokens), tokens, stage]
                    values = [intervals(entry) for entry in task[:tokens]]
                    group = groups["remote" if is_remote else "local"]
                    group["warp0"].append(values[0])
                    group["longest_warp"].append(max(values, key=lambda v: v["whole"]))
                    group["all_warps"].extend(values)
                task_counts.add((len(tasks), remote))
        rows.append(dict(variant=variant, trial=int(trial), rank=record["rank"],
                         task_counts=sorted(task_counts),
                         ticks={kind: {view: average(values) for view, values in group.items()}
                                for kind, group in groups.items()}))
summary = {}
for variant in sorted({r["variant"] for r in rows}):
    selected = [r for r in rows if r["variant"] == variant]
    summary[variant] = {kind: {view: average([r["ticks"][kind][view] for r in selected])
                               for view in ("warp0", "longest_warp", "all_warps")}
                        for kind in ("local", "remote")}
print(json.dumps(dict(rows=rows, summary=summary), indent=2))
