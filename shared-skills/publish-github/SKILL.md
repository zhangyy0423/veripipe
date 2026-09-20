# publish-github — file veripipe verified findings to GitHub

A product-neutral skill that turns veripipe publication-queue **`bug_report`**
events into GitHub **Discussion** (default) or **Issue** drafts, and — only when
you explicitly opt in — posts them via the `gh` CLI.

It is deliberately conservative, because auto-posting to someone else's community
is easy to get wrong:

- **Dry-run by default.** Posting requires BOTH `--post` and `--yes`.
- **Machine-verified findings only:** `kind == bug_report`, `oracle_level ∈ {L1,L2,L3}`,
  `llm_involvement == none`. L4 / model suggestions and free text are never published.
- **Deduplicated** by fingerprint and **capped** by `--max` (default 3).
- **Neutral bodies:** semantic entry id, oracle level, fingerprint, and the short
  failure summary only — never raw payloads, internal paths, or secrets.
- **Automated-disclosure footer** on every post (transparency).
- **Discussions by default**, because many projects route external reports to
  GitHub Discussions rather than Issues. Check the destination project's
  CONTRIBUTING before posting, and prefer Discussions unless Issues are invited.

## Inputs

Two sources (mutually exclusive):

- `--queue <path>` — a veripipe `--queue` JSONL (batch output), or
- `--stdin` — a single `bug_report` payload on stdin (so it can be used directly
  as an adapter `publishing.create_bug_command`).

## Usage

Preview drafts (writes markdown, posts nothing):

```bash
python3 shared-skills/publish-github/scripts/github_publisher.py \
  --queue /tmp/veripipe-queue.jsonl \
  --product-label myproduct \
  --out-dir /tmp/veripipe-drafts
```

Post to Discussions (opt-in, requires an authenticated `gh`):

```bash
python3 shared-skills/publish-github/scripts/github_publisher.py \
  --queue /tmp/veripipe-queue.jsonl \
  --target discussion --repo owner/repo --discussion-category "Bug Reports" \
  --product-label myproduct \
  --post --yes
```

As an adapter `create_bug_command` (one payload per invocation):

```json
{
  "publishing": {
    "enabled": true,
    "create_bug_command": [
      "python3", "shared-skills/publish-github/scripts/github_publisher.py",
      "--stdin", "--target", "discussion",
      "--repo", "owner/repo", "--discussion-category", "Bug Reports",
      "--product-label", "myproduct", "--post", "--yes"
    ]
  }
}
```

Leave `--post`/`--yes` out (or `enabled:false`) to stage drafts for human review.

## Guarantees

- Standard-library only; the network boundary (`gh`) is injectable and fully unit
  tested offline (`tests/unit/publish_github/`).
- Fails closed on missing repo, missing category, unknown target, `--post`
  without `--yes`, or a `gh` error.
