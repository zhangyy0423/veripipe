"""Manifest-driven installer for product-specific agent thin shells."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable


MANIFEST_RELATIVE_PATH = Path(".veripipe") / "installer-manifest.json"
PRODUCT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
TEMPLATE_MAPPINGS = (
    (Path("claude/commands"), Path(".claude/commands")),
    (Path("claude/skills"), Path(".claude/skills")),
    (Path("codex"), Path(".codex/skills")),
    (Path("dsh"), Path(".dsh/skills")),
)
MANAGED_DESTINATION_ROOTS = tuple(destination for _, destination in TEMPLATE_MAPPINGS)


class InstallerError(RuntimeError):
    """Fail-closed installer error suitable for CLI reporting."""


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def require_pipeline_home() -> Path:
    raw_home = os.environ.get("VERIPIPE_HOME", "").strip()
    if not raw_home:
        raise InstallerError("VERIPIPE_HOME is required")
    home = Path(raw_home).expanduser()
    if not home.is_dir():
        raise InstallerError(f"VERIPIPE_HOME is not a directory: {home}")
    return home.resolve()


def validate_product_name(product: str | None) -> str:
    if not product or not PRODUCT_NAME_PATTERN.fullmatch(product) or product in {".", ".."}:
        raise InstallerError("product must be a safe product name")
    return product


def load_framework_version(pipeline_home: Path) -> str:
    version_path = pipeline_home / "VERSION"
    if not version_path.is_file():
        raise InstallerError(f"VERSION is missing: {version_path}")
    version = version_path.read_text(encoding="utf-8").strip()
    if not version:
        raise InstallerError("VERSION is empty")
    return version


def load_adapter(pipeline_home: Path, product: str) -> dict[str, Any]:
    adapter_path = pipeline_home / "products" / product / "adapter.config.json"
    if not adapter_path.is_file():
        raise InstallerError(f"unknown product: {product}")
    try:
        adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InstallerError(f"invalid adapter config: {adapter_path}: {error}") from error
    if adapter.get("product") != product:
        raise InstallerError(f"adapter product mismatch for {product}")
    framework_version = load_framework_version(pipeline_home)
    if adapter.get("framework_version") != framework_version:
        raise InstallerError(
            "adapter framework_version does not match VERSION: "
            f"{adapter.get('framework_version')!r} != {framework_version!r}"
        )
    return adapter


def require_product_root(pipeline_home: Path, product: str, product_cwd: str | Path) -> Path:
    raw_root = Path(product_cwd).expanduser()
    if raw_root.is_symlink():
        raise InstallerError(f"product cwd must not be a symlink: {raw_root}")
    if not raw_root.is_dir():
        raise InstallerError(f"product cwd is not a directory: {raw_root}")
    product_root = raw_root.resolve()
    protected_checkout = (pipeline_home.parent / product).resolve()
    if product_root == protected_checkout:
        raise InstallerError(
            f"refusing to modify the framework's sibling product checkout: {product_root}"
        )
    return product_root


def ensure_safe_destination(product_root: Path, relative_path: Path) -> Path:
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise InstallerError(f"unsafe installer path: {relative_path}")
    destination = product_root / relative_path
    current = product_root
    for part in relative_path.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise InstallerError(f"target path contains symlink: {current}")
    resolved_destination = destination.resolve(strict=False)
    try:
        resolved_destination.relative_to(product_root)
    except ValueError as error:
        raise InstallerError(f"target path escapes product cwd: {relative_path}") from error
    if destination.is_symlink():
        raise InstallerError(f"target file is a symlink: {destination}")
    return destination


def collect_templates(pipeline_home: Path, product: str) -> dict[str, bytes]:
    template_root = pipeline_home / "products" / product / "skills"
    templates: dict[str, bytes] = {}
    for source_relative, destination_relative in TEMPLATE_MAPPINGS:
        source_root = template_root / source_relative
        if not source_root.exists():
            continue
        if source_root.is_symlink() or not source_root.is_dir():
            raise InstallerError(f"template root must be a real directory: {source_root}")
        for source in sorted(source_root.rglob("*")):
            if source.is_symlink():
                raise InstallerError(f"template symlink is not allowed: {source}")
            if not source.is_file():
                continue
            relative = destination_relative / source.relative_to(source_root)
            if ".." in relative.parts:
                raise InstallerError(f"template path traversal is not allowed: {source}")
            key = relative.as_posix()
            if key in templates:
                raise InstallerError(f"duplicate installer destination: {key}")
            templates[key] = source.read_bytes()
    if not templates:
        raise InstallerError(f"no installable templates for product: {product}")
    return templates


def manifest_path(product_root: Path) -> Path:
    return ensure_safe_destination(product_root, MANIFEST_RELATIVE_PATH)


def load_manifest(product_root: Path) -> dict[str, Any] | None:
    path = manifest_path(product_root)
    if not path.exists():
        return None
    if not path.is_file():
        raise InstallerError(f"installer manifest is not a file: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InstallerError(f"invalid installer manifest: {path}: {error}") from error
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list):
        raise InstallerError(f"unsupported installer manifest: {path}")
    return manifest


def manifest_entries(manifest: dict[str, Any]) -> dict[str, str]:
    entries: dict[str, str] = {}
    for item in manifest.get("files", []):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise InstallerError("installer manifest contains an invalid file entry")
        relative = Path(item["path"])
        digest = item.get("sha256")
        if (
            relative.is_absolute()
            or relative in {Path(""), Path(".")}
            or ".." in relative.parts
            or not any(
                relative != root and root in relative.parents
                for root in MANAGED_DESTINATION_ROOTS
            )
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise InstallerError(
                f"installer manifest contains an unsafe or non-managed file entry: {item!r}"
            )
        key = relative.as_posix()
        if key in entries:
            raise InstallerError(f"installer manifest contains duplicate managed path: {key}")
        entries[key] = digest
    return entries


def detect_local_modifications(product_root: Path, entries: dict[str, str]) -> list[str]:
    modified: list[str] = []
    for relative, expected_digest in sorted(entries.items()):
        destination = ensure_safe_destination(product_root, Path(relative))
        if not destination.is_file() or sha256_file(destination) != expected_digest:
            modified.append(relative)
    return modified


def build_manifest(product: str, framework_version: str, templates: dict[str, bytes]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "product": product,
        "framework_version": framework_version,
        "files": [
            {"path": relative, "sha256": sha256_bytes(content)}
            for relative, content in sorted(templates.items())
        ],
    }


def atomic_write(path: Path, content: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary_path.unlink(missing_ok=True)


def serialize_manifest(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def install_or_upgrade(
    *,
    pipeline_home: Path,
    product: str,
    product_root: Path,
    upgrade: bool,
    force: bool,
    dry_run: bool,
) -> dict[str, Any]:
    load_adapter(pipeline_home, product)
    framework_version = load_framework_version(pipeline_home)
    templates = collect_templates(pipeline_home, product)
    existing_manifest = load_manifest(product_root)
    existing_entries: dict[str, str] = {}
    if existing_manifest is not None:
        if existing_manifest.get("product") != product:
            raise InstallerError("installed manifest belongs to a different product")
        existing_entries = manifest_entries(existing_manifest)
        modified = detect_local_modifications(product_root, existing_entries)
        if modified and not force:
            raise InstallerError("locally modified managed files: " + ", ".join(modified))

    new_manifest = build_manifest(product, framework_version, templates)
    desired_entries = manifest_entries(new_manifest)
    obsolete = sorted(set(existing_entries) - set(desired_entries))
    changed = [
        relative
        for relative, digest in sorted(desired_entries.items())
        if existing_entries.get(relative) != digest
        or not ensure_safe_destination(product_root, Path(relative)).is_file()
        or sha256_file(ensure_safe_destination(product_root, Path(relative))) != digest
    ]
    manifest_definition_changed = (
        existing_manifest is not None and existing_manifest != new_manifest
    )
    if (
        existing_manifest is not None
        and (changed or obsolete or manifest_definition_changed)
        and not upgrade
    ):
        raise InstallerError("installed templates differ; rerun with --upgrade")

    unmanaged_conflicts = []
    for relative in desired_entries:
        destination = ensure_safe_destination(product_root, Path(relative))
        if relative not in existing_entries and destination.exists():
            unmanaged_conflicts.append(relative)
    if unmanaged_conflicts and not force:
        raise InstallerError("refusing to overwrite unmanaged files: " + ", ".join(sorted(unmanaged_conflicts)))

    manifest_content = serialize_manifest(new_manifest)
    current_manifest_content = (
        manifest_path(product_root).read_bytes() if manifest_path(product_root).is_file() else None
    )
    if not changed and not obsolete and current_manifest_content == manifest_content:
        return {"status": "unchanged", "product": product, "files": [], "removed_files": []}

    result = {
        "status": "dry-run" if dry_run else ("upgraded" if existing_manifest else "installed"),
        "product": product,
        "files": changed,
        "removed_files": obsolete,
    }
    if dry_run:
        return result
    for relative, content in sorted(templates.items()):
        destination = ensure_safe_destination(product_root, Path(relative))
        if destination.is_file() and sha256_file(destination) == sha256_bytes(content):
            continue
        atomic_write(destination, content)
    for relative in obsolete:
        destination = ensure_safe_destination(product_root, Path(relative))
        if destination.is_file():
            destination.unlink()
    atomic_write(manifest_path(product_root), manifest_content)
    return result


def uninstall(*, product: str, product_root: Path, force: bool, dry_run: bool) -> dict[str, Any]:
    manifest = load_manifest(product_root)
    if manifest is None:
        return {"status": "unchanged", "product": product, "files": []}
    if manifest.get("product") != product:
        raise InstallerError("installed manifest belongs to a different product")
    entries = manifest_entries(manifest)
    modified = detect_local_modifications(product_root, entries)
    if modified and not force:
        raise InstallerError("locally modified managed files: " + ", ".join(modified))
    removed = sorted(entries)
    if dry_run:
        return {"status": "dry-run", "product": product, "files": removed}
    for relative in removed:
        destination = ensure_safe_destination(product_root, Path(relative))
        if destination.is_file():
            destination.unlink()
    manifest_file = manifest_path(product_root)
    manifest_file.unlink()
    try:
        manifest_file.parent.rmdir()
    except OSError:
        pass
    return {"status": "uninstalled", "product": product, "files": removed}


def doctor_product(pipeline_home: Path, product: str, product_root: Path | None) -> dict[str, Any]:
    load_adapter(pipeline_home, product)
    framework_version = load_framework_version(pipeline_home)
    templates = collect_templates(pipeline_home, product)
    result: dict[str, Any] = {"product": product, "template_count": len(templates)}
    if product_root is None:
        result["installed"] = False
        return result
    manifest = load_manifest(product_root)
    if manifest is None:
        result["installed"] = False
        return result
    if manifest.get("product") != product:
        raise InstallerError("installed manifest belongs to a different product")
    if manifest.get("framework_version") != framework_version:
        raise InstallerError(
            "installed manifest framework_version drift: "
            f"{manifest.get('framework_version')!r} != {framework_version!r}; run --upgrade"
        )
    installed_entries = manifest_entries(manifest)
    expected_entries = manifest_entries(
        build_manifest(product, framework_version, templates)
    )
    if installed_entries != expected_entries:
        raise InstallerError("installed template manifest drift; run --upgrade")
    modified = detect_local_modifications(product_root, installed_entries)
    if modified:
        raise InstallerError("manifest drift: " + ", ".join(modified))
    result["installed"] = True
    result["managed_file_count"] = len(installed_entries)
    return result


def discover_products(pipeline_home: Path) -> Iterable[str]:
    products_root = pipeline_home / "products"
    if not products_root.is_dir():
        raise InstallerError(f"products directory is missing: {products_root}")
    for child in sorted(products_root.iterdir()):
        if child.is_dir() and (child / "adapter.config.json").is_file():
            yield child.name


def doctor(
    *, pipeline_home: Path, product: str | None, product_root: Path | None
) -> dict[str, Any]:
    load_framework_version(pipeline_home)
    launcher = pipeline_home / "install.sh"
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise InstallerError(f"install.sh is missing or not executable: {launcher}")
    products = [validate_product_name(product)] if product else list(discover_products(pipeline_home))
    if not products:
        raise InstallerError("no product adapters found")
    checks = [
        doctor_product(pipeline_home, name, product_root if name == product else None)
        for name in products
    ]
    return {"status": "healthy", "framework_version": load_framework_version(pipeline_home), "products": checks}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("install", "uninstall", "doctor"), default="install")
    parser.add_argument("--product")
    parser.add_argument("--product-cwd", default=os.getcwd())
    parser.add_argument("--upgrade", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        pipeline_home = require_pipeline_home()
        if args.action == "doctor":
            if args.upgrade or args.force or args.dry_run:
                raise InstallerError("doctor does not accept --upgrade, --force, or --dry-run")
            product = validate_product_name(args.product) if args.product else None
            product_root = (
                require_product_root(pipeline_home, product, args.product_cwd) if product else None
            )
            result = doctor(
                pipeline_home=pipeline_home,
                product=product,
                product_root=product_root,
            )
        else:
            product = validate_product_name(args.product)
            product_root = require_product_root(pipeline_home, product, args.product_cwd)
            load_adapter(pipeline_home, product)
            if args.action == "uninstall":
                if args.upgrade:
                    raise InstallerError("uninstall does not accept --upgrade")
                result = uninstall(
                    product=product,
                    product_root=product_root,
                    force=args.force,
                    dry_run=args.dry_run,
                )
            else:
                result = install_or_upgrade(
                    pipeline_home=pipeline_home,
                    product=product,
                    product_root=product_root,
                    upgrade=args.upgrade,
                    force=args.force,
                    dry_run=args.dry_run,
                )
    except (InstallerError, OSError) as error:
        print(f"installer error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
