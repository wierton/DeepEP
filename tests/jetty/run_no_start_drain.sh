#!/bin/bash
# Compare identical source trees except the startup Drain in dispatch.hpp.
set -eo pipefail
root=/mnt/docker/o00939992
experiment="$root/jetty-no-start-drain"
source "$root/Ascend/cann/set_env.sh"
export ASCEND_RT_VISIBLE_DEVICES=2,3 TMPDIR="$root/tmp"
mkdir -p "$experiment/logs"
for trial in before:1 after:1 after:2 before:2; do
    variant=${trial%:*}
    round=${trial#*:}
    cd "$experiment/$variant"
    export PYTHONPATH="$PWD" MASTER_PORT=29775
    export EP_JIT_CACHE_DIR="$experiment/cache-$variant"
    export ASCEND_PROCESS_LOG_PATH="$experiment/logs/$variant-$round-ascend"
    echo "START $trial $(date -Is)"
    status=0
    timeout --kill-after=15s 300s "$root/venv/bin/python" -u tests/ep/test_ep.py \
        --num-processes 2 --num-ai-cores 16 --num-tokens 1024 --hidden 4096 \
        --num-topk 4 --num-experts 8 --dispatch-dtype bf16 --test-first-only \
        > "$experiment/logs/$variant-$round.log" 2>&1 || status=$?
    echo "END $trial exit=$status $(date -Is)"
    if [ "$status" -ne 0 ]; then
        tail -60 "$experiment/logs/$variant-$round.log"
        exit "$status"
    fi
done
