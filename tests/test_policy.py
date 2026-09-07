# audited on 20260903
"""The security boundary: a pull request cannot rewrite the gate it is judged by.

Each test here is a way a pull request could pass while checking less
than the branch it targets, or a way an approval could be stretched
further than it should reach.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from countersign.claims import redact_secrets
from countersign.config import Config, ConfigError
from countersign.engine import FAIL_VERDICT, PASS_VERDICT, run_gate
from countersign.policydiff import diff_policy
from countersign.receipt import receipt_json

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]

BASE_CONFIG = """\
[scan]
paths = ["src"]

[claims]
file = "claims.toml"
required = ["tests-pass"]
"""

BASE_CLAIMS = """\
[[claim]]
id = "tests-pass"
statement = "The suite passes"
command = "python3 -c 'print(1)'"
"""

CLEAN_SOURCE = "def price(cents: int) -> int:\n    return cents * 2\n"


class PolicyBoundary(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src").mkdir()
        self._write("countersign.toml", BASE_CONFIG)
        self._write("claims.toml", BASE_CLAIMS)
        self._write("src/app.py", CLEAN_SOURCE)
        self._write(".gitignore", ".countersign/\n")
        self._git("init", "-q", "-b", "main")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "base")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text, encoding="utf-8")

    def _git(self, *args: str) -> None:
        subprocess.run([*GIT, *args], cwd=self.root, check=True, capture_output=True)

    def _config(self) -> Config:
        return Config.load(self.root / "countersign.toml")

    def _weakened_policy_fields(self, result) -> set[str]:
        return {c.field for c in (result.policy_diff or []) if c.weakened}

    def test_baseline_passes(self) -> None:
        result = run_gate(self._config(), claims_base="main")
        self.assertEqual(result.verdict, PASS_VERDICT)
        self.assertEqual(result.policy_diff, [])
        receipt = receipt_json(result)
        self.assertEqual(receipt["policy"]["base_ref"], "main")
        self.assertEqual(receipt["policy"]["base_policy_sha256"], receipt["policy"]["policy_sha256"])
        self.assertIsNotNone(receipt["policy"]["base_config_sha256"])
        self.assertIsNotNone(receipt["policy"]["base_claims_sha256"])
        self.assertEqual(receipt["approval"], {"approved": False, "used": False})

    def test_a_pull_request_cannot_empty_the_scan(self) -> None:
        for config in ('[scan]\npaths = []\n[claims]\nfile = "claims.toml"\n', '[scan]\npaths = ["src"]\nextensions = []\n[claims]\nfile = "claims.toml"\n'):
            with self.subTest(config=config):
                self._write("countersign.toml", config)
                # Without a base the empty scope is refused outright.
                with self.assertRaises(ConfigError):
                    run_gate(self._config())
                # With a base the base policy is enforced and the change is a weakening.
                result = run_gate(self._config(), claims_base="main")
                self.assertEqual(result.verdict, FAIL_VERDICT)
                self.assertTrue(self._weakened_policy_fields(result) & {"paths", "extensions"})
                self.assertEqual(result.files_scanned, 1, "the base policy scanned src as before")

    def test_a_pull_request_cannot_disable_claims_or_drop_a_required_one(self) -> None:
        self._write("countersign.toml", '[scan]\npaths = ["src"]\n[claims]\nfile = ""\n')
        result = run_gate(self._config(), claims_base="main")
        self.assertEqual(result.verdict, FAIL_VERDICT)
        self.assertIn("claims_file", self._weakened_policy_fields(result))
        self.assertEqual(result.claims_status, "ran", "the base policy still ran the claims")

        self._write("countersign.toml", '[scan]\npaths = ["src"]\n[claims]\nfile = "claims.toml"\nrequired = []\n')
        result = run_gate(self._config(), claims_base="main")
        self.assertEqual(result.verdict, FAIL_VERDICT)
        self.assertIn("required_claims", self._weakened_policy_fields(result))

        self._write("countersign.toml", BASE_CONFIG)
        self._write("claims.toml", "")
        result = run_gate(self._config(), claims_base="main")
        self.assertEqual(result.verdict, FAIL_VERDICT)
        self.assertTrue(any(c.status == "missing" for c in result.claim_results or []))

    def test_a_pull_request_cannot_turn_off_fail_on_weakened(self) -> None:
        self._write("countersign.toml", BASE_CONFIG + "fail_on_weakened = false\n")
        self._write("claims.toml", BASE_CLAIMS.replace("python3 -c 'print(1)'", "true"))
        result = run_gate(self._config(), claims_base="main")
        self.assertEqual(result.verdict, FAIL_VERDICT)
        self.assertIn("fail_on_weakened", self._weakened_policy_fields(result))
        self.assertEqual(len(result.weakened_claims), 1)

    def test_a_changed_command_fails_and_the_label_records_it(self) -> None:
        self._write("claims.toml", BASE_CLAIMS.replace("python3 -c 'print(1)'", "python3 -c 'print(1)' || true"))
        result = run_gate(self._config(), claims_base="main")
        self.assertEqual(result.verdict, FAIL_VERDICT)
        self.assertEqual([c.claim_id for c in result.weakened_claims], ["tests-pass"])

        approved = run_gate(self._config(), claims_base="main", approved=True)
        self.assertEqual(approved.verdict, PASS_VERDICT)
        self.assertTrue(approved.approval_used)
        receipt = receipt_json(approved)
        self.assertEqual(receipt["approval"], {"approved": True, "used": True})
        self.assertTrue(any("maintainer approved" in s for s in receipt["plain"]), receipt["plain"])

    def test_the_label_never_rescues_a_run_that_proved_nothing(self) -> None:
        self._write("countersign.toml", '[scan]\npaths = ["src"]\nextensions = [".nothing"]\n[claims]\nfile = "claims.toml"\n')
        self._git("add", "countersign.toml")
        self._git("commit", "-q", "-m", "an empty base policy")
        with self.assertRaises(ConfigError):
            run_gate(self._config(), claims_base="main", approved=True)

        self._write("countersign.toml", '[scan]\npaths = ["src"]\n[claims]\nfile = "missing.toml"\n')
        self._git("add", "countersign.toml")
        self._git("commit", "-q", "-m", "a base policy whose claims file is absent")
        result = run_gate(self._config(), claims_base="main", approved=True)
        self.assertEqual(result.verdict, FAIL_VERDICT)
        self.assertEqual(result.claims_status, "absent")

    def test_one_pull_request_changing_policy_claims_and_code_together(self) -> None:
        self._write("countersign.toml", '[scan]\npaths = ["src"]\nignore_dirs = ["src/legacy"]\n[claims]\nfile = "claims.toml"\nrequired = ["tests-pass"]\n')
        self._write("claims.toml", BASE_CLAIMS.replace('expect = "exit 0"', "").replace("python3 -c 'print(1)'", "python3 -c 'print(2)'"))
        self._write("src/app.py", "def price(cents: int) -> int:\n    return 4999  # fake data until billing lands\n")
        result = run_gate(self._config(), claims_base="main")
        self.assertEqual(result.verdict, FAIL_VERDICT)
        self.assertEqual(len(result.findings), 1)
        self.assertIn("ignore_dirs", self._weakened_policy_fields(result))
        self.assertEqual([c.claim_id for c in result.weakened_claims], ["tests-pass"])
        receipt = receipt_json(result)
        self.assertEqual(receipt["policy"]["weakened"], 1)
        self.assertEqual(receipt["claims_diff"]["weakened"], 1)
        # Approval covers the policy and the claim; it cannot cover the finding.
        approved = run_gate(self._config(), claims_base="main", approved=True)
        self.assertEqual(approved.verdict, FAIL_VERDICT)
        self.assertEqual(len(approved.findings), 1)
        self.assertTrue(approved.approval_used)

    def test_receipts_do_not_expose_a_secret_printed_by_a_claim(self) -> None:
        token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        # The secret comes from the environment, the way a real workflow would leak one; a literal secret in claims.toml is already committed and is the author's own doing.
        self._write("claims.toml", BASE_CLAIMS.replace("python3 -c 'print(1)'", "python3 -c 'import os, sys; print(sys.argv[2] + os.environ[sys.argv[1]])' COUNTERSIGN_TEST_TOKEN token="))
        os.environ["COUNTERSIGN_TEST_TOKEN"] = token
        try:
            result = run_gate(self._config())
        finally:
            del os.environ["COUNTERSIGN_TEST_TOKEN"]
        receipt = receipt_json(result)
        text = str(receipt)
        self.assertNotIn(token, text)
        self.assertIn("[redacted]", text)
        self.assertEqual(receipt["claims"][0]["redactions"], 1)
        self.assertTrue(any("credential-shaped" in note for note in result.notes))

    def test_output_none_keeps_nothing(self) -> None:
        self._write("countersign.toml", BASE_CONFIG + '\n[run]\noutput = "none"\n')
        result = run_gate(self._config())
        self.assertEqual(result.claim_results[0].output_excerpt, "")

    def test_unknown_config_keys_are_refused(self) -> None:
        self._write("countersign.toml", '[scan]\npaths = ["src"]\n[claims]\nrequird = ["tests-pass"]\n')
        with self.assertRaises(ConfigError) as caught:
            self._config()
        self.assertIn("did you mean 'required'?", str(caught.exception))
        self._write("countersign.toml", '[scann]\npaths = ["src"]\n')
        with self.assertRaises(ConfigError):
            self._config()


class PolicyDiff(unittest.TestCase):
    def _cfg(self, text: str) -> Config:
        return Config.from_bytes(text.encode(), root=Path("."), config_path=Path("countersign.toml"))

    def test_widening_is_not_a_weakening(self) -> None:
        changes = diff_policy(self._cfg('[scan]\npaths = ["src"]\n'), self._cfg('[scan]\npaths = ["src", "lib"]\n[claims]\nrequired = ["a"]\n'))
        self.assertEqual([(c.field, c.weakened) for c in changes], [("paths", False), ("required_claims", False)])

    def test_every_narrowing_is_a_weakening(self) -> None:
        base = self._cfg('[scan]\npaths = ["src", "lib"]\nextensions = [".py", ".ts"]\n[claims]\nrequired = ["a"]\n')
        head = self._cfg('[scan]\npaths = ["src"]\nextensions = [".py"]\nignore_dirs = ["src/x"]\nexempt_marker = "x"\nallow_empty = true\n[claims]\nfile = ""\nrequired = []\nfail_on_weakened = false\noptional = true\ncommand_change = "note"\n[receipts]\ndir = "elsewhere"\n')
        weakened = {c.field for c in diff_policy(base, head) if c.weakened}
        self.assertEqual(weakened, {"paths", "extensions", "ignore_dirs", "exempt_marker", "allow_empty", "claims_file", "required_claims", "fail_on_weakened", "claims_optional", "command_change", "receipt_dir"})

    def test_redaction_patterns(self) -> None:
        text, count = redact_secrets("Authorization: Bearer abc.def.ghi AKIAABCDEFGHIJKLMNOP api_key=0123456789abcdef -----BEGIN RSA PRIVATE KEY-----\nzzz\n-----END RSA PRIVATE KEY-----")
        self.assertEqual(count, 4)
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", text)
        self.assertNotIn("zzz", text)
        untouched, none = redact_secrets("2 passed in 0.31s")
        self.assertEqual((untouched, none), ("2 passed in 0.31s", 0))


if __name__ == "__main__":
    unittest.main()


class ClaimInputs(unittest.TestCase):
    """A command can stay word for word the same while the file it runs is
    rewritten. Declared inputs make that a weakening too."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text(CLEAN_SOURCE, encoding="utf-8")
        (self.root / "countersign.toml").write_text('[scan]\npaths = ["src"]\n[claims]\nfile = "claims.toml"\n', encoding="utf-8")
        (self.root / "package.json").write_text('{"scripts": {"test": "python3 -c \'print(1)\'"}}', encoding="utf-8")
        (self.root / "claims.toml").write_text('[[claim]]\nid = "tests-pass"\nstatement = "The suite passes"\ncommand = "python3 -c \'print(1)\'"\ninputs = ["package.json"]\n', encoding="utf-8")
        (self.root / ".gitignore").write_text(".countersign/\n", encoding="utf-8")
        for args in (("init", "-q", "-b", "main"), ("add", "."), ("commit", "-q", "-m", "base")):
            subprocess.run([*GIT, *args], cwd=self.root, check=True, capture_output=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_inputs_are_parsed_and_validated(self) -> None:
        from countersign.claims import ClaimsError, parse_claims
        claims = parse_claims('[[claim]]\nid = "a"\nstatement = "s"\ncommand = "true"\ninputs = ["package.json", "config/"]\n')
        self.assertEqual(claims[0].inputs, ("package.json", "config/"))
        for bad in ('inputs = "package.json"', 'inputs = ["../secrets"]', 'inputs = ["/etc/passwd"]', 'inputs = [""]'):
            with self.subTest(bad=bad):
                with self.assertRaises(ClaimsError):
                    parse_claims(f'[[claim]]\nid = "a"\nstatement = "s"\ncommand = "true"\n{bad}\n')

    def test_a_rewritten_input_is_a_weakening_with_the_command_unchanged(self) -> None:
        config = Config.load(self.root / "countersign.toml")
        clean = run_gate(config, claims_base="main")
        self.assertEqual(clean.verdict, PASS_VERDICT)
        self.assertEqual(clean.claims_diff, [])
        self.assertEqual(len(clean.claim_results[0].inputs["package.json"]), 64)

        (self.root / "package.json").write_text('{"scripts": {"test": "echo tests pass"}}', encoding="utf-8")
        result = run_gate(config, claims_base="main")
        self.assertEqual(result.verdict, FAIL_VERDICT)
        self.assertEqual([(c.claim_id, c.fields, c.weakened) for c in result.claims_diff], [("tests-pass", ("inputs",), True)])
        self.assertIn("package.json", result.claims_diff[0].detail)
        receipt = receipt_json(result)
        self.assertTrue(any("a file it depends on changed" in s for s in receipt["plain"]), receipt["plain"])
        self.assertTrue(any("countersign-approved label" in s for s in receipt["plain"]), "the comment tells the maintainer what to do")
        self.assertFalse(any("countersign-approved label" in s for s in receipt_json(run_gate(config, claims_base="main", approved=True))["plain"]))
        self.assertEqual(receipt["claims"][0]["inputs"]["package.json"], result.claim_results[0].inputs["package.json"])

        approved = run_gate(config, claims_base="main", approved=True)
        self.assertEqual(approved.verdict, PASS_VERDICT)
        self.assertTrue(approved.approval_used)

    def test_dropping_an_input_is_a_weakening_and_adding_one_is_not(self) -> None:
        config = Config.load(self.root / "countersign.toml")
        (self.root / "claims.toml").write_text('[[claim]]\nid = "tests-pass"\nstatement = "The suite passes"\ncommand = "python3 -c \'print(1)\'"\n', encoding="utf-8")
        dropped = run_gate(config, claims_base="main")
        self.assertEqual(dropped.verdict, FAIL_VERDICT)
        self.assertTrue(dropped.claims_diff[0].weakened)
        (self.root / "claims.toml").write_text('[[claim]]\nid = "tests-pass"\nstatement = "The suite passes"\ncommand = "python3 -c \'print(1)\'"\ninputs = ["package.json", "src"]\n', encoding="utf-8")
        widened = run_gate(config, claims_base="main")
        self.assertEqual(widened.verdict, PASS_VERDICT)
        self.assertFalse(widened.claims_diff[0].weakened)

    def test_directory_inputs_fingerprint_their_files_on_both_sides(self) -> None:
        config = Config.load(self.root / "countersign.toml")
        (self.root / "claims.toml").write_text('[[claim]]\nid = "tests-pass"\nstatement = "The suite passes"\ncommand = "python3 -c \'print(1)\'"\ninputs = ["package.json", "src"]\n', encoding="utf-8")
        subprocess.run([*GIT, "commit", "-q", "-am", "declare src as an input"], cwd=self.root, check=True, capture_output=True)
        same = run_gate(config, claims_base="main")
        self.assertEqual(same.claims_diff, [], "the working tree equals the commit, so the directory fingerprints agree")
        (self.root / "src" / "app.py").write_text(CLEAN_SOURCE + "\n", encoding="utf-8")
        changed = run_gate(config, claims_base="main")
        self.assertTrue(changed.claims_diff and changed.claims_diff[0].weakened)

    def test_a_process_left_behind_by_a_claim_does_not_survive_it(self) -> None:
        from countersign.claims import parse_claims, run_claim
        marker = self.root / "pid"
        claim = parse_claims(f'[[claim]]\nid = "leaves"\nstatement = "s"\ncommand = "python3 -c \'import subprocess, sys; p = subprocess.Popen([sys.executable, \\"-c\\", \\"import time; time.sleep(60)\\"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); open(\\"{marker.as_posix()}\\", \\"w\\").write(str(p.pid))\'"\n')[0]
        result = run_claim(claim, self.root, 30, 1000)
        self.assertEqual(result.status, "pass")
        pid = int(marker.read_text())
        import time
        alive = True
        for _ in range(20):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.1)
        if alive:
            os.kill(pid, 9)
        self.assertFalse(alive, "the child the claim started should have died with the claim's process group")

    def test_starter_claims_declare_their_inputs(self) -> None:
        from countersign.starter import detect_starter_claims, render_claims_toml
        from countersign.claims import parse_claims
        (self.root / "tsconfig.json").write_text("{}", encoding="utf-8")
        (self.root / "package.json").write_text('{"scripts": {"test": "vitest run", "lint": "eslint ."}, "devDependencies": {"typescript": "5"}}', encoding="utf-8")
        starters = detect_starter_claims(self.root)
        by_id = {s.claim_id: s.inputs for s in starters}
        self.assertEqual(by_id["tests-pass"], ("package.json",))
        self.assertEqual(by_id["types-check"], ("package.json", "tsconfig.json"))
        parsed = parse_claims(render_claims_toml(starters))
        self.assertEqual(parsed[0].inputs, ("package.json",))
