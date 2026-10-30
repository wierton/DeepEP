"""Original DeepEP counterpart to the migrated API-region probes."""
import difflib
import pathlib
import shutil
import sys

root = pathlib.Path(sys.argv[1]).resolve()
assert "updated_storage" in (root / "jetty-timing-detail/original/tests/ep/test_ep.py").read_text(), \
    "Regenerate the timed fixture: cached correctness inputs need trace storage too"


def once(text, old, new):
    assert text.count(old) == 1, (old, text.count(old))
    return text.replace(old, new)


for variant in (sys.argv[2:] or ["native-reserve", "native-fill"]):
    repo = root / "jetty-init-timed" / variant
    shutil.copytree(root / "jetty-timing-detail/original", repo,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "build", "logs", ".jit-cache"))
    path = repo / "deep_ep/include/deep_ep/impls/ep/dispatch.hpp"
    original = text = path.read_text()
    text = once(text, "    uint64_t trace_prep_sum =", "    uint64_t trace_api_sum = 0;\n    uint64_t trace_prep_sum =")
    if variant == "native-reserve":
        start = "        // Allocate physical SQE"
        finish = "        // Now we need to know the SGEs'"
    else:
        # Original word-generation/store interval, corresponding to FillSqWarpLane.
        # Some address/shuffle preparation is outside this interval in the original;
        # the report must compare the whole request too, not equate boundaries blindly.
        start = "        const auto slot_head ="
        finish = "        const uint64_t trace_request2 = clock64();"
    text = once(text, start, "        const uint64_t native_trace_begin = clock64();\n" + start)
    text = once(text, finish, "        trace_api_sum += clock64() - native_trace_begin;\n" + finish)
    text = once(text, "        trace[base + 15] = trace_advance;", "        trace[base + 15] = trace_advance;\n        trace[base + 16] = trace_api_sum;")
    text = once(text, "(vec_core_idx * 16 + warp_idx) * 16", "(vec_core_idx * 16 + warp_idx) * 24")
    text = once(text, "64 * 16 * 16 + vec_core_idx", "64 * 16 * 24 + vec_core_idx")
    path.write_text(text)
    test_path = repo / "tests/ep/test_ep.py"
    old_test = test_path.read_text()
    new_test = once(old_test, "trace_words = 64 * 16 * 16 + 64 * 8", "trace_words = 64 * 16 * 24 + 64 * 8")
    test_path.write_text(new_test)
    patch = "".join(difflib.unified_diff(original.splitlines(True), text.splitlines(True),
                                       fromfile=str(path.relative_to(repo)), tofile=str(path.relative_to(repo))))
    patch += "".join(difflib.unified_diff(old_test.splitlines(True), new_test.splitlines(True),
                                       fromfile="tests/ep/test_ep.py", tofile="tests/ep/test_ep.py"))
    (repo.parent / (variant + ".patch")).write_text(patch)
    print(repo, flush=True)
