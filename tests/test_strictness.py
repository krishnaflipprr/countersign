# audited on 20260903
"""The three ways a gate can pass while checking nothing.

Each test here stands for a way Countersign could report a pass that the
work did not earn. They are grouped because they share one principle: a
claim that cannot fail is not a claim, and the tool must say so rather
than quietly count it as satisfied.
"""

from __future__ import annotations

import unittest

from countersign.claims import ClaimsError, parse_claims
from countersign.claimsdiff import diff_claims, is_no_op_command


class UnknownKeysAreRefused(unittest.TestCase):
    """A misspelled key must not fall back to the default judgement.

    `expct = "output contains"` parses as valid TOML and leaves the claim
    with expect = "exit 0". The command then only has to run, not to
    contain anything, and the receipt records a pass for a check nobody
    is performing.
    """

    def test_misspelled_expect_is_refused(self) -> None:
        text = """
        [[claim]]
        id = "pricing"
        statement = "Pricing returns real numbers"
        command = "echo seed_data"
        expct = "output contains"
        neddle = "unit_price"
        """
        with self.assertRaises(ClaimsError) as caught:
            parse_claims(text)
        message = str(caught.exception)
        self.assertIn("expct", message)
        self.assertIn("neddle", message)
        self.assertIn("did you mean 'expect'?", message)
        self.assertIn("did you mean 'needle'?", message)

    def test_timeout_without_the_suffix_is_refused(self) -> None:
        text = """
        [[claim]]
        id = "slow"
        statement = "Finishes quickly"
        command = "sleep 300"
        timeout = 3
        """
        with self.assertRaises(ClaimsError) as caught:
            parse_claims(text)
        self.assertIn("did you mean 'timeout_s'?", str(caught.exception))

    def test_unknown_top_level_key_is_refused(self) -> None:
        with self.assertRaises(ClaimsError):
            parse_claims('[[claims]]\nid = "x"\n')

    def test_every_known_key_still_parses(self) -> None:
        text = """
        [[claim]]
        id = "ok"
        statement = "It holds"
        command = "npm test"
        expect = "output contains"
        needle = "0 failing"
        timeout_s = 60
        """
        claims = parse_claims(text)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].timeout_s, 60)
        self.assertEqual(claims[0].needle, "0 failing")


class NoOpCommandDetection(unittest.TestCase):
    """Only commands that provably always succeed count as no-ops."""

    def test_commands_that_cannot_fail(self) -> None:
        for command in ("true", " true ", ":", "exit 0", "/bin/true", "echo ok", "true;"):
            with self.subTest(command=command):
                self.assertTrue(is_no_op_command(command))

    def test_real_commands_are_left_alone(self) -> None:
        for command in (
            "npm test",
            "pytest -q",
            "echo x && npm test",
            "echo $(npm test)",
            "echo hi > /tmp/a",
            "true; npm test",
            "grep -q unit_price out.txt",
            "TRUE",
        ):
            with self.subTest(command=command):
                self.assertFalse(is_no_op_command(command))


class NeuteredCommandIsAWeakening(unittest.TestCase):
    """Swapping the command for one that cannot fail must fail the gate.

    Deleting a claim is already caught as a removal, so the cheaper move is
    to keep the claim and point it at `true`. The claim then survives every
    review that reads the diff for missing ids.
    """

    def _claims(self, command: str) -> list:
        return parse_claims(
            f'[[claim]]\nid = "tests-pass"\nstatement = "The suite passes"\ncommand = "{command}"\n'
        )

    def test_real_command_to_no_op_is_weakened(self) -> None:
        changes = diff_claims(self._claims("npm test"), self._claims("true"))
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0].weakened)
        self.assertIn("always succeeds", changes[0].detail)

    def test_no_op_to_real_command_is_not_weakened_under_the_note_policy(self) -> None:
        changes = diff_claims(self._claims("true"), self._claims("npm test"), command_change_weakens=False)
        self.assertEqual(len(changes), 1)
        self.assertFalse(changes[0].weakened)

    def test_one_real_command_to_another_is_a_note_only_under_the_note_policy(self) -> None:
        changes = diff_claims(self._claims("npm test"), self._claims("pytest -q"), command_change_weakens=False)
        self.assertEqual(len(changes), 1)
        self.assertFalse(changes[0].weakened)

    def test_shell_wrappers_cannot_turn_a_claim_green_by_default(self) -> None:
        for wrapper in ("pytest || true", "pytest; true", "if pytest; then true; else true; fi", "pytest 2>/dev/null || :", "(pytest) || exit 0"):
            with self.subTest(wrapper=wrapper):
                changes = diff_claims(self._claims("pytest"), self._claims(wrapper))
                self.assertTrue(changes[0].weakened, "the engine does not read shell; any changed command needs approval")


if __name__ == "__main__":
    unittest.main()
