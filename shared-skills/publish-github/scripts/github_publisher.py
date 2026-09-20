#!/usr/bin/env python3
"""Product-neutral GitHub publisher for veripipe verified findings.

Turns veripipe publication-queue ``bug_report`` events (or a single payload on
stdin) into GitHub Discussion/Issue drafts, and — only when explicitly told to —
posts them via the ``gh`` CLI.

Design constraints (fail-closed, anti-spam, community-respectful):
  * DRY-RUN BY DEFAULT. Posting requires BOTH ``--post`` and ``--yes``.
  * Only machine-verified findings are eligible: ``kind == "bug_report"`` with
    ``oracle_level in {L1,L2,L3}`` and ``llm_involvement == "none"``. L4 / model
    suggestions and free text are never published.
  * Deduplicated by fingerprint; capped by ``--max`` (default 3).
  * Bodies are neutral: only semantic entry id, oracle level, fingerprint and the
    short failure summary — never raw payloads, internal paths, or secrets.
  * Every post carries an automated-disclosure footer (transparency).
  * Target defaults to ``discussion`` because many projects (e.g. dsh) route
    external reports to GitHub Discussions, not Issues.

The network boundary is the injectable ``Poster`` protocol so the logic is unit
tested offline. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence

ELIGIBLE_ORACLE_LEVELS = {"L1", "L2", "L3"}
VERIPIPE_FOOTER = (
    "\n\n---\n"
    "_Filed automatically by [veripipe](https://github.com/topics/dsh-plugin) — an "
    "anti-false-positive verification pipeline. This is a **machine-verified** finding "
    "(structured terminal-state oracle, no LLM judgement). Reproduce with the fingerprint "
    "above. Please close as duplicate if already tracked._"
)


class PublisherError(RuntimeError):
    """Raised for guard violations (fail-closed)."""


@dataclass
class Finding:
    fingerprint: str
    semantic_map_entry_id: str
    oracle_level: str
    failure_summary: str
    event_id: str


@dataclass
class PostResult:
    title: str
    body: str
    fingerprint: str
    url: Optional[str] = None
    posted: bool = False


@dataclass
class PublishReport:
    dry_run: bool
    target: str
    repo: Optional[str]
    results: List[PostResult] = field(default_factory=list)
    skipped: int = 0


def _short(fp: str) -> str:
    return (fp or "")[:8] or "unknown"


def select_findings(events: Sequence[Mapping[str, object]], *, max_count: int) -> List[Finding]:
    """Filter to machine-verified bug reports, dedupe by fingerprint, cap count."""
    seen: set[str] = set()
    out: List[Finding] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("kind") != "bug_report":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        oracle_level = str(payload.get("oracle_level") or "").strip()
        llm = str(payload.get("llm_involvement") or "").strip()
        if oracle_level not in ELIGIBLE_ORACLE_LEVELS:
            continue
        if llm != "none":
            continue
        fingerprint = str(payload.get("fingerprint") or "").strip()
        entry_id = str(payload.get("semantic_map_entry_id") or "").strip()
        summary = str(payload.get("failure_summary") or "").strip()
        if not (fingerprint and entry_id and summary):
            continue
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append(
            Finding(
                fingerprint=fingerprint,
                semantic_map_entry_id=entry_id,
                oracle_level=oracle_level,
                failure_summary=summary,
                event_id=str(event.get("event_id") or ""),
            )
        )
        if len(out) >= max_count:
            break
    return out


def render_post(finding: Finding, *, product_label: str) -> PostResult:
    title = f"[veripipe][{product_label}] {finding.semantic_map_entry_id} ({_short(finding.fingerprint)})"
    body = (
        f"**Product:** {product_label}\n"
        f"**Semantic contract:** `{finding.semantic_map_entry_id}`\n"
        f"**Oracle level:** {finding.oracle_level} (structured terminal state)\n"
        f"**Fingerprint:** `{finding.fingerprint}`\n\n"
        f"**Machine-verified failure**\n\n"
        f"```\n{finding.failure_summary}\n```"
        f"{VERIPIPE_FOOTER}"
    )
    return PostResult(title=title, body=body, fingerprint=finding.fingerprint)


# --- network boundary (injectable; tests never touch the network) -----------
Poster = Callable[[str, PostResult], str]  # (target, post) -> url


def gh_cli_poster(repo: str, category: Optional[str]) -> Poster:
    """Real poster: create a Discussion (GraphQL) or an Issue via the ``gh`` CLI."""

    def _post(target: str, post: PostResult) -> str:
        if target == "issue":
            proc = subprocess.run(
                ["gh", "issue", "create", "--repo", repo,
                 "--title", post.title, "--body", post.body],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                raise PublisherError(f"gh issue create failed: {proc.stderr.strip()}")
            return proc.stdout.strip()
        if target == "discussion":
            if not category:
                raise PublisherError("--discussion-category is required to post a discussion")
            # gh has no first-class discussion create; use the GraphQL API.
            ids = subprocess.run(
                ["gh", "api", "graphql", "-f", f"owner={repo.split('/')[0]}",
                 "-f", f"name={repo.split('/')[1]}", "-f", "query=" +
                 "query($owner:String!,$name:String!){repository(owner:$owner,name:$name)"
                 "{id discussionCategories(first:25){nodes{id name}}}}"],
                capture_output=True, text=True,
            )
            if ids.returncode != 0:
                raise PublisherError(f"gh api graphql (ids) failed: {ids.stderr.strip()}")
            data = json.loads(ids.stdout)["data"]["repository"]
            repo_id = data["id"]
            cat_id = next((c["id"] for c in data["discussionCategories"]["nodes"]
                           if c["name"] == category), None)
            if not cat_id:
                raise PublisherError(f"discussion category not found: {category}")
            mut = subprocess.run(
                ["gh", "api", "graphql", "-f", f"repositoryId={repo_id}",
                 "-f", f"categoryId={cat_id}", "-f", f"title={post.title}",
                 "-f", f"body={post.body}", "-f", "query=" +
                 "mutation($repositoryId:ID!,$categoryId:ID!,$title:String!,$body:String!)"
                 "{createDiscussion(input:{repositoryId:$repositoryId,categoryId:$categoryId,"
                 "title:$title,body:$body}){discussion{url}}}"],
                capture_output=True, text=True,
            )
            if mut.returncode != 0:
                raise PublisherError(f"gh api graphql (createDiscussion) failed: {mut.stderr.strip()}")
            return json.loads(mut.stdout)["data"]["createDiscussion"]["discussion"]["url"]
        raise PublisherError(f"unknown target: {target}")

    return _post


def publish(
    events: Sequence[Mapping[str, object]],
    *,
    target: str,
    repo: Optional[str],
    product_label: str,
    dry_run: bool,
    max_count: int,
    poster: Optional[Poster] = None,
    out_dir: Optional[Path] = None,
) -> PublishReport:
    if target not in ("discussion", "issue"):
        raise PublisherError(f"unsupported target: {target}")
    findings = select_findings(events, max_count=max_count)
    report = PublishReport(dry_run=dry_run, target=target, repo=repo)
    for finding in findings:
        post = render_post(finding, product_label=product_label)
        if dry_run:
            if out_dir is not None:
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / f"{_short(finding.fingerprint)}.md").write_text(
                    f"# {post.title}\n\n{post.body}\n", encoding="utf-8"
                )
        else:
            if not repo:
                raise PublisherError("--repo is required to post")
            if poster is None:
                poster = gh_cli_poster(repo, None)
            post.url = poster(target, post)
            post.posted = True
        report.results.append(post)
    return report


def load_events(*, queue: Optional[Path], read_stdin: bool) -> List[Mapping[str, object]]:
    if read_stdin:
        raw = sys.stdin.read().strip()
        if not raw:
            return []
        payload = json.loads(raw)
        # accept a bare bug_report payload or a full queue event
        if isinstance(payload, Mapping) and payload.get("kind") == "bug_report":
            return [payload]
        return [{"kind": "bug_report", "event_id": "stdin", "payload": payload}]
    if queue is not None:
        events: List[Mapping[str, object]] = []
        for line in Path(queue).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
        return events
    return []


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Publish veripipe verified findings to GitHub.")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--queue", type=Path, help="veripipe publication queue JSONL")
    src.add_argument("--stdin", action="store_true", help="read a single bug_report payload from stdin")
    parser.add_argument("--target", choices=("discussion", "issue"), default="discussion")
    parser.add_argument("--repo", help="owner/name of the destination repository")
    parser.add_argument("--discussion-category", help="Discussions category name (for --target discussion)")
    parser.add_argument("--product-label", default="product")
    parser.add_argument("--max", type=int, default=3, dest="max_count")
    parser.add_argument("--out-dir", type=Path, help="where to write drafts in dry-run")
    parser.add_argument("--post", action="store_true", help="actually create items (default: dry-run)")
    parser.add_argument("--yes", action="store_true", help="required confirmation alongside --post")
    args = parser.parse_args(argv)

    dry_run = not args.post
    if args.post and not args.yes:
        parser.error("--post requires --yes (safety confirmation)")
    if args.post and not args.repo:
        parser.error("--post requires --repo")

    poster = None
    if args.post:
        poster = gh_cli_poster(args.repo, args.discussion_category)
        if args.target == "issue":
            sys.stderr.write(
                "note: many projects (e.g. dsh) route external reports to GitHub "
                "Discussions; confirm the repo accepts issues before posting.\n"
            )

    try:
        events = load_events(queue=args.queue, read_stdin=args.stdin)
        report = publish(
            events,
            target=args.target,
            repo=args.repo,
            product_label=args.product_label,
            dry_run=dry_run,
            max_count=args.max_count,
            poster=poster,
            out_dir=args.out_dir,
        )
    except PublisherError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1

    print(json.dumps(
        {
            "status": "dry-run" if report.dry_run else "posted",
            "target": report.target,
            "repo": report.repo,
            "count": len(report.results),
            "items": [
                {"title": r.title, "fingerprint": r.fingerprint,
                 "posted": r.posted, "url": r.url}
                for r in report.results
            ],
        },
        ensure_ascii=False, indent=2, sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
