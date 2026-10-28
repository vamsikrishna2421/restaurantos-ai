RECORDED replay fixtures for the E1 text eval (P31a: src/restaurantos/evals, scripts/eval.py): real muse-spark-1.3
parses of the 360 cases in config/evals/datasets/orders_v1.jsonl.

What they are
- chat/<sha256>.json is a meta.cache.ResponseCache entry, {"response": <chat completion>}, keyed by
  meta.cache.request_hash over the exact request body OrderAgent.parse sends for one E1 case (prompt o1, the "full"
  strict order schema of the Spice Route seed menu, the ORDER route: muse-spark-1.3, effort minimal, 1500 max
  completion tokens). The request does not depend on the environment, so dev recordings replay under test settings.
- Recorded by scripts/eval.py run (RESTOS_AI_MODE=record, RESTOS_LIVE_TAG=P31a, spend scope dev:P31a-run, data class
  synthetic, no tenant, no session). The run's per-case results (latency, cost, tokens, the parsed turn) are in
  config/evals/published/runs/<run id>.jsonl; the published report is config/evals/published/latest.json.
- tests/evals/test_run.py::test_ci_replay_gate_2pts replays the 60-case gate subset (config/evals/published/
  replay_gate.json) through the real MetaClient and OrderAgent and fails when exact match drops more than 2 points
  below the published baseline, or when any case has no recording.

When the gate reports replay misses
Any change to the o1 prompt (agents/order_prompt.py), the order schema, the seed menu's prompt-visible fields or the
ORDER route changes the request hashes. Re-run and re-publish E1 (dev, with the lead's OK; about $0.75):
    RESTOS_AI_MODE=record RESTOS_LIVE_TAG=P31a .venv/bin/python scripts/eval.py run --run-id e1_<date> --max-usd 0.95
    .venv/bin/python scripts/eval.py report --run-id e1_<date>
then delete the recordings no current request uses. scripts/rerecord.py --root tests/fixtures/ai_cache/evals also
works for the 60 gate cases (-- tests/evals/test_run.py -k replay_gate), but the published report and the gate
baseline then still describe the old run.
