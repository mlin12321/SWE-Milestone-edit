#!/usr/bin/env bash
# Run the agent-director on a fixed random sample of SWE-Milestone and
# summarize it. Every setting is a variable below; override any of them in the
# environment:
#
#   NUM_TASKS=3 ./scripts/run_director_eval.sh
#   STEPS=summarize TRIAL=director_seed20261127_n30 ./scripts/run_director_eval.sh
#
# Starting it again resumes the trials it finds; the budget ledger remembers
# what was spent.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[[ -f .env_private ]] && set -a && source .env_private && set +a
: "${SWE_MILESTONE_DATA_ROOT:?set it in .env_private}"

# --- what to run ---
SEED="${SEED:-20261127}"                 # fixes the sample
NUM_TASKS="${NUM_TASKS:-30}"             # milestones allotted at most, over all repos
REPOS="${REPOS:-}"                       # only repos whose name contains one of these (comma separated)
ALL_REPOS="${ALL_REPOS:-0}"              # 1: also draw repos whose images are not pulled, and pull them
STEPS="${STEPS:-sample run summarize}"
FRESH="${FRESH:-0}"                      # 1: start new trials instead of resuming the ones found

# --- the agents ---
MODEL="${MODEL:-claude-sonnet-5-5}"      # the director's agents call it; key in UNIFIED_API_KEY
# ... or a whole agent instead of a model, e.g. trial_configs/director_agents/cursor-grok-4.7.json.
# Its program is mounted from this machine; it also needs its login and its hosts:
#   AGENT_HOME_FILES=.config/cursor/auth.json AGENT_DOMAINS=api2.cursor.sh,api2direct.cursor.sh,api3.cursor.sh,agentn.api5.cursor.sh,agent.api5.cursor.sh,repo42.cursor.sh
AGENT_JSON="${AGENT_JSON:-}"
[[ -n "$AGENT_JSON" ]] && AGENT_JSON="$(realpath "$AGENT_JSON")" && MODEL="$(basename "$AGENT_JSON" .json)"
export DIRECTOR_AGENT_JSON="$AGENT_JSON"
export DIRECTOR_AGENT_HOME_FILES="${AGENT_HOME_FILES:-}"
export SWE_MILESTONE_EXTRA_DOMAINS="${AGENT_DOMAINS:-}"
# A trial needs some key to start; an agent with its own login does not use it.
[[ -n "$AGENT_JSON" ]] && export UNIFIED_API_KEY="${UNIFIED_API_KEY:-unused}"
export DIRECTOR_MAX_DEPTH="${DIRECTOR_MAX_DEPTH:-}"   # 1 = no sub-teams (a solo run)

# --- limits ---
BUDGET_USD="${BUDGET_USD:-40}"           # every trial on this ledger, together
BUDGET_FILE="${BUDGET_FILE:-$ROOT/.swe-milestone/director_budget.json}"
COST_LIMIT="${COST_LIMIT:-15}"           # dollars one repo's trial may spend
TIMEOUT="${TIMEOUT:-18000}"              # seconds the harness gives one agent invocation
PRICE_IN="${PRICE_IN:-3}"                # dollars per million tokens, for an agent that
PRICE_OUT="${PRICE_OUT:-15}"             #   reports no usage: an assumption, not a bill

# --- where things are ---
export DIRECTOR_SDK="${DIRECTOR_SDK:-$ROOT/../software-director-sdk}"
export DIRECTOR_EVAL_DRIVER="${DIRECTOR_EVAL_DRIVER:-$ROOT/../DirectorEvalDriver}"
TRIAL="${TRIAL:-director_seed${SEED}_n${NUM_TASKS}}"
OUT="${OUT:-$ROOT/.swe-milestone/director_eval/$TRIAL}"

export DIRECTOR_BUDGET_FILE="$BUDGET_FILE" DIRECTOR_BUDGET_USD="$BUDGET_USD" DIRECTOR_COST_LIMIT="$COST_LIMIT"
export DIRECTOR_TIMEOUT="$((TIMEOUT - 600))"
export DIRECTOR_EXTRA_ARGS="${DIRECTOR_EXTRA_ARGS:---price-in $PRICE_IN --price-out $PRICE_OUT}"

mkdir -p "$OUT"
SAMPLE="$OUT/sample.json"
CONFIG="$OUT/trial.yaml"
{
    echo "trial=$TRIAL seed=$SEED num=$NUM_TASKS model=$MODEL agent_json=${DIRECTOR_AGENT_JSON:-none}"
    echo "budget=\$$BUDGET_USD ($BUDGET_FILE) per_repo=\$$COST_LIMIT timeout=${TIMEOUT}s"
    echo "sdk=$(git -C "$DIRECTOR_SDK" rev-parse --short HEAD) driver=$(git -C "$DIRECTOR_EVAL_DRIVER" rev-parse --short HEAD) harness=$(git rev-parse --short HEAD)"
} | tee "$OUT/settings.txt"

for step in $STEPS; do
    case "$step" in
    sample)
        flags=(); [[ "$ALL_REPOS" == 1 ]] && flags+=(--all-repos)
        uv run python scripts/sample_milestones.py --seed "$SEED" --num "$NUM_TASKS" \
            --repos "$REPOS" "${flags[@]}" --out "$SAMPLE"
        ;;
    run)
        printf 'data_root: ${SWE_MILESTONE_DATA_ROOT}\ntrial_name: %s\nagent: director\nmodel: %s\ntimeout: %s\n' \
            "$TRIAL" "$MODEL" "$TIMEOUT" > "$CONFIG"
        pids=()
        fresh=(); [[ "$FRESH" == 1 ]] && fresh+=(--new)
        while read -r repo count; do
            [[ "$ALL_REPOS" == 1 ]] && ./scripts/pull_images.sh --repo "$repo"
            launched="$(uv run python scripts/run_all.py --config "$CONFIG" --repos "$repo" \
                --milestones "$count" --skip-testbed-copy "${fresh[@]}" 2>&1 | tee -a "$OUT/run.log")"
            # The launcher detaches each repo's worker and prints its process id.
            pid="$(grep -o 'PID=[0-9]*' <<<"$launched" | cut -d= -f2 || true)"
            [[ -n "$pid" ]] && pids+=("$pid")
        done < <(uv run python -c "
import json, sys
for repo, count in json.load(open(sys.argv[1]))['milestones'].items(): print(repo, count)" "$SAMPLE")
        echo "waiting for ${#pids[@]} trial(s): ${pids[*]:-}"
        for pid in "${pids[@]}"; do
            while kill -0 "$pid" 2>/dev/null; do sleep 30; done
        done
        ;;
    summarize)
        uv run python scripts/summarize_director_eval.py --trial "$TRIAL" --ledger "$BUDGET_FILE" \
            --out "$OUT/summary.json" | tee "$OUT/summary.txt"
        ;;
    *) echo "unknown step: $step" >&2; exit 2 ;;
    esac
done
