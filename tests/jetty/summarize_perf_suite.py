"""Parse upstream performance logs without mixing kernel and complete-op timings.

Usage: python summarize_perf_suite.py RESULT_DIR [RESULT_DIR ...]
Print JSON; fail/partial logs never enter numeric summaries. A >5% round spread
or >10% rank skew flags a case for review/retest, not automatic sample deletion.
"""
import csv
import json
from pathlib import Path
import re
import statistics
import sys

NUM = r"([\d.]+)"
PATTERNS = {
    "ep": re.compile(r"EP:\s*(\d+)/\s*2\s*\|\s*(dispatch|cached|combine)\s*:\s*" + NUM + r" us \| urma:\s*" + NUM + r" us, [^\n]*?epilogue:\s*" + NUM + r" us"),
    "pp": re.compile(r"PP:\s*(\d+)/\s*2\s*\|\s*send:\s*" + NUM + r" us, [^\n]*?recv:\s*" + NUM + r" us"),
    "engram": re.compile(r"Rank (\d+): " + NUM + r" us,"),
    "barrier": re.compile(r"EP:\s*(\d+)/\s*2, barrier time:\s*" + NUM + r" us"),
    "bucket": re.compile(r"> (DeepEP|HCCL):\s*" + NUM + r" us"),
}


def parse(case, text):
    matches = PATTERNS[case].findall(text)
    metrics = {}
    for match in matches:
        rank, *values = match
        if case == "ep":
            op, *values = values
            names = [op + "_issue", op + "_urma", op + "_epilogue"]
        elif case == "pp":
            names = ["send", "recv"]
        elif case == "bucket":
            names, rank = [rank + "_complete"], "rank_max"
        else:
            names = [case]
        for name, value in zip(names, values):
            ranks = metrics.setdefault(name, {})
            if rank in ranks:
                raise ValueError(f"duplicate {name} rank {rank}")
            ranks[rank] = float(value)
    expected = {"ep": 9, "pp": 2, "engram": 1, "barrier": 1, "bucket": 2}[case]
    assert len(metrics) == expected, (case, "missing metrics", metrics)
    for ranks in metrics.values():
        assert set(ranks) == ({"rank_max"} if case == "bucket" else {"0", "1"}), ranks
    return metrics


if __name__ == "__main__":
    rows, failures, flags = [], [], []
    for directory in map(Path, sys.argv[1:]):
        with (directory / "status.tsv").open() as stream:
            for status in csv.DictReader(stream, delimiter="\t"):
                case, variant, round_id = (status[x] for x in ("case", "variant", "round"))
                log = directory / f"{case}-{variant}-{round_id}.log"
                identity = dict(directory=str(directory), case=case, variant=variant, round=round_id)
                if status["exit"] != "0":
                    failures.append(dict(**identity, exit=status["exit"]))
                    continue
                try:
                    metrics = parse(case, log.read_text(errors="replace"))
                except (AssertionError, ValueError) as exc:
                    failures.append(dict(**identity, parse_error=str(exc)))
                    continue
                for metric, ranks in metrics.items():
                    row = dict(**identity, metric=metric, ranks_us=ranks, us=max(ranks.values()))
                    rows.append(row)
                    if min(ranks.values()) > 0 and max(ranks.values()) / min(ranks.values()) > 1.1:
                        flags.append(dict(**identity, metric=metric, reason="rank_skew_over_10_percent"))
    summary = []
    for key in sorted({(r["case"], r["variant"], r["metric"]) for r in rows}):
        values = [r["us"] for r in rows if (r["case"], r["variant"], r["metric"]) == key]
        item = dict(zip(("case", "variant", "metric"), key), rounds_us=values,
                    mean_us=statistics.mean(values))
        summary.append(item)
        if len(values) > 1 and min(values) > 0 and max(values) / min(values) > 1.05:
            flags.append(dict(**item, reason="round_spread_over_5_percent"))
    print(json.dumps(dict(rows=rows, summary=summary, failures=failures, flags=flags), indent=2))
