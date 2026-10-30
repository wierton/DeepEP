#!/bin/bash
# Run the five upstream performance entry points, with correctness checks intact.
# Reserve an idle, connected device pair before running this script.
set -eo pipefail
root=${LAB_ROOT:-/mnt/docker/o00939992}
source "$root/Ascend/cann/set_env.sh"
export ASCEND_RT_VISIBLE_DEVICES=${ASCEND_RT_VISIBLE_DEVICES:?Set an idle connected device pair}
export TMPDIR="$root/tmp" MASTER_PORT=${MASTER_PORT:-29863}
out=${RESULT_DIR:-"$root/perf-suite-$(date +%Y%m%d-%H%M%S)"}
mkdir -p "$out"
printf 'case\tvariant\tround\texit\n' > "$out/status.tsv"
for case_name in ep pp engram bucket barrier; do
    case "$case_name" in
        ep) args=(tests/ep/test_ep.py --num-processes 2 --num-ai-cores 16 --num-tokens 1024 --hidden 4096 --num-topk 4 --num-experts 8 --dispatch-dtype bf16 --test-first-only) ;;
        pp) args=(tests/pp/test_pp.py --num-processes 2) ;;
        engram) args=(tests/engram/test_engram.py --num-processes 2) ;;
        bucket) args=(tests/bucket/test_all_gather.py --num-processes 2) ;;
        barrier) args=(tests/comm/test_barrier.py --num-processes 2) ;;
    esac
    # Keep independent optimization variants: do not silently compose patches.
    for trial in original:1 fixed-control:1 dynamic-sge:1 combine-known-header:1 combine-known-header:2 dynamic-sge:2 fixed-control:2 original:2; do
        variant=${trial%:*}; round=${trial#*:}
        if [ "$variant" = original ]; then
            cd "$root/DeepEP-baseline"
        else
            cd "$root/jetty-init-study/$variant"
        fi
        tag="$case_name-$variant-$round"
        cmp "$root/DeepEP-baseline/${args[0]}" "${args[0]}"
        export PYTHONPATH="$PWD" EP_JIT_CACHE_DIR="$root/jetty-init-study/cache-suite-$variant"
        export ASCEND_PROCESS_LOG_PATH="$out/$tag-ascend"
        # Record source identity, command, environment, stdout and stderr.
        {
            date -Is
            printf 'cwd=%s devices=%s cache=%s\n' "$PWD" "$ASCEND_RT_VISIBLE_DEVICES" "$EP_JIT_CACHE_DIR"
            sha256sum "${args[0]}" deep_ep/include/deep_ep/impls/ep/{dispatch,combine}.hpp
            printf '%q ' "$root/venv/bin/python" -u "${args[@]}"; printf '\n'
        } > "$out/$tag.log"
        status=0
        timeout --kill-after=15s 900s "$root/venv/bin/python" -u "${args[@]}" >> "$out/$tag.log" 2>&1 || status=$?
        printf '%s\t%s\t%s\t%s\n' "$case_name" "$variant" "$round" "$status" | tee -a "$out/status.tsv"
        # Fail closed: a broken baseline/topology must not produce fake comparisons.
        if [ "$status" -ne 0 ]; then
            tail -60 "$out/$tag.log"
            exit "$status"
        fi
    done
done
