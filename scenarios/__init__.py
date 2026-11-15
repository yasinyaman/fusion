"""Sector scenarios: synthetic databases and workloads that push Fusion hard.

Three parts, each usable on its own:

* ``scenarios.synth`` builds the source databases (banking, retail, telecom,
  healthcare, manufacturing, logistics) deterministically, at any scale.
* ``scenarios.cases`` holds the scenarios: what an analyst in that sector
  would ask, written to land on a specific branch of the engine.
* ``scenarios.harness`` runs them through a real ``FusionApp`` and checks
  every answer against an oracle that reads the source data directly.

    python -m scenarios generate --scale 0.1
    python -m scenarios run --scale 0.1
"""
