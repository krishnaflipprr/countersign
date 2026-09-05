# audited on 20260905
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from countersign.claims import load_claims
from countersign.cli import main
from countersign.reportclaims import claims_from_report

REPORT = """\
Done! I implemented the pricing feature.

- Created src/pricing.ts with the unitPrice function
- Added tests in test/pricing.test.ts
- All tests pass (12 passing)
- The build succeeds without errors
- Lint is clean
- The endpoint at http://localhost:3000/api/pricing returns the price
- Removed the old file src/legacy-pricing.js
Let me know if you want anything else.
"""


class TestClaimsFromReport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run", "build": "tsc -p .", "lint": "eslint ."}}), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_recognised_sentences_become_claims_with_real_commands(self):
        proposals = claims_from_report(REPORT, self.root)
        by_id = {p.claim.claim_id: p for p in proposals.claims}
        self.assertEqual(by_id["tests-pass"].claim.command, "npm test")
        self.assertEqual(by_id["build-succeeds"].claim.command, "npm run build")
        self.assertEqual(by_id["lint-clean"].claim.command, "npm run lint")
        self.assertEqual(by_id["file-src-pricing-ts"].claim.command, "test -f 'src/pricing.ts'")
        self.assertEqual(by_id["file-test-pricing-test-ts"].claim.command, "test -f 'test/pricing.test.ts'")
        self.assertEqual(by_id["file-gone-src-legacy-pricing-js"].claim.command, "test ! -e 'src/legacy-pricing.js'")
        self.assertEqual(by_id["url-localhost-3000-api-pricing"].claim.command, "curl -sf -o /dev/null 'http://localhost:3000/api/pricing'")
        self.assertEqual(by_id["url-localhost-3000-api-pricing"].claim.expect, "exit 0")
        for proposal in proposals.claims:
            self.assertTrue(proposal.claim.statement.startswith("Agent report: "), proposal.claim.statement)
            self.assertIn(proposal.sentence.strip()[:20], proposal.claim.statement)

    def test_unrecognised_runner_is_reported_not_guessed(self):
        (self.root / "package.json").unlink()
        proposals = claims_from_report("All tests pass now.", self.root)
        self.assertEqual(proposals.claims, [])
        self.assertEqual(len(proposals.unresolved), 1)
        self.assertIn("no test runner", proposals.unresolved[0].reason)

    def test_sentences_with_nothing_checkable_are_ignored(self):
        proposals = claims_from_report("I refactored the helpers and improved naming.", self.root)
        self.assertEqual(proposals.claims, [])
        self.assertEqual(proposals.unresolved, [])

    def test_duplicate_mentions_collapse_to_one_claim(self):
        proposals = claims_from_report("Tests pass. All tests are passing. Created src/a.ts. I created src/a.ts again.", self.root)
        ids = [p.claim.claim_id for p in proposals.claims]
        self.assertEqual(ids, ["tests-pass", "file-src-a-ts"])

    def _cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["claims", "from-report", *args])
        return code, out.getvalue()

    def test_cli_prints_toml_and_writes_only_when_asked(self):
        report = self.root / "report.md"
        report.write_text(REPORT, encoding="utf-8")
        config = self.root / "countersign.toml"
        config.write_text('[claims]\nfile = "claims.toml"\n', encoding="utf-8")
        code, output = self._cli(str(report), "--config", str(config))
        self.assertEqual(code, 0, output)
        self.assertIn('[[claim]]', output)
        self.assertIn('id = "tests-pass"', output)
        self.assertFalse((self.root / "claims.toml").exists())

        code, output = self._cli(str(report), "--config", str(config), "--write")
        self.assertEqual(code, 0, output)
        claims = {c.claim_id: c for c in load_claims(self.root / "claims.toml")}
        self.assertIn("tests-pass", claims)
        self.assertIn("url-localhost-3000-api-pricing", claims)

        code, output = self._cli(str(report), "--config", str(config), "--write")
        self.assertEqual(code, 0, output)
        self.assertIn("already declared", output)
        self.assertEqual(len(load_claims(self.root / "claims.toml")), len(claims))

    def test_cli_reads_stdin_and_exits_1_when_nothing_is_checkable(self):
        config = self.root / "countersign.toml"
        config.write_text("", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), mock.patch("sys.stdin", io.StringIO("I tidied up the imports.")):
            code = main(["claims", "from-report", "-", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertIn("nothing checkable", out.getvalue())


if __name__ == "__main__":
    unittest.main()
