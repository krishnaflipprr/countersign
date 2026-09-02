"""The marker scan: unfinished work that looks like finished work.

This engine is generalized from a gate that ran daily on a production
certified tree of 546+ source files and was tuned to zero false positives:
patterns are deliberately narrow. A check that cries wolf gets ignored, so
every rule here earned its place by catching a real defect class and never
firing on honest code.

Two layers:

1. Marker rules (all languages in scope): regex over each line, ported from
   the proven gate.
2. Structural rules (Python only, via the ast module): functions whose body
   is nothing (a bare ``pass`` or ``...``) are reported even when no marker
   comment advertises them. Overloads, abstract methods and Protocol methods
   are the legitimate uses of empty bodies and are excluded.

Exemptions are in the file itself: append the exemption marker to a line
that is a genuine false positive (a UI label, a vendor capability note).
Exemptions are counted and reported on every receipt, so they stay visible
instead of rotting in a config file.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config

EMPTY_BODY_EXEMPT_DECORATORS = {"overload", "abstractmethod", "overridable", "abc.override"}
EMPTY_BODY_EXEMPT_BASES = {"Protocol"}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    pattern: "re.Pattern[str]"
    why: str


def _rule(rule_id: str, source: str, flags: int, why: str) -> Rule:
    return Rule(rule_id, re.compile(source, flags), why)


RULES: list[Rule] = [
    _rule("unfinished-marker", r"\b(TODO|FIXME|XXX|HACK)\b", 0, "unfinished-work marker; finish the work or record the question where the team keeps them"),  # countersign: exempt
    _rule("not-implemented", r"not (yet implemented|implemented yet)", re.IGNORECASE, "the code declares itself unimplemented"),  # countersign: exempt
    _rule("not-implemented-error", r"NotImplementedError", re.IGNORECASE, "raises instead of doing the work"),  # countersign: exempt
    _rule("deferred-implementation", r"implemented (later|in a future)", re.IGNORECASE, "defers the implementation"),  # countersign: exempt
    _rule("stub-word", r"\bstub(bed)?\b", re.IGNORECASE, "unfinished stand-in code"),  # countersign: exempt
    _rule("fabricated-data", r"(fake|dummy|mock|sample|placeholder|hardcoded|hard-coded) (data|value|values|response|result|results|payload)", re.IGNORECASE, "fabricated values standing in for a real query or API call"),  # countersign: exempt
    _rule("simplified-implementation", r"(simplified|simplistic) (implementation|version|approach)", re.IGNORECASE, "a deliberately incomplete implementation"),  # countersign: exempt
    _rule("real-implementation-deferred", r"in (a )?real (implementation|system|world|deployment)", re.IGNORECASE, "describes what production would do instead of doing it"),  # countersign: exempt
    _rule("would-be-done", r"would be (implemented|replaced|fetched|queried)", re.IGNORECASE, "describes work not done"),  # countersign: exempt
    _rule("coming-soon", r"coming soon", re.IGNORECASE, "a feature advertised as absent"),  # countersign: exempt
    _rule("empty-return-standin", r"return (\[\]|\{\}|None|null)\s*(#|//)\s*(TODO|placeholder|stub|for now)", re.IGNORECASE, "returns empty as a stand-in for a real result"),  # countersign: exempt
]


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule_id: str
    why: str
    evidence: str


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name):
            names.add(decorator.id)
        elif isinstance(decorator, ast.Attribute):
            names.add(decorator.attr)
    return names


def _inside_protocol(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    while parent is not None:
        if isinstance(parent, ast.ClassDef):
            return any(
                (isinstance(base, ast.Name) and base.id in EMPTY_BODY_EXEMPT_BASES)
                or (isinstance(base, ast.Attribute) and base.attr in EMPTY_BODY_EXEMPT_BASES)
                for base in parent.bases
            )
        parent = parents.get(parent)
    return False


def _python_empty_functions(source: str) -> list[tuple[int, str]]:
    """Line numbers of functions whose body does nothing, with their names.

    A docstring alone does not count as doing something. Overloads,
    abstract methods and Protocol methods are the legitimate empty body and
    are not reported.
    """
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    reported: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _decorator_names(node) & EMPTY_BODY_EXEMPT_DECORATORS:
            continue
        if _inside_protocol(node, parents):
            continue
        body = list(node.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        if not body:
            reported.append((node.lineno, node.name))
            continue
        does_something = False
        for statement in body:
            if isinstance(statement, ast.Pass):
                continue
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and statement.value.value is Ellipsis:
                continue
            does_something = True
            break
        if not does_something:
            reported.append((node.lineno, node.name))
    return reported


def scan_file(config: Config, relative: str, absolute: Path) -> tuple[list[Finding], int]:
    """Scan one file. Returns (findings, exemptions_used)."""
    try:
        text = absolute.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], 0

    findings: list[Finding] = []
    exemptions = 0
    lines = text.splitlines()

    for number, line in enumerate(lines, start=1):
        if config.exempt_marker in line:
            exemptions += 1
            continue
        for rule in RULES:
            if rule.pattern.search(line):
                findings.append(Finding(relative, number, rule.rule_id, rule.why, line.strip()[:240]))

    if absolute.suffix == ".py":
        try:
            for lineno, name in _python_empty_functions(text):
                marker_line = lines[lineno - 1] if lineno - 1 < len(lines) else ""
                if config.exempt_marker in marker_line:
                    exemptions += 1
                    continue
                findings.append(
                    Finding(relative, lineno, "empty-body", f"function '{name}' has a body that does nothing", marker_line.strip()[:240])
                )
        except SyntaxError:
            findings.append(Finding(relative, 1, "unparseable", "the file cannot be parsed as Python; an agent may have left it broken", ""))

    return findings, exemptions


def scan_tree(config: Config) -> tuple[list[Finding], int, int]:
    """Scan every collected file. Returns (findings, exemptions, files_scanned)."""
    findings: list[Finding] = []
    exemptions = 0
    files = config.collect_files()
    root = Path(config.root).resolve()
    for absolute in files:
        relative = str(absolute.relative_to(root))
        file_findings, file_exemptions = scan_file(config, relative, absolute)
        findings.extend(file_findings)
        exemptions += file_exemptions
    findings.sort(key=lambda f: (f.path, f.line, f.rule_id))
    return findings, exemptions, len(files)
