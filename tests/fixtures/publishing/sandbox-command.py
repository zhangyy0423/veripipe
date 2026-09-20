#!/usr/bin/env python3
"""Deterministic JSON command fixture for guarded publisher contracts."""

import argparse
import json
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("create-bug", "publish-wiki"))
    args = parser.parse_args()
    payload = json.load(sys.stdin)
    publication = payload.get("_publication")
    if not isinstance(publication, dict):
        raise ValueError("_publication is required")
    event_id = str(publication.get("event_id") or "").strip()
    payload_sha256 = str(publication.get("payload_sha256") or "").strip()
    if not event_id or not payload_sha256:
        raise ValueError("stable publication identity is required")
    if args.action == "create-bug":
        result = {
            "card_id": f"sandbox-{event_id}",
            "_publication": publication,
        }
    else:
        result = {
            "page_id": f"sandbox-{event_id}",
            "_publication": publication,
        }
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True)


if __name__ == "__main__":
    main()
