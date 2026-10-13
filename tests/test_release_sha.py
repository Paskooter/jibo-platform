"""Release pin tooling runs only in temporary, synthetic Git checkouts."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "PLUGIN_" + "RELEASE_SHA"
SYNTHETIC_SHA = "a1b2c3d4" * 5


class ReleaseSHAScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jibo-release-sha-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        self.script = self.root / "scripts/set-release-sha.py"
        self.script.parent.mkdir()
        if (ROOT / "scripts/set-release-sha.py").exists():
            shutil.copyfile(ROOT / "scripts/set-release-sha.py", self.script)
        (self.root / "README.md").write_text("install --ref " + TOKEN + "\n")
        (self.root / "docs").mkdir()
        (self.root / "docs/setup.md").write_text("repeat " + TOKEN + " twice " + TOKEN + "\n")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)

    def run_script(self, sha=SYNTHETIC_SHA):
        return subprocess.run([sys.executable, str(self.script), sha],
                              cwd=self.root, text=True, capture_output=True)

    def test_replaces_every_tracked_occurrence_without_touching_local_files(self):
        local = self.root / "local.txt"
        local.write_text(TOKEN)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "README.md").read_text(), "install --ref " + SYNTHETIC_SHA + "\n")
        self.assertEqual((self.root / "docs/setup.md").read_text(), "repeat " + SYNTHETIC_SHA + " twice " + SYNTHETIC_SHA + "\n")
        self.assertEqual(local.read_text(), TOKEN)
        self.assertIn("3 occurrence(s) in 2 file(s)", result.stdout)

    def test_rejects_malformed_or_zero_sha_without_writes(self):
        before = (self.root / "README.md").read_bytes()
        for sha in ("a" * 39, "a" * 41, "g" * 40, " " + SYNTHETIC_SHA,
                    SYNTHETIC_SHA + "\n", "0" * 40, "main", "v0.3.1"):
            with self.subTest(sha=sha):
                result = self.run_script(sha)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("40-character", result.stderr)
                self.assertEqual((self.root / "README.md").read_bytes(), before)

    def test_refuses_symlinks_before_any_replacement(self):
        before = (self.root / "README.md").read_bytes()
        link = self.root / "docs/setup.md"
        link.unlink()
        link.symlink_to(self.root / "README.md")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)
        self.assertEqual((self.root / "README.md").read_bytes(), before)

    def test_refuses_an_already_filled_checkout(self):
        self.assertEqual(self.run_script().returncode, 0)
        result = self.run_script("b" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no release token", result.stderr)
        self.assertIn(SYNTHETIC_SHA, (self.root / "README.md").read_text())

    def test_fails_if_readback_still_contains_a_token(self):
        spec = importlib.util.spec_from_file_location("release_sha_control", self.script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(Path, "write_bytes", return_value=0):
            with self.assertRaisesRegex(ValueError, "remains"):
                module.set_release_sha(self.root, SYNTHETIC_SHA)

    def test_uppercase_hex_is_preserved_exactly(self):
        sha = SYNTHETIC_SHA.upper()
        result = self.run_script(sha)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "README.md").read_text(), "install --ref " + sha + "\n")


if __name__ == "__main__":
    unittest.main()
