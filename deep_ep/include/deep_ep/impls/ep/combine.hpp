#pragma once

#include <deep_ep/comm/barrier.hpp>
#include <deep_ep/common/compiled.hpp>
#include <comm_api/aicore/jetty/hcomm_jetty_simt.h>
#include <deep_ep/impls/ep/combine_utils.hpp>
#include <deep_ep/layout/ep/workspace.hpp>
#include <deep_ep/common/bitset.hpp>
#include <deep_ep/common/exception.hpp>
#include <deep_ep/common/math.hpp>

#include <kernel_operator.h>
#include <c_api/sync/sync.h>
#include <c_api/vector_datamove/vector_datamove.h>
#include <simt_api/device_functions.h>
#include <simt_api/device_warp_functions.h>

namespace deep_ep {

struct CombineTask {
    int16_t rank_idx;
    int16_t num_tokens;
    int32_t rank_slot_idx;
};

template <int kNumRanks, int kNumTopk, int kHidden, int kNumTokensPerTask, int kNumStages, int kNumUbBytes>
struct CombineUBLayout {
    static constexpr int kNumMetadataElems = kNumTopk + 2;
    static constexpr int kNumUbMetadataElems = math::max(warpSize, kNumMetadataElems + 1);
    static constexpr int kNumHiddenBytes = kHidden * sizeof(bfloat16_t);
    static constexpr int kNumTopkWeightBytes = math::align<int>(kNumTopk * sizeof(float), kNumUbAlignmentBytes);
    static constexpr int kNumTopkWeightElems = kNumTopkWeightBytes / sizeof(float);
    static constexpr int kNumTokenBytes = kNumHiddenBytes + kNumTopkWeightBytes;
    static constexpr int kNumWQEBBsPerToken = AscendC::simt::jetty::HcommJetty::kNumWQEBBsPerSQESlot;
    static constexpr int kNumWQEBBsPerTask = kNumTokensPerTask * kNumWQEBBsPerToken;

    AscendC::HcommJettyInfo jetty_info;
    AscendC::HcommJettyInfo publish_info;
    alignas(32) uint8_t jetty_stage[AscendC::simt::jetty::HcommJetty::kNumMaxSQEBytes];
    uint64_t task_heads[kNumStages];
    alignas(kNumUbAlignmentBytes) AscendC::HcommJettyPeerInfo peer_infos[kNumRanks];
    alignas(kNumUbAlignmentBytes) int remaining_tokens[kNumRanks];
    alignas(kNumUbAlignmentBytes) int rank_next_recv_slot_idx[kNumRanks];
    alignas(kNumUbAlignmentBytes) int metadata[kNumStages][kNumTokensPerTask][kNumUbMetadataElems];

    static constexpr int kNumFixedBytes =
        math::align(2 * static_cast<int>(sizeof(AscendC::HcommJettyInfo)), kNumUbAlignmentBytes) +
        AscendC::simt::jetty::HcommJetty::kNumMaxSQEBytes +
        math::align(kNumStages * static_cast<int>(sizeof(uint64_t)), kNumUbAlignmentBytes) +
        math::align(kNumRanks * static_cast<int>(sizeof(AscendC::HcommJettyPeerInfo)), kNumUbAlignmentBytes) +
        2 * math::align(kNumRanks * static_cast<int>(sizeof(int)), kNumUbAlignmentBytes) +
        math::align(kNumStages * kNumTokensPerTask * kNumUbMetadataElems * static_cast<int>(sizeof(int)), kNumUbAlignmentBytes);
    static constexpr int kNumAvailableHiddenBytes = math::max(0, math::align<int, false>(kNumUbBytes, kNumUbAlignmentBytes) - kNumFixedBytes);
    static constexpr int kNumHiddenRingSlots = math::min(32, kNumAvailableHiddenBytes / kNumHiddenBytes);
    using HiddenReducer = RingReducer<kHidden, kNumTopk, kNumHiddenRingSlots>;
    alignas(kNumUbAlignmentBytes) typename HiddenReducer::Buffer hidden;
};

template <int kNumRanks, int kNumTopk, int kNumMaxTokensPerRank, int kNumVecCores,
          int kNumTokensPerTask, typename ub_layout_t, bool kHasTopkWeights>
__simt_vf__ __aicore__ __launch_bounds__(kNumTokensPerTask * warpSize) void load_meta_issue_write(
        __ubuf__ ub_layout_t* ub, __gm__ const int* src_metadata, __gm__ const bfloat16_t* x, __gm__ uint8_t* send_buffer, __gm__ uint8_t* combine_recv_buffer, __gm__ const uint32_t* topk_weights,
        const uint64_t combine_recv_buffer_offset, const CombineTask task,
        const int stage_idx, const int rank_idx, const bool issue_write) {
    EP_STATIC_ASSERT(kNumTopk > 0 and kNumTopk <= warpSize);

    const auto thread_idx = static_cast<int>(threadIdx.x);
    const auto token_idx = thread_idx / warpSize;
    const auto lane_idx = thread_idx % warpSize;
    const auto valid_token = token_idx < task.num_tokens;
    const auto gm_token_metadata = src_metadata + (task.rank_slot_idx + token_idx * kNumVecCores) * ub_layout_t::kNumMetadataElems;
    int slot_idx = -1, metadata_value = -1;
    // load metadata from GM to register
    #pragma unroll
    for (int metadata_idx = lane_idx; metadata_idx < ub_layout_t::kNumMetadataElems; metadata_idx += warpSize) {
        metadata_value = valid_token ? gm_token_metadata[metadata_idx] : -1;
        if (metadata_idx < kNumTopk)
            slot_idx = metadata_value;
        else
            ub->metadata[stage_idx][token_idx][metadata_idx] = metadata_value;
    }

    // write compacted metadata to ub (remove all -1 value and write only the valid slot_idx)
    const auto valid_slot_mask = asc_ballot(slot_idx >= 0);
    const auto num_slots = __popc(valid_slot_mask);
    if (slot_idx >= 0) {
        const auto packed_idx = __popc(valid_slot_mask & ((1U << lane_idx) - 1));
        ub->metadata[stage_idx][token_idx][packed_idx] = slot_idx;
    }
    if (lane_idx == 0)
        ub->metadata[stage_idx][token_idx][ub_layout_t::kNumUbMetadataElems - 1] = num_slots;

    const auto src_token_global_idx = asc_shfl(metadata_value, kNumTopk % warpSize);
    const auto master_topk_idx = asc_shfl(metadata_value, (kNumTopk + 1) % warpSize);

    // copy topk weights
    if constexpr (kHasTopkWeights) {
        if (valid_token and lane_idx < ub_layout_t::kNumTopkWeightElems) {
            const auto weight = lane_idx < kNumTopk and slot_idx >= 0 ? topk_weights[slot_idx] : 0;
            const auto is_local = task.rank_idx == rank_idx;
            const auto slot_group_idx = kNumRanks < kNumTopk ? rank_idx : master_topk_idx;
            const auto src_token_idx = src_token_global_idx - rank_idx * kNumMaxTokensPerRank;
            const auto local_slot_idx = static_cast<int64_t>(slot_group_idx) * kNumMaxTokensPerRank + src_token_idx;
            const auto remote_slot_idx = static_cast<int64_t>(task.rank_slot_idx) + token_idx * kNumVecCores;
            const auto dst = (is_local ? combine_recv_buffer : send_buffer) +
                (is_local ? local_slot_idx : remote_slot_idx) * ub_layout_t::kNumTokenBytes + ub_layout_t::kNumHiddenBytes;
            asc_stcg(reinterpret_cast<__gm__ uint32_t*>(dst) + lane_idx, weight);
        }
    }

    const auto first_slot_lane_idx = valid_slot_mask == 0 ? 0 : __ffs(static_cast<int>(valid_slot_mask)) - 1;
    const auto first_slot_idx = asc_shfl(slot_idx, first_slot_lane_idx);

    AscendC::simt::jetty::HcommJetty jetty(&ub->jetty_info, ub->jetty_stage, uint64_t{0});
    if (issue_write and valid_token) {
        const auto src_token = src_token_global_idx - task.rank_idx * kNumMaxTokensPerRank;
        const auto group = kNumRanks < kNumTopk ? rank_idx : master_topk_idx;
        const auto remote_offset = combine_recv_buffer_offset +
            (static_cast<uint64_t>(group) * kNumMaxTokensPerRank + src_token) * ub_layout_t::kNumTokenBytes;
        const auto send_offset = (static_cast<uint64_t>(task.rank_slot_idx) + token_idx * kNumVecCores) *
            ub_layout_t::kNumTokenBytes;
        const auto info = ub->peer_infos + task.rank_idx;
        if (remote_offset > info->remoteBufferSize ||
            ub_layout_t::kNumTokenBytes > info->remoteBufferSize - remote_offset)
            __asc_simt_vf::__trap();
        AscendC::simt::jetty::HcommPeer peer(info, uint64_t{0});
        AscendC::simt::jetty::HcommSge sge{};
        if (lane_idx == 0) {
            sge.addr = num_slots == 1 ?
                reinterpret_cast<uint64_t>(x) + static_cast<uint64_t>(first_slot_idx) * ub_layout_t::kNumHiddenBytes :
                reinterpret_cast<uint64_t>(send_buffer) + send_offset;
            sge.len = ub_layout_t::kNumHiddenBytes;
        } else if (lane_idx == 1) {
            sge.addr = reinterpret_cast<uint64_t>(send_buffer) + send_offset + ub_layout_t::kNumHiddenBytes;
            sge.len = ub_layout_t::kNumTopkWeightBytes;
        }
        if (jetty.Write<2, false>(AscendC::simt::HcommCoopWarp{}, peer,
                reinterpret_cast<__gm__ void*>(info->remoteBaseAddr + remote_offset), sge) != 0)
            __asc_simt_vf::__trap();
    }
    asc_syncthreads();
    if (thread_idx == 0 && issue_write) {
        jetty.AdvanceSq();
        ub->task_heads[stage_idx] = ub->jetty_info.packedHead;
    }

}

template <int kNumRanks, int64_t kNumTimeoutCycles>
__simt_vf__ __aicore__ __launch_bounds__(math::align(kNumRanks, warpSize))
void load_combine_peers(__ubuf__ AscendC::HcommJettyPeerInfo* peers,
                        __ubuf__ AscendC::HcommJettyInfo* info, __ubuf__ uint8_t* stage,
                        __gm__ void* jetty_ptrs, const int rank_idx, const int jetty_idx) {
    const auto peer_idx = static_cast<int>(threadIdx.x);
    if (peer_idx == 0) {
        // The SIMT constructor also initializes its request-batch state in stage.
        AscendC::simt::jetty::HcommJetty jetty(info, stage, jetty_ptrs, jetty_idx);
        if (jetty.template Drain<kNumTimeoutCycles>() != 0)
            __asc_simt_vf::__trap();
    }
    if (peer_idx < kNumRanks and peer_idx != rank_idx) {
        AscendC::simt::jetty::HcommPeer peer(peers + peer_idx, jetty_ptrs,
            peer_idx < rank_idx ? peer_idx : peer_idx - 1);
    }
}

template <int kNumRanks, int kNumTopk, int kHidden, int kNumMaxTokensPerRank, int kNumVecCores,
          int64_t kNumTimeoutCycles, int kNumUbBytes,
          bool kDoBarrier = false, bool kHasTopkWeights = true>
__global__ __vector__ void combine_impl(__gm__ bfloat16_t* x, __gm__ float* topk_weights, __gm__ int* src_metadata,
                                        __gm__ int* psum_num_recv_tokens_per_rank, __gm__ uint8_t* combine_recv_buffer,
                                        __gm__ uint8_t* send_buffer, __gm__ void* workspace, __gm__ void* jetty_ptrs,
                                        const int rank_idx) {
    AscendC::InitSocState();

    constexpr auto kNumTokensPerTask = 8;
    constexpr auto kNumStages = 2;
    constexpr auto kNumTasksPerDoorbell = 8;

    EP_STATIC_ASSERT(math::is_power_of_2(kNumTasksPerDoorbell));

    using UBLayout = CombineUBLayout<kNumRanks, kNumTopk, kHidden, kNumTokensPerTask, kNumStages, kNumUbBytes>;
    using HiddenReducer = typename UBLayout::HiddenReducer;
    constexpr auto kNumTokenBytes = UBLayout::kNumTokenBytes;
    constexpr auto kNumWQEBBsPerToken = UBLayout::kNumWQEBBsPerToken;
    constexpr auto kNumWQEBBsPerTask = UBLayout::kNumWQEBBsPerTask;

    EP_STATIC_ASSERT(kNumTokensPerTask > 0);
    EP_STATIC_ASSERT(kNumTokensPerTask <= 8);
    EP_STATIC_ASSERT(kNumTopk <= UBLayout::kNumHiddenRingSlots, "Combine UB cannot hold top-k inputs");
    EP_STATIC_ASSERT(kNumVecCores > 0);
    EP_STATIC_ASSERT(kNumRanks <= kNumMaxRanks);
    EP_STATIC_ASSERT(kNumMaxTokensPerRank > 0);

    const auto topk_weight_u32 = reinterpret_cast<__gm__ uint32_t*>(topk_weights);
    const auto vec_core_idx = static_cast<int>(AscendC::GetBlockIdx());
    const auto combine_recv_buffer_offset = reinterpret_cast<uint64_t>(combine_recv_buffer) - reinterpret_cast<uint64_t>(workspace);
    auto& signals = *static_cast<__gm__ layout::EPSignals*>(workspace);

    EP_STATIC_ASSERT(sizeof(UBLayout) <= kNumUbBytes, "Combine task stages and hidden ring exceed UB capacity");
    __ubuf__ UBLayout* ub = 0;

    comm::scalar::barrier<kNumRanks, kNumTimeoutCycles, false>(signals.common_signals, vec_core_idx);

    // One scalar publication descriptor, separate from the SIMT producer state.
    // It is only constructed on the multi-rank path below.
    if constexpr (kNumRanks > 1) {
        AscendC::Simt::VF_CALL<load_combine_peers<kNumRanks, kNumTimeoutCycles>>(
            AscendC::Simt::Dim3(math::align(kNumRanks, warpSize), 1, 1),
            ub->peer_infos, &ub->jetty_info, ub->jetty_stage, jetty_ptrs, rank_idx, vec_core_idx);
        asc_sync_notify(PIPE_V, PIPE_S, EVENT_ID0);
        asc_sync_wait(PIPE_V, PIPE_S, EVENT_ID0);
    }
    Bitset<kNumRanks> active_ranks = {};

    int num_total_tasks = 0;
    int num_remote_tasks = 0;


    const auto remaining_tokens = ub->remaining_tokens;
    const auto rank_next_recv_slot_idx = ub->rank_next_recv_slot_idx;
    for (int src_rank_idx = 0; src_rank_idx < kNumRanks; ++ src_rank_idx) {
        const auto recv_slot_idx = src_rank_idx == 0 ? 0 : psum_num_recv_tokens_per_rank[src_rank_idx - 1];
        const auto num_tokens = psum_num_recv_tokens_per_rank[src_rank_idx] - recv_slot_idx;
        // apply a swizzle to vec-cores to ensure balanced jetty workload
        const auto start_idx = (vec_core_idx + src_rank_idx * math::ceil_div(kNumVecCores, kNumRanks)) % kNumVecCores;
        const auto num_iters = start_idx < num_tokens ? math::ceil_div(num_tokens - start_idx, kNumVecCores) : 0;
        remaining_tokens[src_rank_idx] = num_iters;
        rank_next_recv_slot_idx[src_rank_idx] = recv_slot_idx + start_idx;
        if (num_iters > 0)
            active_ranks.set(src_rank_idx);
        const auto num_tasks = math::ceil_div(num_iters, kNumTokensPerTask);
        num_total_tasks += num_tasks;
        if (src_rank_idx != rank_idx) {
            num_remote_tasks += num_tasks;
        }
    }

    // Stagger the round-robin peer order across ranks and vector cores.
    int16_t next_rank_idx = (rank_idx + vec_core_idx + 1) % kNumRanks;
    const auto get_task = [&]() __aicore__ {
        auto task_rank_idx = next_rank_idx;
        if (remaining_tokens[task_rank_idx] == 0)
            task_rank_idx = active_ranks.find(next_rank_idx);
        const auto rank_slot_idx = rank_next_recv_slot_idx[task_rank_idx];
        int16_t num_task_tokens = math::min(kNumTokensPerTask, remaining_tokens[task_rank_idx]);
        rank_next_recv_slot_idx[task_rank_idx] += num_task_tokens * kNumVecCores;
        remaining_tokens[task_rank_idx] -= num_task_tokens;
        if (remaining_tokens[task_rank_idx] == 0)
            active_ranks.clear(task_rank_idx);
        next_rank_idx = task_rank_idx + 1 == kNumRanks ? 0 : task_rank_idx + 1;
        return CombineTask{task_rank_idx, num_task_tokens, rank_slot_idx};
    };

    bool reclaim_published_tasks = false;
    if constexpr (kNumRanks > 1) {
        // At most eight tasks awaiting publication plus two prefetched tasks.
        // Four BBs/token and one CQE/task are properties of the public SIMT API.
        // Only require a bounded window, not enough space for the entire call.
        constexpr auto kMaxPendingTasks = kNumTasksPerDoorbell + kNumStages;
        EP_DEVICE_ASSERT(kMaxPendingTasks * kNumWQEBBsPerTask + 2 <= ub->jetty_info.sqDepth);
        EP_DEVICE_ASSERT(kMaxPendingTasks < ub->jetty_info.cqDepth);
        reclaim_published_tasks =
            static_cast<uint64_t>(num_remote_tasks) * kNumWQEBBsPerTask + 2 > ub->jetty_info.sqDepth ||
            static_cast<uint32_t>(num_remote_tasks) >= ub->jetty_info.cqDepth;
    }
    HiddenReducer reducer(&ub->hidden);
    CombineTask fetched_tasks[kNumStages];
    const auto issue_load_meta_write = [&](const uint8_t stage_idx, const int task_idx) __aicore__ {
        if (task_idx >= num_total_tasks)
            return;
        const auto task = get_task();
        const auto issue_write = task.rank_idx != rank_idx;
        fetched_tasks[stage_idx] = task;
        AscendC::Simt::VF_CALL<load_meta_issue_write<kNumRanks, kNumTopk, kNumMaxTokensPerRank,
                                                 kNumVecCores, kNumTokensPerTask, UBLayout, kHasTopkWeights>>(
            AscendC::Simt::Dim3(kNumTokensPerTask * warpSize, 1, 1),
            ub, src_metadata, x, send_buffer, combine_recv_buffer, topk_weight_u32,
            combine_recv_buffer_offset, task, stage_idx, rank_idx, issue_write
        );
        asc_sync_notify(PIPE_V, PIPE_S, static_cast<event_t>(stage_idx));
    };

    #pragma unroll 1
    for (uint8_t stage_idx = 0; stage_idx < kNumStages; ++ stage_idx) {
        issue_load_meta_write(stage_idx, stage_idx);
    }

    int num_issued_remote_tasks = 0;
    uint8_t stage_idx = 0;
    for (int task_iter_idx = 0; task_iter_idx < num_total_tasks; ++ task_iter_idx) {
        const auto current_task = fetched_tasks[stage_idx];
        const auto num_stage_tokens = current_task.num_tokens;
        const auto is_local = current_task.rank_idx == rank_idx;

        asc_sync_wait(PIPE_V, PIPE_S, static_cast<event_t>(stage_idx));
        const auto current_head = is_local ? uint64_t{0} : ub->task_heads[stage_idx];
        for (int token_idx = 0; token_idx < num_stage_tokens; ++ token_idx) {
            const auto meta = ub->metadata[stage_idx][token_idx];
            const auto recv_slot_idx = current_task.rank_slot_idx + token_idx * kNumVecCores;
            const auto src_token_idx = meta[kNumTopk] - rank_idx * kNumMaxTokensPerRank;
            const auto subslot_idx = kNumRanks < kNumTopk ? rank_idx : meta[kNumTopk + 1];
            const auto combine_slot_idx = subslot_idx * kNumMaxTokensPerRank + src_token_idx;
            const auto output_addr = is_local ?
                reinterpret_cast<__gm__ bfloat16_t*>(
                    combine_recv_buffer + static_cast<int64_t>(combine_slot_idx) * kNumTokenBytes) :
                reinterpret_cast<__gm__ bfloat16_t*>(send_buffer + static_cast<int64_t>(recv_slot_idx) * kNumTokenBytes);
            const auto num_slots = meta[UBLayout::kNumUbMetadataElems - 1];
            const auto first_slot_idx = num_slots > 0 ? meta[0] : -1;
            if (num_slots >= 2) {
                reducer.load_first_2_token(x, meta[0], meta[1], asc_load_l2_cache_mode::NOTALLOC_KEEP);
                for (int slot_idx = 2; slot_idx < num_slots; ++ slot_idx)
                    reducer.load_next_token(x + static_cast<int64_t>(meta[slot_idx]) * kHidden,
                                           asc_load_l2_cache_mode::NOTALLOC_KEEP);
            }
            if (is_local and num_slots == 1) {
                reducer.load_next_token(
                    x + static_cast<int64_t>(first_slot_idx) * kHidden, asc_load_l2_cache_mode::NOTALLOC_KEEP);
                reducer.store_last_token(output_addr);
            } else if (num_slots > 1) {
                reducer.reduce(num_slots);
                reducer.store_last_token(output_addr);
            }
        }

        issue_load_meta_write(stage_idx, task_iter_idx + kNumStages);

        if (not is_local and (++ num_issued_remote_tasks % kNumTasksPerDoorbell == 0)) {
            asc_sync_notify(PIPE_MTE3, PIPE_S, static_cast<event_t>(stage_idx));
            asc_sync_wait(PIPE_MTE3, PIPE_S, static_cast<event_t>(stage_idx));
            AscendC::HcommJetty publisher(&ub->publish_info, nullptr,
                reinterpret_cast<GM_ADDR>(jetty_ptrs), vec_core_idx);
            // Publish only the completed prefix, not a prefetched future task.
            ub->publish_info.packedHead = current_head;
            publisher.RingDoorbell();
            if (reclaim_published_tasks) {
                // Drain only the published prefix. The producer's head may
                // already include prefetched tasks whose payload is not ready.
                // VF execution is ordered after the metadata prefetch above;
                // CQ reclamation cannot race another SIMT reservation here.
                AscendC::Simt::VF_CALL<comm::simt::drain_jetty<kNumTimeoutCycles>>(
                    AscendC::Simt::Dim3(32), &ub->publish_info);
                asc_sync_notify(PIPE_V, PIPE_S, EVENT_ID2);
                asc_sync_wait(PIPE_V, PIPE_S, EVENT_ID2);
            }
        }

        stage_idx = stage_idx + 1 == kNumStages ? 0 : stage_idx + 1;
    }

    asc_sync_notify(PIPE_MTE3, PIPE_S, EVENT_ID0);
    asc_sync_wait(PIPE_MTE3, PIPE_S, EVENT_ID0);

    if (num_remote_tasks > 0) {
        AscendC::HcommJetty publisher(&ub->publish_info, nullptr,
            reinterpret_cast<GM_ADDR>(jetty_ptrs), vec_core_idx);
        publisher.RingDoorbell();
    }

    if constexpr (kDoBarrier)
        comm::scalar::barrier<
            kNumRanks, kNumTimeoutCycles, true, false, true, kNumVecCores, kNumVecCores, true>(
                signals.common_signals, vec_core_idx, jetty_ptrs);
}

} // namespace deep_ep
