# audited on 20260903
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from countersign.claims import FAIL, PASS, TIMEOUT, Claim, ClaimsError, load_claims, run_claim


class TestRunClaim(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, claim: Claim):
        return run_claim(claim, self.cwd, default_timeout_s=30, max_output_bytes=2000)

    def test_exit_zero_passes(self):
        result = self._run(Claim("ok", "prints", "python3 -c \"print('proof')\""))
        self.assertEqual(result.status, PASS)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("proof", result.output_excerpt)

    def test_nonzero_exit_fails(self):
        result = self._run(Claim("bad", "fails", "python3 -c \"raise SystemExit(3)\""))
        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.exit_code, 3)

    def test_nonzero_expectation_passes_on_failure(self):
        claim = Claim("neg", "this command must fail", "python3 -c \"raise SystemExit(1)\"", expect="nonzero exit")
        result = self._run(claim)
        self.assertEqual(result.status, PASS)

    def test_output_contains(self):
        claim = Claim("needle", "output has the needle", "python3 -c \"print('needle here')\"",
                      expect="output contains", needle="needle here")
        self.assertEqual(self._run(claim).status, PASS)
        missing = Claim("absent", "needle absent", "python3 -c \"print('needle here')\"",
                        expect="output contains", needle="not present")
        self.assertEqual(self._run(missing).status, FAIL)

    def test_timeout_is_reported_not_hidden(self):
        claim = Claim("slow", "sleeps", "python3 -c \"import time; time.sleep(5)\"", timeout_s=1)
        result = self._run(claim)
        self.assertEqual(result.status, TIMEOUT)
        self.assertIsNone(result.exit_code)

    def test_output_written_before_a_timeout_is_kept(self):
        claim = Claim(
            "slow-talker", "prints then hangs",
            "python3 -c \"print('started', flush=True); import time; time.sleep(5)\"",
            timeout_s=1,
        )
        result = self._run(claim)
        self.assertEqual(result.status, TIMEOUT)
        self.assertIn("started", result.output_excerpt)

    def test_undecodable_output_does_not_crash_the_run(self):
        claim = Claim("binary", "prints bytes that are not utf-8",
                      "python3 -c \"import sys; sys.stdout.buffer.write(b'\\\\xff\\\\xfe ok\\\\n')\"")
        result = self._run(claim)
        self.assertEqual(result.status, PASS)
        self.assertIn("ok", result.output_excerpt)

    @unittest.skipIf(os.name == "nt", "process groups are POSIX; Windows uses taskkill /T")
    def test_timeout_kills_the_whole_process_tree(self):
        grandchild = (
            "import subprocess, sys, time; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print(p.pid, flush=True); p.wait()"
        )
        claim = Claim("tree", "spawns a grandchild then hangs", f"{sys.executable} -c \"{grandchild}\"", timeout_s=1)
        result = self._run(claim)
        self.assertEqual(result.status, TIMEOUT)
        pid = int(result.output_excerpt.strip().splitlines()[0])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        os.kill(pid, 9)
        self.fail(f"grandchild {pid} survived the timeout")


class TestLoadClaims(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "claims.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, text: str):
        self.path.write_text(text, encoding="utf-8")

    def test_none_path_means_no_file(self):
        self.assertIsNone(load_claims(None))

    def test_parses_claims(self):
        self._write(
            '[[claim]]\nid = "a"\nstatement = "A"\ncommand = "true"\n\n'
            '[[claim]]\nid = "b"\nstatement = "B"\ncommand = "false"\nexpect = "nonzero exit"\ntimeout_s = 5\n'
        )
        claims = load_claims(self.path)
        self.assertEqual([c.claim_id for c in claims], ["a", "b"])
        self.assertEqual(claims[1].expect, "nonzero exit")
        self.assertEqual(claims[1].timeout_s, 5)

    def test_duplicate_ids_rejected(self):
        self._write(
            '[[claim]]\nid = "a"\nstatement = "A"\ncommand = "true"\n\n'
            '[[claim]]\nid = "a"\nstatement = "A2"\ncommand = "true"\n'
        )
        with self.assertRaises(ClaimsError):
            load_claims(self.path)

    def test_commandless_claim_rejected(self):
        self._write('[[claim]]\nid = "a"\nstatement = "A"\n')
        with self.assertRaises(ClaimsError):
            load_claims(self.path)

    def test_unknown_expectation_rejected(self):
        self._write('[[claim]]\nid = "a"\nstatement = "A"\ncommand = "true"\nexpect = "vibes"\n')
        with self.assertRaises(ClaimsError):
            load_claims(self.path)

    def test_contains_without_needle_rejected(self):
        self._write('[[claim]]\nid = "a"\nstatement = "A"\ncommand = "true"\nexpect = "output contains"\n')
        with self.assertRaises(ClaimsError):
            load_claims(self.path)

    def test_malformed_toml_is_a_claims_error(self):
        self._write('[[claim]\nid = "a"\n')
        with self.assertRaises(ClaimsError):
            load_claims(self.path)

    def test_claim_table_instead_of_array_is_a_claims_error(self):
        self._write('[claim]\nid = "a"\nstatement = "A"\ncommand = "true"\n')
        with self.assertRaises(ClaimsError):
            load_claims(self.path)

    def test_bad_timeout_is_a_claims_error(self):
        self._write('[[claim]]\nid = "a"\nstatement = "A"\ncommand = "true"\ntimeout_s = "soon"\n')
        with self.assertRaises(ClaimsError):
            load_claims(self.path)
        self._write('[[claim]]\nid = "a"\nstatement = "A"\ncommand = "true"\ntimeout_s = 0\n')
        with self.assertRaises(ClaimsError):
            load_claims(self.path)


if __name__ == "__main__":
    unittest.main()
