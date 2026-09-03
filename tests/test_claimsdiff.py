# audited on 20260903
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from countersign.claims import Claim, ClaimsError, parse_claims
from countersign.claimsdiff import ADDED, CHANGED, REMOVED, claims_text_at, diff_against_ref, diff_claims


def _c(claim_id, command="true", expect="exit 0", needle=None, statement="S", timeout_s=None):
    return Claim(claim_id, statement, command, expect, needle, timeout_s)


class TestDiffClaims(unittest.TestCase):
    def test_added_removed_and_unchanged(self):
        changes = diff_claims([_c("a"), _c("b")], [_c("b"), _c("c")])
        self.assertEqual([(x.claim_id, x.kind, x.weakened) for x in changes], [("a", REMOVED, True), ("c", ADDED, False)])

    def test_expectation_and_needle_changes_are_weakening(self):
        changes = diff_claims([_c("a", expect="exit 0")], [_c("a", expect="nonzero exit")])
        self.assertEqual(changes[0].kind, CHANGED)
        self.assertTrue(changes[0].weakened)
        self.assertEqual(changes[0].fields, ("expect",))
        changes = diff_claims([_c("a", expect="output contains", needle="unit_price")], [_c("a", expect="output contains", needle="{")])
        self.assertTrue(changes[0].weakened)

    def test_command_change_is_reported_for_review_not_as_weakening(self):
        changes = diff_claims([_c("a", command="npm test")], [_c("a", command="npm test -- --passWithNoTests")])
        self.assertEqual(changes[0].fields, ("command",))
        self.assertFalse(changes[0].weakened)
        self.assertIn("npm test", changes[0].detail)

    def test_no_base_file_means_everything_added(self):
        changes = diff_claims(None, [_c("a")])
        self.assertEqual([(x.claim_id, x.kind) for x in changes], [("a", ADDED)])


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestDiffAgainstRef(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run([*self.git, "init", "-q", "-b", "main"], cwd=self.root, check=True, capture_output=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _commit(self, message: str) -> None:
        subprocess.run([*self.git, "add", "-A"], cwd=self.root, check=True, capture_output=True)
        subprocess.run([*self.git, "commit", "-q", "-m", message], cwd=self.root, check=True, capture_output=True)

    def test_reads_the_base_and_names_the_weakening(self):
        (self.root / "claims.toml").write_text('[[claim]]\nid = "tests-pass"\nstatement = "Tests pass"\ncommand = "npm test"\n', encoding="utf-8")
        (self.root / "README.md").write_text("x\n", encoding="utf-8")
        self._commit("base")
        (self.root / "claims.toml").write_text('[[claim]]\nid = "tests-pass"\nstatement = "Tests pass"\ncommand = "npm test"\nexpect = "nonzero exit"\n', encoding="utf-8")
        head = parse_claims((self.root / "claims.toml").read_bytes())
        changes, problem = diff_against_ref(self.root, "HEAD", "claims.toml", head)
        self.assertIsNone(problem)
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0].weakened)

    def test_claims_file_in_a_subdirectory(self):
        (self.root / "svc").mkdir()
        (self.root / "svc" / "claims.toml").write_text('[[claim]]\nid = "a"\nstatement = "A"\ncommand = "true"\n', encoding="utf-8")
        self._commit("base")
        (self.root / "svc" / "claims.toml").unlink()
        changes, _problem = diff_against_ref(self.root / "svc", "HEAD", "claims.toml", None)
        self.assertEqual([(x.claim_id, x.kind) for x in changes], [("a", REMOVED)])

    def test_absent_at_base_is_none_but_bad_ref_is_an_error(self):
        (self.root / "README.md").write_text("x\n", encoding="utf-8")
        self._commit("base")
        self.assertIsNone(claims_text_at(self.root, "HEAD", "claims.toml"))
        with self.assertRaises(ClaimsError):
            claims_text_at(self.root, "no-such-ref", "claims.toml")

    def test_unparseable_base_is_reported_not_hidden(self):
        (self.root / "claims.toml").write_text("[[claim]\n", encoding="utf-8")
        self._commit("base")
        (self.root / "claims.toml").write_text('[[claim]]\nid = "a"\nstatement = "A"\ncommand = "true"\n', encoding="utf-8")
        head = parse_claims((self.root / "claims.toml").read_bytes())
        changes, problem = diff_against_ref(self.root, "HEAD", "claims.toml", head)
        self.assertIsNotNone(problem)
        self.assertEqual([x.kind for x in changes], [ADDED])

    def test_outside_a_repository_is_an_error(self):
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaises(ClaimsError):
                claims_text_at(Path(other), "HEAD", "claims.toml")


if __name__ == "__main__":
    unittest.main()
