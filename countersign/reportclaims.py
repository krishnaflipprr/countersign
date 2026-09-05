# audited on 20260905
"""Claims proposed from an agent's own completion report.

An agent ends its work with prose: "All tests pass. Created src/pricing.ts.
The endpoint at http://localhost:3000/api returns the price." Every one of
those is a claim, and most of them are checkable with one command. This
module turns the checkable sentences into proposed claims, deterministically,
with the agent's own sentence kept as the statement so the receipt later
says exactly which promise held and which did not.

Only English patterns, and only sentences whose disproof command can be
derived from the repository itself (its test runner, its build script, a
file path, a URL). A sentence that is checkable in principle but has no
derivable command is reported as unresolved with the reason, never guessed.
No model reads the report.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .claims import Claim
from .starter import TESTS_PASS, StarterClaim, _package_manager, detect_starter_claims


@dataclass(frozen=True)
class Proposal:
    claim: Claim
    sentence: str
    source: str


@dataclass(frozen=True)
class Unresolved:
    sentence: str
    reason: str


@dataclass
class Proposals:
    claims: list[Proposal] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)


_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_URL = re.compile(r"https?://[^\s'\"`<>()]+")
_PATH = re.compile(r"(?<![\w/])(?:[\w@.-]+/)*[\w@-][\w@.-]*\.[A-Za-z][A-Za-z0-9]{0,9}(?![\w/])")

_TESTS = re.compile(r"\b(?:tests?|test suite|specs?)\b.{0,40}?\b(?:pass|passes|passing|passed|green|succeed|succeeds)\b|\b(?:pass|passes|passing|passed)\b.{0,20}?\btests?\b", re.IGNORECASE)
_BUILD = re.compile(r"\bbuild(?:s|ing)?\b.{0,30}?\b(?:succeed|succeeds|succeeded|successful|successfully|passes|clean|cleanly|without errors|no errors)\b|\bbuilt successfully\b", re.IGNORECASE)
_LINT = re.compile(r"\blint(?:er|ing)?\b.{0,30}?\b(?:pass|passes|passing|passed|clean|no (?:errors|warnings))\b", re.IGNORECASE)
_TYPES = re.compile(r"\b(?:type ?checks?|type ?checking|typechecks?|tsc|types)\b.{0,30}?\b(?:pass|passes|passing|passed|clean|no (?:type )?errors)\b", re.IGNORECASE)
_CREATED = re.compile(r"\b(?:created|added|wrote|generated|introduced|updated|modified|edited|implemented|saved)\b", re.IGNORECASE)
_REMOVED = re.compile(r"\b(?:removed|deleted|dropped)\b", re.IGNORECASE)


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        line = _BULLET.sub("", line).strip()
        if not line:
            continue
        for piece in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _statement(sentence: str) -> str:
    flat = " ".join(sentence.split())
    return "Agent report: " + (flat if len(flat) <= 160 else flat[:157] + "...")


def _build_command(root: Path) -> str | None:
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict) and isinstance(scripts.get("build"), str) and scripts["build"].strip():
            return f"{_package_manager(root)} run build"
    if (root / "Cargo.toml").is_file():
        return "cargo build"
    if (root / "go.mod").is_file():
        return "go build ./..."
    return None


def claims_from_report(text: str, root: Path) -> Proposals:
    """Checkable sentences of ``text`` as proposed claims for the repository at ``root``."""
    root = Path(root)
    starters: dict[str, StarterClaim] = {c.claim_id: c for c in detect_starter_claims(root)}
    proposals = Proposals()
    seen: set[str] = set()

    def propose(claim_id: str, sentence: str, command: str, source: str, expect: str = "exit 0") -> None:
        if claim_id in seen:
            return
        seen.add(claim_id)
        proposals.claims.append(Proposal(Claim(claim_id, _statement(sentence), command, expect), sentence, source))

    def from_starter(claim_id: str, sentence: str, missing_reason: str) -> None:
        starter = starters.get(claim_id)
        if starter is None:
            if claim_id not in seen:
                proposals.unresolved.append(Unresolved(sentence, missing_reason))
            return
        propose(claim_id, sentence, starter.command, starter.source)

    for sentence in _sentences(text):
        urls = _URL.findall(sentence)
        for url in urls:
            url = url.rstrip(".,;:")
            bare = re.sub(r"^https?://", "", url)
            propose(f"url-{_slug(bare)}", sentence, f"curl -sf -o /dev/null {_quote(url)}", "URL in the report")
        without_urls = _URL.sub(" ", sentence)

        if _TESTS.search(without_urls):
            from_starter(TESTS_PASS, sentence, "no test runner recognised in this repository (no package.json test script, pytest, go.mod, Cargo.toml or Gemfile with spec/)")
        if _BUILD.search(without_urls):
            command = _build_command(root)
            if command:
                propose("build-succeeds", sentence, command, "build command of the repository")
            elif "build-succeeds" not in seen:
                proposals.unresolved.append(Unresolved(sentence, "no build command recognised (no package.json build script, Cargo.toml or go.mod)"))
        if _LINT.search(without_urls):
            from_starter("lint-clean", sentence, "no linter configuration recognised (no package.json lint script, ruff configuration)")
        if _TYPES.search(without_urls):
            from_starter("types-check", sentence, "no type checker recognised (no typecheck script, tsconfig with typescript, or mypy configuration)")

        removed = _REMOVED.search(without_urls)
        created = _CREATED.search(without_urls)
        if removed or created:
            verb = removed if removed and (not created or removed.start() < created.start()) else created
            rest = without_urls[verb.end():]
            path_match = _PATH.search(rest)
            if path_match:
                path = path_match.group(0).rstrip(".")
                if verb is removed:
                    propose(f"file-gone-{_slug(path)}", sentence, f"test ! -e {_quote(path)}", "file named in the report")
                else:
                    propose(f"file-{_slug(path)}", sentence, f"test -f {_quote(path)}", "file named in the report")

    return proposals


def without_ids(proposals: Proposals, ids: set[str]) -> Proposals:
    """The proposals whose claim id is not in ``ids`` (those already declared)."""
    return Proposals(claims=[p for p in proposals.claims if p.claim.claim_id not in ids], unresolved=list(proposals.unresolved))


def render_proposals_toml(proposals: Proposals) -> str:
    lines: list[str] = []
    for proposal in proposals.claims:
        lines += [
            f"# from the agent's report, {proposal.source}",
            "[[claim]]",
            f'id = "{proposal.claim.claim_id}"',
            f"statement = {_toml(proposal.claim.statement)}",
            f"command = {_toml(proposal.claim.command)}",
            f'expect = "{proposal.claim.expect}"',
            "",
        ]
    return "\n".join(lines)


def _toml(value: str) -> str:
    return json.dumps(value)
