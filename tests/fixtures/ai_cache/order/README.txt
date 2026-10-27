HAND-AUTHORED replay fixtures for P13 (src/restaurantos/agents/order_agent.py). NOT recorded from Meta.

What they are
- chat/<sha256>.json is a meta.cache.ResponseCache entry, {"response": <chat completion>}, keyed by
  meta.cache.request_hash over the exact request body OrderAgent.parse sends for one case of
  tests/fakes/order_agent.py::REPLAY_CASES (te_en_canonical, ta_en, es, injection) on the Spice Route seed menu,
  with the "full" schema variant and the ORDER route of environment "test" (muse-spark-1.3, effort minimal,
  1500 max completion tokens).
- The model output in each file is that case's canned OrderTurnLLM, written by hand (P13, Oct 27). Every response
  carries "system_fingerprint": "hand-authored:P13" and the id "chatcmpl-hand-authored-<case>". The usage numbers
  are rough estimates (prompt characters / 3), not measurements: never quote them.

How they were made
- Offline, by tests/fakes/order_agent.py::write_hand_authored_fixtures: the real OrderAgent and a real MetaClient
  in record mode on an httpx.MockTransport, so the file names are exactly the hashes a replay computes. Nothing left
  the machine and no key was read. Regenerate the stand-ins with:
      .venv/bin/python -m tests.fakes.order_agent
- Any change to the o1 prompt (agents/order_prompt.py), the order schema, the seed menu's prompt-visible fields or
  the ORDER route changes the hashes: regenerate the stand-ins or re-record.

Re-recording with real Spark outputs (lead only; 4 calls, well under P13's $0.05)
      .venv/bin/python scripts/rerecord.py --root tests/fixtures/ai_cache/order --max-usd 0.05 --tag P13 \
          -- tests/agents/test_order_agent.py -k "replay or injection"
  (test_injection_flag_cart_unchanged replays the injection case but has no "replay" in its name.)
- make_test_settings keeps environment "test" in a rerecord run, and Settings reads the repo-root key files only in
  "dev", so the key has to come from the MODEL_API_KEY environment variable for that run.
- A recording overwrites the stand-in with the same hash and carries no hand-authored marker; then delete any
  stale stand-ins (grep -l hand-authored chat/*.json) and update this file.
