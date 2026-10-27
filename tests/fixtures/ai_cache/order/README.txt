RECORDED replay fixtures for P13 (src/restaurantos/agents/order_agent.py): real muse-spark-1.3 outputs.

What they are
- chat/<sha256>.json is a meta.cache.ResponseCache entry, {"response": <chat completion>}, keyed by
  meta.cache.request_hash over the exact request body OrderAgent.parse sends for one case of
  tests/fakes/order_agent.py::REPLAY_CASES (te_en_canonical, ta_en, es, injection) on the Spice Route seed menu,
  with the "full" schema variant and the ORDER route of environment "test" (muse-spark-1.3, effort minimal,
  1500 max completion tokens).
- Recorded by the lead on Oct 27, 2026 (rerecord:P13, 4 calls, $0.0082, p50 1.85 s). The tests pass on these real
  outputs, so the canned OrderTurnLLM in CASES and the model agree on all four cases.

Re-recording (lead only; 4 calls, well under $0.05)
      .venv/bin/python scripts/rerecord.py --root tests/fixtures/ai_cache/order --max-usd 0.05 --tag P13 \
          -- tests/agents/test_order_agent.py -k "replay or injection"
  (test_injection_flag_cart_unchanged replays the injection case but has no "replay" in its name.)
- Any change to the o1 prompt (agents/order_prompt.py), the order schema, the seed menu's prompt-visible fields or
  the ORDER route changes the hashes: re-record, or regenerate hand-authored stand-ins offline with
      .venv/bin/python -m tests.fakes.order_agent
  (stand-ins carry "system_fingerprint": "hand-authored:P13"; delete any left over after a recording with
  grep -l hand-authored chat/*.json).
- rerecord.py passes the dev key to the pytest child as MODEL_API_KEY (test settings never read key files).
