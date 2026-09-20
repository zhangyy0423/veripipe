# Publishing verified findings to GitHub

The `publish-github` skill turns veripipe publication-queue findings into GitHub
**Discussion** (default) or **Issue** drafts, and — only when you explicitly opt
in — posts them via the `gh` CLI. It is product-neutral: the destination repo
and category are parameters, never hard-coded.

Source: `shared-skills/publish-github/` (SKILL.md + `scripts/github_publisher.py`).

## Why it is conservative

Auto-posting into someone else's community is easy to get wrong, so the skill is
fail-closed and low-noise by construction:

- **Dry-run by default.** Posting requires BOTH `--post` and `--yes`.
- **Machine-verified findings only.** Eligible events are `kind == bug_report`
  with `oracle_level ∈ {L1,L2,L3}` and `llm_involvement == none`. L4 / model
  suggestions and free text are never published.
- **Deduplicated** by fingerprint and **capped** by `--max` (default 3).
- **Neutral bodies.** Semantic entry id, oracle level, fingerprint, and the
  short failure summary only — never raw payloads, internal paths, or secrets.
- **Automated-disclosure footer** on every draft, for transparency.
- **Discussions by default.** Many projects route external reports to GitHub
  Discussions rather than Issues; check the destination's CONTRIBUTING and
  prefer Discussions unless Issues are invited.

## Inputs

Two mutually exclusive sources:

- `--queue <path>` — a veripipe `--queue` JSONL (batch output), or
- `--stdin` — a single `bug_report` payload on stdin, so the skill can be used
  directly as an adapter `publishing.create_bug_command`.

## Usage

Preview drafts (writes markdown, posts nothing):

```bash
python3 shared-skills/publish-github/scripts/github_publisher.py \
  --queue /tmp/veripipe-queue.jsonl \
  --product-label myproduct \
  --out-dir /tmp/veripipe-drafts
```

Post to Discussions (opt-in; needs an authenticated `gh`):

```bash
python3 shared-skills/publish-github/scripts/github_publisher.py \
  --queue /tmp/veripipe-queue.jsonl \
  --target discussion --repo owner/repo --discussion-category "Bug Reports" \
  --product-label myproduct \
  --post --yes
```

## Adapter integration

Wire the skill as an adapter `create_bug_command` (one payload per invocation)
and keep `enabled: false` to stage drafts for human review:

```json
{
  "publishing": {
    "enabled": false,
    "create_bug_command": [
      "python3", "shared-skills/publish-github/scripts/github_publisher.py",
      "--stdin", "--target", "discussion",
      "--repo", "owner/repo", "--discussion-category", "Bug Reports",
      "--product-label", "myproduct", "--post", "--yes"
    ]
  }
}
```

## Guarantees

- Standard-library only; the `gh` network boundary is injectable and fully unit
  tested offline (`tests/unit/publish_github/`).
- Fails closed on missing repo, missing category, unknown target, `--post`
  without `--yes`, or a `gh` error.
- Never emits product source, internal identifiers, or credentials; only the
  neutral summary fields above cross into a post.
