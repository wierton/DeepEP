# Remote validation, 2026-10-29

## Environment and source

- Host: `root@179.59.1.1`, x86_64, Ascend950PR (8 devices).
- Work directory: `/mnt/docker/o00939992`.
- Toolkit and 950 ops: `9.3.0~weekly.20261021.01`, installed under `Ascend/`.
- Existing driver retained: `25.7.rc1.6`.
- Python: independent `venv` with system site packages, Python 3.12.3,
  torch 2.7.1+cpu, torch_npu 2.7.1.post4.
- Migration source: DeepEP `949fda2`, DeepJIT `c966a2b`.
- Baseline: DeepEP `3b25377`; original kernels and communication code.
  Only setup.py / csrc/runtime/jit.hpp build include configuration and the
  DeepJIT path fix were supplied to build the baseline on this x86 host.
- Devices 0 and 1 were selected while idle. Other users' processes were not stopped.

## Fixed: architecture-specific DeepJIT header lookup

Host extension compilation initially required missing `elfutils/libdwfl.h`.
The user installed the system development dependencies; compilation then passed.
Import subsequently failed before launching a kernel:

```text
Ascend adv_api include directory was not found:
/mnt/docker/o00939992/Ascend/cann-9.3.0/aarch64-linux/asc/include/adv_api
```

DeepJIT pinned revision `65f5139` hardcoded this AArch64 directory. Commit
`c966a2b` uses `asc/include/adv_api`, the toolkit's architecture-neutral entry
point. DeepEP `949fda2` pins this commit in the wierton/DeepJIT fork.
Host rebuild and `import deep_ep` pass afterward. No fake directory or
machine-specific path was added to production code.

## Checks completed

```bash
source /mnt/docker/o00939992/Ascend/cann/set_env.sh
cd /mnt/docker/o00939992/DeepEP-Ascend
/mnt/docker/o00939992/venv/bin/python setup.py build_ext --inplace
/mnt/docker/o00939992/venv/bin/python -c 'import deep_ep'
make -C tests/jetty compile CANN_HOME=/mnt/docker/o00939992/Ascend/cann
/mnt/docker/o00939992/venv/bin/python tests/jetty/test_combine_schedule.py
```

Host build, import, device template compilation (no CCE_FLAGS workaround), and
four CPU scheduling model tests pass. A separate torch NPU tensor-add smoke
test on device 0 returns eight values of 2. These do not establish Jetty
communication correctness.

## Two-rank test: blocked before Jetty execution

```bash
PYTHONPATH=. ASCEND_RT_VISIBLE_DEVICES=0,1 MASTER_PORT=29671 \
EP_JIT_CACHE_DIR=/mnt/docker/o00939992/jit-cache \
ASCEND_PROCESS_LOG_PATH=/mnt/docker/o00939992/logs/ascend \
timeout --kill-after=15s 180s /mnt/docker/o00939992/venv/bin/python -u \
  tests/ep/test_ep.py --num-processes 2 --num-ai-cores 2 \
  --num-tokens 128 --hidden 512 --num-topk 2 --num-experts 4 \
  --dispatch-dtype bf16 --skip-perf-test --test-first-only
```

Fails in the original HCCLContext constructor at `num_layers > 1`, before
Dispatch/Combine launch. The original code selects `layers[1]`; this group
does not provide a second layer. This was the initial run, before the explicit
layer-zero change described below.
Future Dispatch runs must also use at least 16 AI cores as required by the
original host dispatch wrapper; the first small test selected only 2.

## Single-rank differential test: not passing

Using `--num-processes 1 --num-ai-cores 16` with the same token/hidden/expert
arguments reaches Dispatch. Both migrated and original kernels report error
334: `SIMT accesses an invalid GM address or the cross-device memory access
times out`. Both show the same invalid-address register pattern; this is
evidence of a shared failure, not proof of its root cause. Do not label it a
Jetty regression or claim it is fixed. Neither run completed correctness
validation. External timeouts bound the test processes.

## Logs retained on the server

All paths are under `/mnt/docker/o00939992/logs/`:

- `toolkit-install.log`, `950-ops-install.log`: installation.
- `device-compile.log`, `host-build.log`, `import.log`: build/import checks.
- `npu-smoke.log`, `combine-schedule.log`: smoke and CPU model checks.
- `ep-2rank-bf16.log`: topology assertion.
- `ep-1rank-bf16.log`: first test's minimum-core assertion.
- `ep-1rank-bf16-16cores.log`, `ascend/debug/plog/`: migrated runtime failure.
- `baseline-host-build.log`, `baseline-ep-1rank-bf16.log`,
  `baseline-ascend/debug/plog/`: baseline comparison.

These initial runs did not validate multi-rank Jetty correctness. See the
layer-zero follow-up below for the subsequently passing restricted case.

## Explicit layer-zero test

Commit `ba68aad` selects network layer **ID 0**, checks that HCCL advertises it,
and retains the UB_MEM / UBC_CTP link checks. This is the user's requested
environment adaptation, not an automatic fallback or a new environment variable.
The host extension rebuild passes (`layer0-build.log`).

The two-rank command above was rerun with `--num-ai-cores 16` and logs under
`layer0-ascend/`, stdout in `ep-layer0-2rank.log`. Buffer construction now
completes and the test reaches Dispatch. Both ranks report error 334 in
`simt_persistent_worker`, with the same invalid-address register patterns
`0xedfcfdfe18001200` and `0xdf79ddf318001200` seen in the single-rank test.
The external 180-second timeout terminates the hung test (exit 124).

For comparison, the original baseline was rebuilt with only the same layer-ID
selection added to its communication code (in addition to the build adaptations
already listed above). Its two-rank test also reports error 334 with the same
register patterns. Logs: `baseline-layer0-build.log`, `baseline-layer0-2rank.log`,
`baseline-layer0-ascend/`. This does not prove the migrated remote data path is
correct, but the observed failure is reproducible without the Jetty migration.

In the migrated two-rank binary, fault offsets `0x5fd8` and `0x6628` are
`STG.NC.NMFV.b32`. Their surrounding expert/rank count stores correspond to
the `kDoCPUSync` host-workspace writeback paths.

### Passing isolation test: CPU count writeback disabled

The migrated two-rank test **exits 0** with the existing `--do-cpu-sync 0`
option. Correctness checks remain enabled (no `--skip-check`). This runs the
first BF16 mode, including the test's Dispatch/Combine and cached-handle checks;
it is not coverage of every dtype, mode, topology, PP or Bucket path.

```bash
source /mnt/docker/o00939992/Ascend/cann/set_env.sh
cd /mnt/docker/o00939992/DeepEP-Ascend
PYTHONPATH=. ASCEND_RT_VISIBLE_DEVICES=0,1 MASTER_PORT=29671 \
EP_JIT_CACHE_DIR=/mnt/docker/o00939992/jit-cache \
ASCEND_PROCESS_LOG_PATH=/mnt/docker/o00939992/logs/layer0-no-cpusync-ascend \
timeout --kill-after=15s 120s /mnt/docker/o00939992/venv/bin/python -u \
  tests/ep/test_ep.py --num-processes 2 --num-ai-cores 16 \
  --num-tokens 128 --hidden 512 --num-topk 2 --num-experts 4 \
  --dispatch-dtype bf16 --skip-perf-test --test-first-only --do-cpu-sync 0
```

Output: `logs/ep-layer0-no-cpusync-2rank.log`. Disabling CPU count writeback
is a diagnostic isolation, **not a fix for the default CPU synchronization
path**. Its underlying host-mapping/compiler/runtime cause is still unproven.
