"""The GM agent (ARCHITECTURE §6): code computes every number; the model only picks among code-made candidates.

- ``gm.situation`` (P22): the deterministic, integers-only situation report, with notes fenced as DATA;
- ``gm.candidates`` (P22): at most 16 candidate actions, each with a code-written effect and its rendered action line;
- ``gm.baseline`` (P22): the playbook, which never reads notes;
- ``gm.simulate`` (P22): the 60-minute dry run without and with the selected steps;
- ``gm.observe`` (P22, P1-2): what an observe re-plan adds (re-route, supplier switch, un-86).

Submodules are imported explicitly (``from restaurantos.gm import situation``). This package imports nothing, so the
planner, trigger and ticker modules (P23a, P23b) load only what they use.
"""
