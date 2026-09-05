# audited on 20260905
import unittest

from countersign.claims import ClaimResult
from countersign.claimsdiff import ClaimChange
from countersign.engine import GateResult
from countersign.plain import plain_sentences
from countersign.stubscan import Finding


def _result(**overrides) -> GateResult:
    base = dict(
        run_id="20260905T000000-abcd1234", recorded_at="2026-09-05T00:00:00+00:00", verdict="pass",
        config_path="countersign.toml", config_sha256="a" * 64, claims_sha256="b" * 64,
        git_commit="c" * 40, files_scanned=12, claim_results=[], claims_status="ran",
    )
    base.update(overrides)
    return GateResult(**base)


class TestPlainSentences(unittest.TestCase):
    def test_clean_pass_says_what_held(self):
        result = _result(claim_results=[ClaimResult("tests-pass", "The full test suite passes", "npm test", "exit 0", "pass", 0, 900)])
        text = " ".join(plain_sentences(result))
        self.assertIn("Countersigned", text)
        self.assertIn("12 files", text)
        self.assertIn("1 claim", text)
        self.assertIn("The full test suite passes", text)
        self.assertNotIn("Not countersigned", text)

    def test_findings_are_explained_by_kind_with_location(self):
        result = _result(verdict="fail", findings=[
            Finding("src/page.tsx", 41, "unfinished-marker", "why", "// TODO: wire this"),
            Finding("src/pricing.ts", 12, "fabricated-data", "why", "return 49.99; // fake data"),
            Finding("src/notify.py", 8, "empty-body", "why", "def send_invoice():"),
        ])
        sentences = plain_sentences(result)
        text = " ".join(sentences)
        self.assertIn("Not countersigned", text)
        self.assertIn("3 places", text)
        self.assertIn("src/page.tsx line 41", text)
        self.assertIn("note left for later", text)
        self.assertIn("made-up data", text)
        self.assertIn("function that does nothing", text)

    def test_failed_claim_quotes_the_agent_statement_and_the_last_output_line(self):
        result = _result(verdict="fail", claim_results=[
            ClaimResult("tests-pass", "The full test suite passes", "npm test", "exit 0", "fail", 1, 5000, "...\n2 failing\n"),
        ])
        text = " ".join(plain_sentences(result))
        self.assertIn("'The full test suite passes' did not hold", text)
        self.assertIn("exit code 1", text)
        self.assertIn("2 failing", text)

    def test_timeout_missing_and_skipped_are_named(self):
        result = _result(verdict="fail", claim_results=[
            ClaimResult("slow", "The migration completes", "make migrate", "exit 0", "timeout", None, 300000),
            ClaimResult("tests-pass", "required by countersign.toml but not declared in the claims file", "", "exit 0", "missing"),
        ])
        text = " ".join(plain_sentences(result))
        self.assertIn("stopped after 300 seconds", text)
        self.assertIn("required", text)
        self.assertIn("never declared", text)
        skipped = " ".join(plain_sentences(_result(claim_results=None, claims_status="skipped")))
        self.assertIn("No claims were checked", skipped)
        self.assertIn("not a pass", skipped)

    def test_weakened_claims_are_spelled_out(self):
        result = _result(verdict="fail", claims_base="origin/main", claims_diff=[
            ClaimChange("tests-pass", "changed", ("expect",), True, "expect: 'exit 0' to 'nonzero exit'"),
            ClaimChange("lint-clean", "removed", (), True, "removed: The linter reports nothing"),
            ClaimChange("types-check", "added", (), False, "added: Types check"),
        ])
        text = " ".join(plain_sentences(result))
        self.assertIn("compared with origin/main", text)
        self.assertIn("'tests-pass' now expects the opposite outcome", text)
        self.assertIn("'lint-clean' was removed", text)
        self.assertNotIn("types-check", text)

    def test_dirty_tree_and_no_ai_are_stated(self):
        text = " ".join(plain_sentences(_result(git_dirty=True)))
        self.assertIn("uncommitted changes", text)
        self.assertIn("No AI judged anything", text)

    def test_every_sentence_ends_with_a_full_stop_and_has_no_em_dash(self):
        result = _result(verdict="fail", git_dirty=True, findings=[Finding("a.py", 1, "stub-word", "why", "x")],
                         claim_results=[ClaimResult("c", "S", "cmd", "exit 0", "fail", 2, 10, "")])
        for sentence in plain_sentences(result):
            self.assertTrue(sentence.endswith("."), sentence)
            self.assertNotIn("—", sentence)


if __name__ == "__main__":
    unittest.main()
