#!/bin/bash
set -eo pipefail
root=/mnt/docker/o00939992
source "$root/Ascend/cann/set_env.sh"
export ASCEND_RT_VISIBLE_DEVICES=2,3 TMPDIR="$root/tmp"
experiment="$root/jetty-timing-v3"
mkdir -p "$experiment/logs"
for trial in original:1 migrated:1 migrated:2 original:2; do
    variant=${trial%:*}
    round=${trial#*:}
    cd "$experiment/$variant"
    export PYTHONPATH="$PWD" MASTER_PORT=29785
    export EP_JIT_CACHE_DIR="$experiment/cache-$variant"
    export ASCEND_PROCESS_LOG_PATH="$experiment/logs/$variant-$round-ascend"
    echo "START $trial $(date -Is)"
    status=0
    timeout --kill-after=15s 300s "$root/venv/bin/python" -u tests/ep/test_ep.py \
        --num-processes 2 --num-ai-cores 16 --num-tokens 1024 --hidden 4096 \
        --num-topk 4 --num-experts 8 --dispatch-dtype bf16 --test-first-only --skip-perf-test \
        > "$experiment/logs/$variant-$round.log" 2>&1 || status=$?
    echo "END $trial exit=$status $(date -Is)"
    if [ "$status" -ne 0 ]; then
        tail -40 "$experiment/logs/$variant-$round.log"
        exit "$status"
    fi
done
