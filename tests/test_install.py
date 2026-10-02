"""Installer tests use isolated destinations, never the user's home directory."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallTest(unittest.TestCase):
    def test_install_and_reinstall_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            env = {**os.environ, "BIN_DIR": str(project / "bin")}
            for _ in range(2):
                result = subprocess.run(["sh", str(ROOT / "install.sh"), "--project", temporary],
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((project / "bin/goodeye").resolve(), ROOT / "goodeye.py")
            self.assertEqual((project / ".claude/skills/goodeye").resolve(), ROOT / "skills/goodeye")
            self.assertEqual((project / ".claude/skills/goodeye-brand").resolve(), ROOT / "skills/goodeye-brand")

    def test_existing_skill_directory_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            skill = project / ".claude/skills/goodeye"
            skill.mkdir(parents=True)
            (skill / "custom.txt").write_text("keep me")
            result = subprocess.run(["sh", str(ROOT / "install.sh"), "--project", temporary],
                                    env={**os.environ, "BIN_DIR": str(project / "bin")},
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((skill / "custom.txt").read_text(), "keep me")
            self.assertFalse((project / "bin/goodeye").exists())

    def test_missing_project_argument_is_reported(self):
        result = subprocess.run(["sh", str(ROOT / "install.sh"), "--project"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--project needs a directory", result.stderr)
