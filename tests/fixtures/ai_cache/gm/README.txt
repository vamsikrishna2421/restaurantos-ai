Replay fixtures for P23a (src/restaurantos/gm/planner.py): the GM planner's one streamed Responses call.

What they are
- responses_stream/<sha256>.json is a meta.cache.ResponseCache entry, {"events": [{"t": <s>, "event": <SSE payload>}]},
  keyed by meta.cache.request_hash over the exact request body GMPlanner sends at the dinner_rush canonical moment
  (tests/fakes/gm.py::rush_world: Sat 2026-11-07 18:48, paneer ACT, incident inc_paneer at epoch 1, trigger act; the
  Patel supplier note makes the report tainted): instructions g1 and the request of gm/plan_stream.py::stream_request
  (input gm.situation.planner_input(report), text.format GM_PLAN_SCHEMA strict as "GMPlan", reasoning effort low with
  summary concise), on the GM route of environment "test" (muse-spark-1.3, 4,000 max output tokens).
- Only tests/gm/test_planner.py::test_replay_dinner_rush_plan reads them (a real MetaClient in replay mode). Every
  other planner test runs on tests/fakes/gm.py. The file holds model output, reasoning summaries and usage: no key,
  no guest data.
- Status: see the bottom of this file.

Re-recording (one stream, about $0.015; P23a's budget is $0.10)
      .venv/bin/python scripts/rerecord.py --root tests/fixtures/ai_cache/gm --max-usd 0.10 --tag P23a \
          -- tests/gm/test_planner.py -k test_replay_dinner_rush_plan
  rerecord.py passes the dev key to the pytest child as MODEL_API_KEY (test settings never read key files). The test
  accepts record status "ok" while it records. If the stream goes idle (12 s) the planner falls back to Chat and the
  run records a chat/ entry instead: delete it and run again.
- Any change to the g1 instructions or request (gm/plan_stream.py), GM_PLAN_SCHEMA, the situation report
  (gm/situation.py, gm/candidates.py, the seed, tests/gm/test_situation.py::rush_snapshot) or the GM route changes the
  hash, and the test fails with a "no replay fixture" message. Re-record, or regenerate the hand-authored stand-in
  offline (no network, no key, $0):
      .venv/bin/python -m tests.fakes.gm
  It replays tests/fakes/gm.py::canned_plan through the real planner and MetaClient in record mode against a local SSE
  answer, so its file name is exactly the replay hash; its final response carries "metadata": {"fixture":
  "hand-authored:P23a"}. After a real recording, delete leftover stand-ins: grep -l hand-authored responses_stream/*.

Status
- Oct 27, 2026: hand-authored stand-in written by P23a (tests.fakes.gm), then replaced by a real recording
  (rerecord:P23a, 1 stream, $0.013015, 7.7 s, 3,479 input and 2,039 output tokens, 1,296 of them reasoning). The model
  kept all three paneer dishes selling, ordered the Patel 3 kg bridge (C06, not the playbook's 4 kg), offered both
  alternatives and notified Carlos, Arjun and Maria with drafts (T2 on this tainted report).
