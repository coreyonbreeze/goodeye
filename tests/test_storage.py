"""Failure-path checks for atomic storage publication."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import goodeye


class StorageTest(unittest.TestCase):
    def test_failed_batch_publication_preserves_previous_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary, "decisions.jsonl")
            original = json.dumps({"decision_id": "old"}) + "\n"
            log.write_text(original)
            with patch.object(goodeye, "DECISIONS", str(log)):
                with patch.object(goodeye.os, "replace", side_effect=OSError("disk unavailable")):
                    with self.assertRaises(OSError):
                        goodeye.append_decisions([{"decision_id": "a"}, {"decision_id": "b"}])
                self.assertEqual(log.read_text(), original)
                self.assertEqual(list(Path(temporary).iterdir()), [log])
                goodeye.append_decisions([{"decision_id": "a"}, {"decision_id": "b"}])
                self.assertEqual([r["decision_id"] for r in goodeye.decisions()], ["old", "a", "b"])

    def test_version_is_invisible_until_complete(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(goodeye, "HOME", temporary):
            version = Path(temporary, "assets", "item", "v1")
            with goodeye.new_version_dir(str(version)) as staging:
                Path(staging, "meta.json").write_text("{}")
                self.assertFalse(version.exists())
            self.assertTrue((version / "meta.json").exists())
            self.assertFalse(Path(staging).exists())

    def test_failed_version_can_be_retried(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(goodeye, "HOME", temporary):
            version = Path(temporary, "assets", "item", "v1")
            with self.assertRaises(OSError):
                with goodeye.new_version_dir(str(version)) as staging:
                    Path(staging, "partial.png").write_bytes(b"partial")
                    raise OSError("copy interrupted")
            self.assertFalse(version.exists())
            self.assertFalse(Path(staging).exists())
            with goodeye.new_version_dir(str(version)):
                pass
            self.assertTrue(version.exists())
