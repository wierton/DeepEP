"""Compare same-clock-domain deltas; never subtract SIMT and scalar timestamps."""
import json
import gzip
import pathlib
import statistics
import sys

rows = []
directory = pathlib.Path(sys.argv[1])
api = "--api" in sys.argv[2:]
detail = "--detail" in sys.argv[2:] or api
patterns = [v for v in sys.argv[2:] if v not in ("--detail", "--api")]
width = 24 if api else 16 if detail else 8
scalar_base = 64 * 16 * width
paths = sorted(directory.glob(patterns[0] if patterns else "*.log"))
if not paths and not patterns:
    paths = sorted(directory.glob("*.log.gz"))
assert paths, "No timing logs found"
for path in paths:
    variant, round_id = path.name.removesuffix(".gz").removesuffix(".log").rsplit("-", 1)
    text = gzip.decompress(path.read_bytes()).decode() if path.suffix == ".gz" else path.read_text()
    records = []
    position = 0
    while (position := text.find("JETTY_TIMING ", position)) >= 0:
        position += len("JETTY_TIMING ")
        record, end = json.JSONDecoder().raw_decode(text, position)
        records.append(record)
        position = end
    assert len(records) == 2, (path, len(records))
    assert {r["rank"] for r in records} == {0, 1}
    for record in records:
        assert len(record["samples"]) == 20
        measurements = {}
        for sample in record["samples"]:
            # Torch transports uint64 bit patterns through an int64 tensor.
            sample = [v & ((1 << 64) - 1) for v in sample]
            ncores = sample[scalar_base + 6]
            assert 0 < ncores <= 64, ncores
            per_core = {name: [] for name in ("init_simt", "build_simt", "finalize_simt",
                        "clock_pair_simt", "join_scalar", "publish_scalar", "barrier_scalar", "sqes")}
            if detail:
                per_core.update({k: [] for k in ("peer_load_simt", "jetty_load_simt", "init_wait_simt",
                    "prep_per_request_simt", "emit_per_request_simt", "batch_wait_simt", "advance_simt")})
            if api:
                per_core["api_region_per_request_simt"] = []
                if variant.endswith("-joint"):
                    per_core.update(reserve_a_simt=[], reserve_b_simt=[])
            for core in range(ncores):
                scalar = sample[scalar_base + core * 8:scalar_base + core * 8 + 8]
                assert scalar[4] == 1, "final communication barrier must remain enabled"
                assert scalar[0] <= scalar[1] <= scalar[2] <= scalar[3]
                per_core["join_scalar"].append(scalar[1] - scalar[0])
                per_core["publish_scalar"].append(scalar[2] - scalar[1])
                per_core["barrier_scalar"].append(scalar[3] - scalar[2])
                warps = [sample[(core * 16 + w) * width:(core * 16 + w) * width + width] for w in range(16)]
                sqes = warps[0][5]
                assert sqes == scalar[5]
                per_core["sqes"].append(sqes)
                if not sqes:
                    continue
                if detail:
                    assert all(w[8] < 2**32 and w[9] < 2**32 for w in warps), "probe overflow"
                    assert sum(w[13] for w in warps) == sqes
                    per_core["peer_load_simt"].append(max(w[8] for w in warps))
                    per_core["jetty_load_simt"].append(max(w[9] for w in warps))
                    per_core["init_wait_simt"].append(max(w[10] for w in warps))
                    per_core["prep_per_request_simt"].append(sum(w[11] for w in warps) / sqes)
                    per_core["emit_per_request_simt"].append(sum(w[12] for w in warps) / sqes)
                    per_core["batch_wait_simt"].append(warps[0][14])
                    per_core["advance_simt"].append(warps[0][15])
                if api:
                    per_core["api_region_per_request_simt"].append(sum(w[16] for w in warps) / sqes)
                    if variant.endswith("-joint"):
                        assert all(w[16] == w[17] + w[18] for w in warps), "non-additive reservation split"
                        assert all(w[16] < 2**32 for w in warps), "invalid reservation timestamp order"
                        per_core["reserve_a_simt"].append(sum(w[17] for w in warps) / sqes)
                        per_core["reserve_b_simt"].append(sum(w[18] for w in warps) / sqes)
                active = min(sqes, 16)
                if variant == "original" or variant.startswith("native-"):
                    per_warp = (sqes + 15) // 16
                    active = (sqes + per_warp - 1) // per_warp
                for warp in warps:
                    assert 0 < warp[0] <= warp[1] <= warp[2] <= warp[3] <= warp[4]
                    assert warp[6] <= warp[7]
                per_core["init_simt"].append(max(w[1] - w[0] for w in warps))
                per_core["build_simt"].append(max(w[3] - w[2] for w in warps[:active]))
                per_core["finalize_simt"].append(warps[0][4] - warps[0][3])
                per_core["clock_pair_simt"].append(statistics.mean(w[7] - w[6] for w in warps))
            for name, values in per_core.items():
                measurements.setdefault(name, []).append(statistics.mean(values))
        rows.append(dict(variant=variant, round=int(round_id), rank=record["rank"], samples=20,
                         mean_ticks={k: statistics.mean(v) for k, v in measurements.items()},
                         median_ticks={k: statistics.median(v) for k, v in measurements.items()}))
summary = {}
for variant in sorted({r["variant"] for r in rows}):
    selected = [r for r in rows if r["variant"] == variant]
    if selected:
        summary[variant] = {k: statistics.mean(r["mean_ticks"][k] for r in selected)
                            for k in selected[0]["mean_ticks"]}
print(json.dumps(dict(rows=rows, summary=summary), indent=2))
