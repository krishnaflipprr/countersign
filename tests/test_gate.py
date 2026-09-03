# audited on 20260903
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from countersign.claims import ClaimsError
from countersign.config import Config, ConfigError
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

CLAIMS_READ_REGISTER = """\
[[claim]]
id = "run-already-recorded"
statement = "The register already holds this run's start when claims execute"
command = "python3 -c \\"import sys; sys.exit(0 if 'run_started' in open('.countersign/register.jsonl', encoding='utf-8').read() else 1)\\""
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

LABEL_TS = "export const label = `fake data` + 'until billing lands';\n"


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
        self.assertTrue(receipt["scan"]["tests_excluded"])
        self.assertEqual(receipt["claims"][0]["claim_id"], "proof-works")
        self.assertIn("git_dirty", receipt)

        pack_text = pack_path.read_text(encoding="utf-8")
        self.assertIn(result.run_id, pack_text)
        self.assertIn("What this pack does not cover", pack_text)
        self.assertIn("NOT COUNTERSIGNED", pack_text)
        self.assertIn("Test files were excluded", pack_text)

        summary = markdown_summary(result)
        self.assertIn("NOT COUNTERSIGNED", summary)
        self.assertIn("unfinished-marker", summary)
        terminal = terminal_summary(result, use_color=False)
        self.assertIn("NOT COUNTERSIGNED", terminal)

        register = Register(self.config.register_path())
        intact, _note = register.verify_chain()
        self.assertTrue(intact)
        kinds = [entry["kind"] for entry in register.entries()]
        self.assertEqual(kinds[0], "run_started")
        self.assertIn("finding", kinds)
        self.assertIn("claim", kinds)
        self.assertEqual(kinds[-1], "run_finished")

    def test_clean_tree_passes(self):
        (self.root / "src" / "service.py").write_text(SERVICE_CLEAN, encoding="utf-8")
        result = run_gate(self.config)
        self.assertEqual(result.verdict, PASS_VERDICT)
        self.assertEqual(result.findings, [])
        self.assertIn("COUNTERSIGNED", terminal_summary(result, use_color=False))

    def test_run_start_is_on_disk_before_claims_execute(self):
        (self.root / "claims.toml").write_text(CLAIMS_READ_REGISTER, encoding="utf-8")
        result = run_gate(self.config)
        self.assertEqual(result.claim_results[0].status, "pass", result.claim_results[0].output_excerpt)

    def test_missing_config_file_is_a_config_error(self):
        config = Config.load(self.root / "absent.toml")
        with self.assertRaises(ConfigError):
            run_gate(config)

    def test_bad_claims_file_fails_cleanly_and_writes_nothing(self):
        (self.root / "claims.toml").write_text('[[claim]]\nid = "x"\nstatement = "S"\n', encoding="utf-8")
        with self.assertRaises(ClaimsError):
            run_gate(self.config)
        self.assertFalse(self.config.register_path().exists())

    def test_claims_turned_off_is_reported_as_skipped(self):
        self.config.claims_file = None
        result = run_gate(self.config)
        self.assertEqual(result.claims_status, "skipped")
        self.assertIsNone(result.claim_results)
        self.assertTrue(any("skipped" in note for note in result.notes))
        self.assertIn("skipped", terminal_summary(result, use_color=False))
        self.assertIn("skipped", markdown_summary(result))

    def test_inert_exemption_marker_is_noted_on_the_receipt(self):
        (self.root / "src" / "service.py").write_text("def fine():\n    return 1  # countersign: exempt\n", encoding="utf-8")
        result = run_gate(self.config)
        self.assertEqual(result.exemptions, 0)
        self.assertTrue(any("suppress nothing" in note for note in result.notes), result.notes)

    def test_pack_states_test_exclusion_only_when_it_applies(self):
        self.config.exclude_tests = False
        result = run_gate(self.config)
        self.assertFalse(result.tests_excluded)
        pack_path = build_pack(result, self.config.receipts_root() / f"{result.run_id}.html")
        self.assertNotIn("Test files were excluded", pack_path.read_text(encoding="utf-8"))
        self.assertFalse(receipt_json(result)["scan"]["tests_excluded"])

    def test_markdown_keeps_backticks_inside_code_spans(self):
        (self.root / "src" / "label.ts").write_text(LABEL_TS, encoding="utf-8")
        result = run_gate(self.config)
        self.assertTrue(any(f.path == "src/label.ts" for f in result.findings))
        summary = markdown_summary(result)
        self.assertIn("`` export const label = `fake data`", summary)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_git_state_is_recorded_honestly(self):
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run([*git, "init", "-q"], cwd=self.root, check=True, capture_output=True)
        subprocess.run([*git, "add", "."], cwd=self.root, check=True, capture_output=True)
        subprocess.run([*git, "commit", "-q", "-m", "first"], cwd=self.root, check=True, capture_output=True)
        (self.root / ".gitignore").write_text(".countersign/\n", encoding="utf-8")
        subprocess.run([*git, "add", "."], cwd=self.root, check=True, capture_output=True)
        subprocess.run([*git, "commit", "-q", "-m", "ignore receipts"], cwd=self.root, check=True, capture_output=True)

        result = run_gate(self.config)
        self.assertEqual(len(result.git_commit), 40)
        self.assertFalse(result.git_dirty)
        self.assertNotIn("uncommitted", terminal_summary(result, use_color=False))

        (self.root / "src" / "service.py").write_text(SERVICE_CLEAN, encoding="utf-8")
        result = run_gate(self.config)
        self.assertTrue(result.git_dirty)
        self.assertIn("uncommitted", terminal_summary(result, use_color=False))
        self.assertIn("uncommitted", markdown_summary(result))
        self.assertTrue(receipt_json(result)["git_dirty"])

    def test_outside_a_repository_git_state_is_unknown_not_clean(self):
        result = run_gate(self.config)
        self.assertEqual(result.git_commit, "not a git repository")
        self.assertIsNone(result.git_dirty)

    def test_reproduce_matches_until_the_files_change(self):
        result = run_gate(self.config)
        write_receipt(result, self.config.receipts_root() / f"{result.run_id}.json")
        reproduced, notes = reproduce_run(self.config, result.run_id)
        self.assertTrue(reproduced, msg="; ".join(notes))

        (self.root / "src" / "service.py").write_text(SERVICE_CLEAN, encoding="utf-8")
        reproduced, notes = reproduce_run(self.config, result.run_id)
        self.assertFalse(reproduced)
        self.assertTrue(any("DOES NOT reproduce" in note for note in notes))

    def test_reproduce_notes_a_changed_file_count(self):
        result = run_gate(self.config)
        write_receipt(result, self.config.receipts_root() / f"{result.run_id}.json")
        (self.root / "src" / "extra.py").write_text("x = 1\n", encoding="utf-8")
        reproduced, notes = reproduce_run(self.config, result.run_id)
        self.assertTrue(reproduced, msg="; ".join(notes))
        self.assertTrue(any("1 file(s) then, 2 now" in note for note in notes), notes)

    def test_reproduce_of_a_damaged_receipt_is_a_verdict_not_a_crash(self):
        result = run_gate(self.config)
        receipt_path = write_receipt(result, self.config.receipts_root() / f"{result.run_id}.json")
        receipt_path.write_text("{}", encoding="utf-8")
        reproduced, notes = reproduce_run(self.config, result.run_id)
        self.assertFalse(reproduced)
        self.assertTrue(any("receipt" in note and "cannot" in note for note in notes), notes)
        receipt_path.write_text("not json", encoding="utf-8")
        reproduced, notes = reproduce_run(self.config, result.run_id)
        self.assertFalse(reproduced)

    def test_receipt_json_shape(self):
        result = run_gate(self.config)
        payload = receipt_json(result)
        for key in ("run_id", "verdict", "config", "scan", "findings", "claims", "register", "notes", "git_commit", "git_dirty"):
            self.assertIn(key, payload)


if __name__ == "__main__":
    unittest.main()
