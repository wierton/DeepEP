#!/bin/bash
# Run original / public API / fixed-size reservation with correctness enabled.
set -eo pipefail
root=/mnt/docker/o00939992
source "$root/Ascend/cann/set_env.sh"
export ASCEND_RT_VISIBLE_DEVICES=${ASCEND_RT_VISIBLE_DEVICES:-2,3} TMPDIR="$root/tmp"
experiment="$root/jetty-init-study"
mkdir -p "$experiment/fixed-batch-logs"
trials=${*:-"original:1 fixed-control:1 unchecked-fixed-batch:1 unchecked-fixed-batch:2 fixed-control:2 original:2"}
for trial in $trials; do
    variant=${trial%:*}
    round=${trial#*:}
    if [ "$variant" = original ]; then
        cd "$root/DeepEP-baseline"
    else
        cd "$experiment/$variant"
    fi
    export PYTHONPATH="$PWD" MASTER_PORT=29775
    export EP_JIT_CACHE_DIR="$experiment/cache-fixed-$variant"
    export ASCEND_PROCESS_LOG_PATH="$experiment/fixed-batch-logs/$variant-$round-ascend"
    echo "START $trial devices=$ASCEND_RT_VISIBLE_DEVICES $(date -Is)"
    status=0
    timeout --kill-after=15s 300s "$root/venv/bin/python" -u tests/ep/test_ep.py \
        --num-processes 2 --num-ai-cores 16 --num-tokens 1024 --hidden 4096 \
        --num-topk 4 --num-experts 8 --dispatch-dtype bf16 --test-first-only \
        > "$experiment/fixed-batch-logs/$variant-$round.log" 2>&1 || status=$?
    echo "END $trial exit=$status $(date -Is)"
    if [ "$status" -ne 0 ]; then
        tail -60 "$experiment/fixed-batch-logs/$variant-$round.log"
        exit "$status"
    fi
done
