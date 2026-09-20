import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSTALLER_MODULE = "scripts.installer.installer"
MANIFEST_RELATIVE_PATH = Path(".veripipe") / "installer-manifest.json"


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.pipeline_home = self.base / "framework"
        self.product_cwd = self.base / "product"
        self.outside = self.base / "outside"
        self.pipeline_home.mkdir()
        self.product_cwd.mkdir()
        self.outside.mkdir()
        (self.pipeline_home / "VERSION").write_text("0.1.0\n", encoding="utf-8")
        install_sh = self.pipeline_home / "install.sh"
        install_sh.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        install_sh.chmod(0o755)
        self._write_adapter("sample")
        self._write_templates("sample")

    def _write_adapter(self, product):
        product_root = self.pipeline_home / "products" / product
        product_root.mkdir(parents=True, exist_ok=True)
        (product_root / "adapter.config.json").write_text(
            json.dumps(
                {
                    "product": product,
                    "version": "0.1.0",
                    "framework_version": "0.1.0",
                }
            ),
            encoding="utf-8",
        )

    def _write_templates(self, product):
        product_root = self.pipeline_home / "products" / product / "skills"
        templates = {
            "claude/commands/sample-smoke.md": "run smoke\n",
            "claude/skills/sample-smoke/SKILL.md": "claude smoke\n",
            "codex/sample-smoke/SKILL.md": "codex smoke\n",
            "dsh/sample-smoke/SKILL.md": "dsh smoke\n",
        }
        for relative, content in templates.items():
            path = product_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def _run(self, *args, env_overrides=None):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        env["VERIPIPE_HOME"] = str(self.pipeline_home)
        if env_overrides:
            for key, value in env_overrides.items():
                if value is None:
                    env.pop(key, None)
                else:
                    env[key] = value
        return subprocess.run(
            [sys.executable, "-m", INSTALLER_MODULE, *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _tree_digest(self):
        digest = hashlib.sha256()
        for path in sorted(self.product_cwd.rglob("*")):
            digest.update(path.relative_to(self.product_cwd).as_posix().encode("utf-8"))
            if path.is_file():
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def test_missing_pipeline_home_is_rejected(self):
        result = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
            env_overrides={"VERIPIPE_HOME": None},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("VERIPIPE_HOME", result.stderr)

    def test_unknown_product_is_rejected(self):
        result = self._run(
            "--product",
            "missing",
            "--product-cwd",
            str(self.product_cwd),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown product", result.stderr)

    def test_install_writes_manifest_and_repeat_is_unchanged(self):
        first = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        manifest_path = self.product_cwd / MANIFEST_RELATIVE_PATH
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["product"], "sample")
        self.assertEqual(
            sorted(item["path"] for item in manifest["files"]),
            [
                ".claude/commands/sample-smoke.md",
                ".claude/skills/sample-smoke/SKILL.md",
                ".codex/skills/sample-smoke/SKILL.md",
                ".dsh/skills/sample-smoke/SKILL.md",
            ],
        )
        before = self._tree_digest()

        second = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["status"], "unchanged")
        self.assertEqual(self._tree_digest(), before)

    def test_dry_run_reports_changes_without_writing_product(self):
        result = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
            "--dry-run",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "dry-run")
        self.assertEqual(list(self.product_cwd.iterdir()), [])

    def test_upgrade_protects_local_edits_and_force_preserves_user_files(self):
        installed = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        managed = self.product_cwd / ".codex/skills/sample-smoke/SKILL.md"
        managed.write_text("local edit\n", encoding="utf-8")
        user_file = self.product_cwd / ".codex/skills/user-skill/SKILL.md"
        user_file.parent.mkdir(parents=True)
        user_file.write_text("keep me\n", encoding="utf-8")
        source = self.pipeline_home / "products/sample/skills/codex/sample-smoke/SKILL.md"
        source.write_text("framework upgrade\n", encoding="utf-8")

        refused = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
            "--upgrade",
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("locally modified", refused.stderr)
        self.assertEqual(managed.read_text(encoding="utf-8"), "local edit\n")

        forced = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
            "--upgrade",
            "--force",
        )
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual(managed.read_text(encoding="utf-8"), "framework upgrade\n")
        self.assertEqual(user_file.read_text(encoding="utf-8"), "keep me\n")

    def test_upgrade_removes_only_obsolete_manifest_files(self):
        installed = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        obsolete = self.product_cwd / ".claude/commands/sample-smoke.md"
        source = self.pipeline_home / "products/sample/skills/claude/commands/sample-smoke.md"
        source.unlink()
        user_file = self.product_cwd / ".claude/commands/user-command.md"
        user_file.write_text("keep me\n", encoding="utf-8")

        upgraded = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
            "--upgrade",
        )

        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        self.assertFalse(obsolete.exists())
        self.assertTrue(user_file.exists())
        self.assertEqual(
            json.loads(upgraded.stdout)["removed_files"],
            [".claude/commands/sample-smoke.md"],
        )

    def test_uninstall_only_removes_manifest_files(self):
        installed = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        user_file = self.product_cwd / ".claude/skills/user-owned/SKILL.md"
        user_file.parent.mkdir(parents=True)
        user_file.write_text("keep me\n", encoding="utf-8")
        runtime_file = self.product_cwd / ".tmp/runtime/ledger.sqlite"
        runtime_file.parent.mkdir(parents=True)
        runtime_file.write_text("runtime\n", encoding="utf-8")

        removed = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
            "uninstall",
        )

        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertTrue(user_file.exists())
        self.assertTrue(runtime_file.exists())
        self.assertFalse((self.product_cwd / MANIFEST_RELATIVE_PATH).exists())
        self.assertFalse((self.product_cwd / ".codex/skills/sample-smoke/SKILL.md").exists())

    def test_uninstall_protects_locally_modified_managed_file(self):
        installed = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        managed = self.product_cwd / ".codex/skills/sample-smoke/SKILL.md"
        managed.write_text("local edit\n", encoding="utf-8")

        refused = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
            "uninstall",
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertTrue(managed.exists())

        forced = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
            "uninstall",
            "--force",
        )
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertFalse(managed.exists())

    def test_doctor_detects_manifest_drift(self):
        installed = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        healthy = self._run(
            "doctor",
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )
        self.assertEqual(healthy.returncode, 0, healthy.stderr)
        managed = self.product_cwd / ".claude/commands/sample-smoke.md"
        managed.write_text("drift\n", encoding="utf-8")

        drifted = self._run(
            "doctor",
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )

        self.assertNotEqual(drifted.returncode, 0)
        self.assertIn("manifest drift", drifted.stderr)

    def test_doctor_rejects_outdated_framework_manifest(self):
        installed = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        (self.pipeline_home / "VERSION").write_text("0.2.0\n", encoding="utf-8")
        adapter_path = self.pipeline_home / "products/sample/adapter.config.json"
        adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
        adapter["framework_version"] = "0.2.0"
        adapter_path.write_text(json.dumps(adapter), encoding="utf-8")

        result = self._run(
            "doctor",
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("framework_version", result.stderr)

    def test_doctor_rejects_current_template_drift(self):
        installed = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        source = self.pipeline_home / "products/sample/skills/codex/sample-smoke/SKILL.md"
        source.write_text("new framework template\n", encoding="utf-8")

        result = self._run(
            "doctor",
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("template manifest drift", result.stderr)

    def test_plain_install_cannot_silently_upgrade_manifest_version(self):
        installed = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        manifest_path = self.product_cwd / MANIFEST_RELATIVE_PATH
        (self.pipeline_home / "VERSION").write_text("0.2.0\n", encoding="utf-8")
        adapter_path = self.pipeline_home / "products/sample/adapter.config.json"
        adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
        adapter["framework_version"] = "0.2.0"
        adapter_path.write_text(json.dumps(adapter), encoding="utf-8")

        result = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--upgrade", result.stderr)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["framework_version"], "0.1.0")

    def test_tampered_manifest_cannot_delete_runtime_evidence_with_force(self):
        installed = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        runtime_file = self.product_cwd / ".tmp/runtime/ledger.sqlite"
        runtime_file.parent.mkdir(parents=True)
        runtime_file.write_text("runtime evidence\n", encoding="utf-8")
        manifest_path = self.product_cwd / MANIFEST_RELATIVE_PATH
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"].append(
            {
                "path": ".tmp/runtime/ledger.sqlite",
                "sha256": hashlib.sha256(runtime_file.read_bytes()).hexdigest(),
            }
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
            "uninstall",
            "--force",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("managed", result.stderr)
        self.assertTrue(runtime_file.is_file())
        self.assertTrue(manifest_path.is_file())

    def test_duplicate_manifest_paths_fail_closed(self):
        installed = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        manifest_path = self.product_cwd / MANIFEST_RELATIVE_PATH
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"].append(dict(manifest["files"][0]))
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
            "uninstall",
            "--force",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate managed path", result.stderr)
        self.assertTrue(manifest_path.is_file())

    def test_bare_doctor_validates_all_framework_products(self):
        result = self._run("doctor")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "healthy")
        self.assertEqual([item["product"] for item in payload["products"]], ["sample"])

    def test_path_traversal_and_target_symlink_escape_are_rejected(self):
        traversal = self._run(
            "--product",
            "../sample",
            "--product-cwd",
            str(self.product_cwd),
        )
        self.assertNotEqual(traversal.returncode, 0)
        self.assertIn("safe product name", traversal.stderr)

        (self.product_cwd / ".codex").symlink_to(self.outside, target_is_directory=True)
        escaped = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(self.product_cwd),
        )
        self.assertNotEqual(escaped.returncode, 0)
        self.assertIn("symlink", escaped.stderr)
        self.assertEqual(list(self.outside.rglob("*")), [])

    def test_real_sibling_checkout_is_hard_blocked(self):
        # A product checkout that sits right next to VERIPIPE_HOME (i.e.
        # <pipeline_home>/../<product>) is the framework's own source tree and
        # must never be modified by the installer.
        sibling = self.base / "sample"
        sibling.mkdir()
        result = self._run(
            "--product",
            "sample",
            "--product-cwd",
            str(sibling),
            "--dry-run",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sibling product checkout", result.stderr)


if __name__ == "__main__":
    unittest.main()
