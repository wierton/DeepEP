"""Same scalar clock only; phases from one selected core add to its total."""
import gzip
import json
from pathlib import Path
import statistics
import sys

names = ("entry_barrier", "vf_launch", "metadata", "wait_local_slots",
         "local_payload", "join_simt", "publish")
detail = "--combine-detail" in sys.argv[2:]
combine = "--combine" in sys.argv[2:] or detail
if combine:
    names = ("entry_barrier", "jetty_peer_init", "task_setup", "prefetch",
             "task_loop", "final_publish", "final_barrier")
prefix = "COMBINE_DETAIL " if detail else "COMBINE_TIMING " if combine else "SCALAR_TIMING "
stride = 16 if detail else 10
rows = []
pattern = "combine-detail-*.log*" if detail else "combine-*.log*" if combine else "scalar-*.log*"
for path in sorted(Path(sys.argv[1]).glob(pattern)):
    if combine and not detail and path.name.startswith("combine-detail-"):
        continue
    text = gzip.decompress(path.read_bytes()).decode() if path.suffix == ".gz" else path.read_text()
    variant, trial = path.name.removesuffix(".gz").removesuffix(".log").rsplit("-", 1)
    records, pos = [], 0
    while (pos := text.find(prefix, pos)) >= 0:
        pos += len(prefix)
        record, size = json.JSONDecoder().raw_decode(text, pos)
        records.append(record)
        pos = size
    assert len(records) == 2 and {r["rank"] for r in records} == {0, 1}, path
    for record in records:
        assert len(record["samples"]) == 20
        selected, core_means = [], []
        last_start = {}
        for sample in record["samples"]:
            count = sample[8]
            assert 0 < count <= 64
            cores = [sample[i*stride:(i+1)*stride] for i in range(count)]
            for c in cores:
                c[:8] = [value & ((1 << 64) - 1) for value in c[:8]]
            assert all(c[8] == count and c[9] == 0 for c in cores)
            assert all(c[:8] == sorted(c[:8]) and c[0] > 0 for c in cores)
            for core, c in enumerate(cores):
                assert c[0] > last_start.get(core, 0), "stale scalar trace across samples"
                last_start[core] = c[0]
            # Longest post-entry-barrier scalar span; do not mix per-phase maxima.
            c = max(cores, key=lambda c: c[7] - c[1])
            values = {name: c[i+1] - c[i] for i, name in enumerate(names)}
            values["post_barrier_total"] = c[7] - c[1]
            assert sum(values[n] for n in names[1:]) == values["post_barrier_total"]
            if detail:
                for i, label in enumerate(("loop_wait", "loop_payload", "loop_issue", "loop_publish"), 10):
                    values[label] = c[i]
                values["loop_other"] = values["task_loop"] - sum(c[10:14])
                assert values["loop_other"] >= 0
                values["tasks"] = c[14]
                values["remote_tasks"] = c[15]
            selected.append(values)
            core_means.append({name: statistics.mean(c[i+1] - c[i] for c in cores)
                               for i, name in enumerate(names)})
        rows.append(dict(variant=variant, trial=int(trial), rank=record["rank"],
                         selected_core_ticks={k: statistics.mean(v[k] for v in selected) for k in selected[0]},
                         mean_core_ticks={k: statistics.mean(v[k] for v in core_means) for k in core_means[0]}))
summary = {}
for variant in sorted({r["variant"] for r in rows}):
    values = [r for r in rows if r["variant"] == variant]
    summary[variant] = {kind: {k: statistics.mean(r[kind][k] for r in values) for k in values[0][kind]}
                        for kind in ("selected_core_ticks", "mean_core_ticks")}
print(json.dumps(dict(rows=rows, summary=summary), indent=2))
