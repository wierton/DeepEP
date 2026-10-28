# Installed Jetty migration checks

The migration starts from the official DeepEP-Ascend initial release `3b25377`,
not the yuqitao fork. No private SQ/CQ encoder is retained in DeepEP.

## Checks performed (2026-10-28)

- Host extension: `python setup.py build_ext --inplace` succeeds with CANN 9.3.0,
  Python 3.13, torch 2.14.0+cpu and torch_npu 2.14.0.
- `compile.asc` instantiates PP send/receive, Bucket AllGather, Dispatch,
  cached Dispatch, Combine with/without weights, SIMD and SIMT barriers.
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
   `aarch64-linux/asc/include/adv_api`. On this x86 installation, `import deep_ep`
   fails there even though the extension builds. Changing that third-party
   discovery logic is separate from adding Jetty include directories.
3. SIMT Drain's timeout template is a polling-attempt bound, not the original
   scalar cycle deadline. The existing timeout value is passed through but is
   not a claim of identical wall-clock timeout behavior.
4. No NPU was available: DMA visibility, CQE behavior, repeated invocation,
   ring wrap, routing, throughput and end-to-end results still need hardware
   validation. The schedule model does not validate these properties.
