# Product adapters & agent thin-shells

`veripipe` is product-neutral. A product joins the pipeline by shipping an
**adapter** in its own (private) repository. Nothing here contains product
source, semantic content, URLs, or credentials.

## Adapter layout

```
products/<product>/
  adapter.config.json      # product id, framework_version, playwright config
  semantic-map.yaml        # L2 terminal-state oracle definitions
  skills/                  # agent thin-shells (optional, per host)
    claude/
      commands/<command>.md
      skills/<name>/SKILL.md
    codex/<name>/SKILL.md
    dsh/<name>/SKILL.md
```

- `adapter.config.json` names the product and pins `framework_version` to the
  framework `VERSION`. The installer and `doctor` refuse to proceed on drift.
- `semantic-map.yaml` supplies the machine-comparable terminal states the L2
  oracle checks. It never contains agent output text.
- `skills/` holds *thin-shells*: small host-specific entry points that call the
  neutral engine (or the MCP tools). They carry no product logic.

## Symmetric host mapping

The installer maps each host's template source to the host's on-disk
convention. All three hosts are first-class and symmetric:

| Host | Template source (in adapter) | Installed destination |
| --- | --- | --- |
| Claude Code | `skills/claude/commands/<cmd>.md` | `.claude/commands/<cmd>.md` |
| Claude Code | `skills/claude/skills/<name>/SKILL.md` | `.claude/skills/<name>/SKILL.md` |
| Codex | `skills/codex/<name>/SKILL.md` | `.codex/skills/<name>/SKILL.md` |
| Ducc / dsh | `skills/dsh/<name>/SKILL.md` | `.dsh/skills/<name>/SKILL.md` |

The `.dsh/skills/<name>/SKILL.md` destination matches the dsh host home
(`.dsh/`) convention, exactly mirroring how Codex uses `.codex/skills`. Ship a
`skills/dsh/` directory in the adapter and the installer places it for you.

## Installer

The installer is manifest-driven and fail-closed. It resolves the framework
from `VERIPIPE_HOME` and writes a SHA-256 manifest to
`<product-cwd>/.veripipe/installer-manifest.json`.

```bash
# Install (or re-run; unchanged installs are a no-op)
VERIPIPE_HOME=/path/to/veripipe \
  python3 -m scripts.installer.installer --product <product> --product-cwd /path/to/product

# Preview only
... --product <product> --product-cwd /path/to/product --dry-run

# Upgrade after a framework bump (protects locally edited files unless --force)
... --product <product> --product-cwd /path/to/product --upgrade

# Health check: manifest drift, template drift, framework_version drift
VERIPIPE_HOME=/path/to/veripipe \
  python3 -m scripts.installer.installer doctor --product <product> --product-cwd /path/to/product

# Remove only the files recorded in the manifest
... uninstall --product <product> --product-cwd /path/to/product
```

Safety properties enforced by the installer and covered by tests:

- rejects absolute paths, `..` traversal, and symlink escape of the product cwd;
- refuses to modify the framework's own sibling checkout;
- protects locally modified managed files (needs `--force` to overwrite);
- removes only files it recorded — never user files or runtime evidence
  (e.g. a `.tmp/` ledger), even with a tampered manifest and `--force`;
- fails closed on duplicate managed paths or an unsupported manifest schema.

## Confidentiality

Keep product source, internal prompts, intranet addresses, project
identifiers, run logs, queue payloads, credentials, and unreviewed product
knowledge out of any adapter that is published. Adapters and the private
knowledge they reference live in a separate private repository.
