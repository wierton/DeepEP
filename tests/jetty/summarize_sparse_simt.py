"""Summarize outer SIMT intervals without mixing scalar clocks or phase maxima."""
import gzip
import json
from pathlib import Path
import statistics
import sys

rows = []
for path in sorted(Path(sys.argv[1]).glob("sparse-*.log*")):
    text = gzip.decompress(path.read_bytes()).decode() if path.suffix == ".gz" else path.read_text()
    variant, trial = path.name.removesuffix(".gz").removesuffix(".log").rsplit("-", 1)
    records, pos = [], 0
    while (pos := text.find("SPARSE_TIMING ", pos)) >= 0:
        pos += len("SPARSE_TIMING ")
        record, pos = json.JSONDecoder().raw_decode(text, pos)
        records.append(record)
    assert len(records) == 2 and {r["rank"] for r in records} == {0, 1}, path
    for record in records:
        assert len(record["samples"]) == 20
        samples = []
        for sample in record["samples"]:
            sample = [v & ((1 << 64) - 1) for v in sample]
            count = sample[4]
            assert 0 < count <= 64
            cores = []
            for core in range(count):
                warps = [sample[(core * 16 + w) * 6:(core * 16 + w + 1) * 6] for w in range(16)]
                assert all(w[4] == count and w[:4] == sorted(w[:4]) and w[0] > 0 for w in warps)
                # Select one warp for additive phase accounting within this core.
                w = max(warps, key=lambda w: w[3] - w[0])
                cores.append(dict(init=w[1]-w[0], intermediate=w[2]-w[1],
                                  requests_finalize=w[3]-w[2], total=w[3]-w[0]))
            samples.append({k: statistics.mean(c[k] for c in cores) for k in cores[0]})
        rows.append(dict(variant=variant, trial=int(trial), rank=record["rank"],
                         ticks={k: statistics.mean(s[k] for s in samples) for k in samples[0]}))
summary = {v: {k: statistics.mean(r["ticks"][k] for r in rows if r["variant"] == v)
               for k in rows[0]["ticks"]} for v in sorted({r["variant"] for r in rows})}
print(json.dumps(dict(rows=rows, summary=summary), indent=2))
