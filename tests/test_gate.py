import tempfile
import unittest
from pathlib import Path

from countersign.config import Config
from countersign.engine import FAIL_VERDICT, PASS_VERDICT, run_gate
from countersign.pack import build_pack
from countersign.receipt import load_receipt, markdown_summary, receipt_json, terminal_summary, write_receipt
from countersign.register import Register
from countersign.reproduce import reproduce_run

CONFIG_TOML = """\
[scan]
paths = ["src"]

[claims]
file = "claims.toml"

[receipts]
dir = ".countersign"

[run]
timeout_s = 60
max_output_bytes = 2000
"""

CLAIMS_TOML = """\
[[claim]]
id = "proof-works"
statement = "The proof command runs"
command = "python3 -c \\"print('counterproof')\\""
expect = "exit 0"
"""

SERVICE_WITH_MARKER = '''def get_feed():
    # TODO: connect the real feed
    return {"items": []}
'''

SERVICE_CLEAN = """def get_feed():
    return {"items": fetch_items()}


def fetch_items():
    return [{"title": "one"}]
"""


class TestGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src").mkdir()
        (self.root / "countersign.toml").write_text(CONFIG_TOML, encoding="utf-8")
        (self.root / "claims.toml").write_text(CLAIMS_TOML, encoding="utf-8")
        (self.root / "src" / "service.py").write_text(SERVICE_WITH_MARKER, encoding="utf-8")
        self.config = Config.load(self.root / "countersign.toml")

    def tearDown(self):
        self._tmp.cleanup()

    def test_finding_fails_the_gate_and_everything_lands_on_disk(self):
        result = run_gate(self.config)
        self.assertEqual(result.verdict, FAIL_VERDICT)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].rule_id, "unfinished-marker")
        self.assertEqual(result.claims_status, "ran")
        self.assertEqual(result.claim_results[0].status, "pass")

        receipt_path = write_receipt(result, self.config.receipts_root() / f"{result.run_id}.json")
        pack_path = build_pack(result, self.config.receipts_root() / f"{result.run_id}.html")
        self.assertTrue(receipt_path.exists())
        self.assertTrue(pack_path.exists())

        receipt = load_receipt(receipt_path)
        self.assertEqual(receipt["verdict"], "fail")
        self.assertEqual(receipt["scan"]["findings"], 1)
        self.assertEqual(receipt["claims"][0]["claim_id"], "proof-works")

        pack_text = pack_path.read_text(encoding="utf-8")
        self.assertIn(result.run_id, pack_text)
        self.assertIn("What this pack does not cover", pack_text)
        self.assertIn("NOT COUNTERSIGNED", pack_text)

        summary = markdown_summary(result)
        self.assertIn("NOT COUNTERSIGNED", summary)
        self.assertIn("unfinished-marker", summary)
        terminal = terminal_summary(result, use_color=False)
        self.assertIn("NOT COUNTERSIGNED", terminal)

        register = Register(self.config.register_path())
        intact, _note = register.verify_chain()
        self.assertTrue(intact)
        kinds = [entry["kind"] for entry in register.entries()]
        self.assertIn("run_started", kinds)
        self.assertIn("finding", kinds)
        self.assertIn("claim", kinds)
        self.assertIn("run_finished", kinds)

    def test_clean_tree_passes(self):
        (self.root / "src" / "service.py").write_text(SERVICE_CLEAN, encoding="utf-8")
        result = run_gate(self.config)
        self.assertEqual(result.verdict, PASS_VERDICT)
        self.assertEqual(result.findings, [])
        self.assertIn("COUNTERSIGNED", terminal_summary(result, use_color=False))

    def test_reproduce_matches_until_the_files_change(self):
        result = run_gate(self.config)
        write_receipt(result, self.config.receipts_root() / f"{result.run_id}.json")
        reproduced, notes = reproduce_run(self.config, result.run_id)
        self.assertTrue(reproduced, msg="; ".join(notes))

        (self.root / "src" / "service.py").write_text(SERVICE_CLEAN, encoding="utf-8")
        reproduced, notes = reproduce_run(self.config, result.run_id)
        self.assertFalse(reproduced)
        self.assertTrue(any("DOES NOT reproduce" in note for note in notes))

    def test_bad_claims_file_fails_cleanly(self):
        (self.root / "claims.toml").write_text('[[claim]]\nid = "x"\nstatement = "S"\n', encoding="utf-8")
        with self.assertRaises(Exception):
            run_gate(self.config)

    def test_receipt_json_shape(self):
        result = run_gate(self.config)
        payload = receipt_json(result)
        for key in ("run_id", "verdict", "config", "scan", "findings", "claims", "register", "notes"):
            self.assertIn(key, payload)


if __name__ == "__main__":
    unittest.main()
