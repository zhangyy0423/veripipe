#!/usr/bin/env python3
"""Offline JSON model command used only by owner-approved pilot tests."""

import json
import os
from pathlib import Path
import sys


request = json.load(sys.stdin)
route = request["route"]
if os.environ.get("MODEL_PILOT_FIXTURE_MODE") == "provider-error":
    print(os.environ.get("MODEL_PILOT_FIXTURE_SECRET", "fixture-secret"), file=sys.stderr)
    raise SystemExit(3)
marker = os.environ.get("MODEL_PILOT_FIXTURE_MARKER")
if marker:
    path = Path(marker)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"role": route["role"]}, sort_keys=True) + "\n")

print(
    json.dumps(
        {
            "decision": "allow",
            "reason": "offline fixture advice only",
            "usage": {
                "input_tokens": 20,
                "output_tokens": 5,
                "estimated_cost": 0.0,
                "duration_ms": 10,
            },
            "validation_result": "schema-valid",
            "prompt_version": route["prompt_version"],
        },
        sort_keys=True,
    )
)
