#!/bin/bash
# Independent JIT cache per variant; stock correctness checks remain enabled.
set -eo pipefail
root=/mnt/docker/o00939992
source "$root/Ascend/cann/set_env.sh"
export ASCEND_RT_VISIBLE_DEVICES=2,3 TMPDIR="$root/tmp"
experiment="$root/jetty-ablation-v2"
mkdir -p "$experiment/logs"
trials=${*:-"control:1 no-start-drain:1 no-entry-sync:1 no-last-atomic:1 cached-tail-local:1 cached-tail-local:2 no-last-atomic:2 no-entry-sync:2 no-start-drain:2 control:2"}
for item in $trials; do
    variant=${item%:*}
    round=${item#*:}
    cd "$experiment/$variant"
    export PYTHONPATH="$PWD" MASTER_PORT=29775
    export EP_JIT_CACHE_DIR="$experiment/cache-$variant"
    export ASCEND_PROCESS_LOG_PATH="$experiment/logs/$variant-$round-ascend"
    echo "START $variant $round $(date -Is)"
    status=0
    timeout --kill-after=15s 300s "$root/venv/bin/python" -u tests/ep/test_ep.py \
        --num-processes 2 --num-ai-cores 16 --num-tokens 1024 --hidden 4096 \
        --num-topk 4 --num-experts 8 --dispatch-dtype bf16 --test-first-only \
        > "$experiment/logs/$variant-$round.log" 2>&1 || status=$?
    echo "END $variant $round exit=$status $(date -Is)"
    if [ "$status" -ne 0 ]; then
        tail -60 "$experiment/logs/$variant-$round.log"
        exit "$status"
    fi
done
