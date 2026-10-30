# Installed Jetty migration checks

The migration starts from the official DeepEP-Ascend initial release `3b25377`,
not the yuqitao fork. No private SQ/CQ encoder is retained in DeepEP.

## Latest hardware performance retest (2026-10-30)

[Submitted Jetty API/caller commits and combined-source validation](results/2026-10-30/submitted-source/README.md).
The new callers require the matching asc-comm headers; offline compilation passes,
but the combined-source hardware attempt was blocked before Dispatch execution.

[DeepEP vs optimized Jetty: five performance entries, failures and retests](results/2026-10-30/full-suite/README.md).
EP and Engram complete; PP Drain and Bucket correctness failures reproduce.
Barrier is variable across ranks/runs. The report preserves failed samples and
does not claim full correctness or uniform performance parity.

## Checks performed (2026-10-28)

- Host extension: `python setup.py build_ext --inplace` succeeds with CANN 9.3.0,
  Python 3.13, torch 2.14.0+cpu and torch_npu 2.14.0.
- `compile.asc` instantiates PP send/receive, Bucket AllGather, Dispatch,
  cached Dispatch, Combine with/without weights, SIMD and SIMT barriers,
  Dispatch/Combine epilogues, single-rank Combine and the ranks < top-k path.
- Device compilation on this x86 machine:

  ```sh
  make -C tests/jetty compile CANN_HOME=/home/wierton/Ascend/cann \
    CCE_FLAGS='-U__clang_major__ -Wno-deprecated-declarations'
  python tests/jetty/test_combine_schedule.py
  ```

  The test-only macro override avoids this host glibc's unsupported float128
  declarations in Bisheng device compilation. It is not added to production JIT
  flags. Compilation without it fails on this machine.
- The CPU model checks prefetch/publication/count invariants, not the hardware
  instruction implementation. Compilation and model tests are not NPU tests.

## Semantic audit

| Concern | Migration behavior / remaining limitation |
| --- | --- |
| Remote address | Use peer remote registered base plus the original local-window-relative offset; SIMT validates the span against remoteBufferSize. |
| Permission | Existing Host HcommMemReg and Channel creation remain unchanged. Public Jetty APIs obtain the peer token/EID from that channel. |
| Dispatch layout | Each warp supplies alternating token/metadata SGEs; destination token offsets are unchanged. Allocation uses the public four-BB request slots. |
| Combine layout | Two SGEs retain the original hidden/weight source selection and destination offset. Public SIMT reserves four BBs/token versus the original two. |
| Completion | SIMT AdvanceSq produces a CQE per finalized batch; Combine finalizes each task. Use SIMT Drain for EP, including external barriers and epilogues. SIMD Drain uses different SQ-tail accounting and must not consume EP completions. |
| Publication | Preserve scalar publication after payload MTE3 completion. RingDoorbell uses a saved ready-prefix head in Combine, not the potentially prefetched future head in GM. |
| Capacity | Combine retains its eight-remote-task publication cadence. If the whole call exceeds SQ or CQ capacity, drain the published prefix at that cadence. Reserve space for eight pending plus two prefetched tasks. Small calls retain the no-intermediate-drain path. |
| Synchronization | Additional capacity reclamation waits only for already published requests. It never waits for the prefetched unpublished suffix. It can affect performance under pressure. |

## Unresolved / not validated

1. Installed SIMD HcommPeer does not initialize remoteBaseAddr/remoteBufferSize.
   PP/Bucket use these fields, so they are not ready for correctness claims.
   This migration does not patch the installed library silently.
2. DeepJIT's existing `find_ascend_toolkit()` hardcodes
   `aarch64-linux/asc/include/adv_api`. Preserve the original lazy initialization:
   the extension builds and `import deep_ep` succeeds on this x86 installation,
   but first JIT runtime use still encounters that upstream path limitation.
   Changing third-party discovery is separate from adding include directories.
3. SIMT Drain's timeout template is a polling-attempt bound, not the original
   scalar cycle deadline. The existing timeout value is passed through but is
   not a claim of identical wall-clock timeout behavior.
4. No NPU was available: DMA visibility, CQE behavior, repeated invocation,
   ring wrap, routing, throughput and end-to-end results still need hardware
   validation. The schedule model does not validate these properties.
