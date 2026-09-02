import json
import tempfile
import unittest
from pathlib import Path

from countersign.register import GENESIS, Register, RegisterDamaged, entry_hash


class TestRegister(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "register.jsonl"
        self.register = Register(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_register_is_intact(self):
        intact, note = self.register.verify_chain()
        self.assertTrue(intact)
        self.assertEqual(note, "0 entries, chain intact")

    def test_append_chains_entries(self):
        first = self.register.append("run_started", {"run_id": "a"})
        second = self.register.append("finding", {"run_id": "a", "path": "x.py"})
        self.assertEqual(first["index"], 0)
        self.assertEqual(first["previous_hash"], GENESIS)
        self.assertEqual(second["index"], 1)
        self.assertEqual(second["previous_hash"], first["hash"])
        self.assertEqual(
            second["hash"],
            entry_hash(first["hash"], {"index": 1, "kind": "finding", "recorded_at": second["recorded_at"], "body": second["body"]}),
        )
        intact, note = self.register.verify_chain()
        self.assertTrue(intact)
        self.assertIn("2 entries", note)

    def test_edited_entry_breaks_chain(self):
        self.register.append("run_started", {"run_id": "a"})
        self.register.append("finding", {"run_id": "a", "path": "x.py", "line": 1})
        lines = self.path.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["body"]["line"] = 900
        lines[1] = json.dumps(entry, sort_keys=True)
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        intact, note = self.register.verify_chain()
        self.assertFalse(intact)
        self.assertIn("altered", note)

    def test_deleted_entry_breaks_chain(self):
        self.register.append("run_started", {"run_id": "a"})
        self.register.append("finding", {"run_id": "a", "path": "x.py"})
        self.register.append("run_finished", {"run_id": "a", "verdict": "fail"})
        lines = self.path.read_text(encoding="utf-8").splitlines()
        del lines[1]
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        intact, note = self.register.verify_chain()
        self.assertFalse(intact)

    def test_corrupt_line_is_a_verdict_not_a_crash(self):
        self.register.append("run_started", {"run_id": "a"})
        self.path.write_text("this is not json\n", encoding="utf-8")
        intact, note = self.register.verify_chain()
        self.assertFalse(intact)
        self.assertIn("cannot be read", note)
        with self.assertRaises(RegisterDamaged):
            self.register.head()


if __name__ == "__main__":
    unittest.main()
