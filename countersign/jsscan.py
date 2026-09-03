# audited on 20260903
"""Structural check for TypeScript and JavaScript: functions that do nothing.

Python gets this check from the ``ast`` module. There is no parser for
TypeScript in the standard library, and a dependency is not on the table, so
this module does the one narrow thing the check needs without parsing the
language: it blanks out every comment, string, template literal and regular
expression literal (keeping line breaks), then looks for the three shapes
an agent leaves behind when it declares a function and never writes it:

  function name(...) {}            declarations, async, generators, default
  name(...) {}                     class and object literal methods
  export const name = (...) => {}  exported arrow and function expressions

Only a body that is empty before blanking counts: a block holding nothing
but a comment is a documented no-op, which is a decision, not unfinished
work (and whatever the comment admits is the marker rules' business). The check is
tuned to never fire on honest code:

- constructors are skipped (parameter properties make an empty body normal),
- Angular lifecycle hooks (``ngOnInit`` and friends) are skipped,
- control flow keywords are never treated as a method name,
- unexported arrow functions are skipped (callbacks and defaults are
  legitimately empty), as is an exported ``noop``,
- declaration files (``.d.ts``) and minified sources are skipped outright.

Overloads, abstract methods and interface methods have no body and match
nothing. Anything this cannot see is a miss, never a false positive.
"""

from __future__ import annotations

import re

# A line longer than this is generated or minified code, where empty
# functions are polyfill noise rather than an agent's unfinished work.
MINIFIED_LINE_LENGTH = 1000

# Words that can precede ``(`` and a block without being a method name.
NOT_A_METHOD = frozenset({
    "if", "for", "while", "switch", "catch", "with", "function", "return",
    "await", "yield", "typeof", "new", "throw", "void", "delete", "in", "of",
    "case", "else", "do", "try", "finally", "constructor", "super", "import",
    "export", "as", "from", "declare", "abstract", "class", "interface",
    "namespace", "module", "enum", "type", "let", "const", "var", "instanceof",
})

# Method names whose empty body is a framework convention, not unfinished work.
FRAMEWORK_HOOK = re.compile(r"^ng[A-Z]\w*$")
NOOP_NAME = re.compile(r"^no[_-]?op$", re.IGNORECASE)

MODIFIERS = frozenset({"public", "private", "protected", "static", "async", "override", "readonly", "get", "set", "declare", "abstract"})

_IDENT = r"[A-Za-z_$][\w$]*"
_FUNCTION_DECL = re.compile(r"\bfunction\b\s*(\*)?\s*(" + _IDENT + r")?\s*(?=[<(])")
_METHOD_LINE = re.compile(r"^[ \t]*((?:(?:" + "|".join(sorted(MODIFIERS)) + r")\s+)*)(" + _IDENT + r")\s*(?=[<(])", re.MULTILINE)
_EXPORTED_ARROW = re.compile(r"\bexport\s+(?:const|let|var)\s+(" + _IDENT + r")\b")

# Characters after which a ``/`` begins a regular expression literal rather
# than a division. Good enough for blanking; a wrong guess only affects
# what is blanked on that line.
_REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%<>~^")


def mask_source(source: str) -> str:
    """Blank every comment, string, template literal and regex literal.

    Every blanked character becomes a space; line breaks and everything
    else are kept, so offsets and line numbers in the result are the
    offsets and line numbers of the source.
    """
    out = list(source)
    n = len(source)
    i = 0
    last_significant: str | None = None
    # Stack of template-literal expression depths: inside ``${ ... }`` code
    # resumes, and a ``}`` at depth zero returns to the template.
    template_depths: list[int] = []

    def blank(start: int, end: int) -> None:
        for k in range(start, end):
            if out[k] not in "\r\n":
                out[k] = " "

    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = i
            while j < n and source[j] not in "\r\n":
                j += 1
            blank(i, j)
            i = j
            continue
        if c == "/" and nxt == "*":
            j = source.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
            continue
        if c in "'\"":
            j = i + 1
            while j < n and source[j] != c and source[j] not in "\r\n":
                if source[j] == "\\":
                    j += 1
                j += 1
            blank(i + 1, min(j, n))
            i = min(j + 1, n)
            last_significant = c
            continue
        if c == "`":
            i = _mask_template(source, out, i, template_depths)
            last_significant = "`"
            continue
        if template_depths:
            if c == "{":
                template_depths[-1] += 1
            elif c == "}":
                if template_depths[-1] == 0:
                    template_depths.pop()
                    i = _mask_template(source, out, i, template_depths, resume=True)
                    last_significant = "`"
                    continue
                template_depths[-1] -= 1
        if c == "/" and (last_significant is None or last_significant in _REGEX_PRECEDERS or last_significant == "return"):
            j = i + 1
            in_class = False
            while j < n and source[j] not in "\r\n":
                ch = source[j]
                if ch == "\\":
                    j += 2
                    continue
                if in_class:
                    if ch == "]":
                        in_class = False
                elif ch == "[":
                    in_class = True
                elif ch == "/":
                    break
                j += 1
            blank(i + 1, min(j, n))
            i = min(j + 1, n)
            last_significant = "/"
            continue
        if not c.isspace():
            if c.isalnum() or c in "_$":
                k = i
                while k < n and (source[k].isalnum() or source[k] in "_$"):
                    k += 1
                word = source[i:k]
                last_significant = "return" if word == "return" else word[-1]
                i = k
                continue
            last_significant = c
        i += 1
    return "".join(out)


def _mask_template(source: str, out: list[str], start: int, depths: list[int], *, resume: bool = False) -> int:
    """Blank a template literal from ``start`` (a backtick, or the ``}``
    closing an expression when resuming). Returns the index after it."""
    n = len(source)
    i = start + 1
    if not resume:
        pass
    else:
        out[start] = " "
    while i < n:
        c = source[i]
        if c == "\\":
            out[i] = " "
            if i + 1 < n and source[i + 1] not in "\r\n":
                out[i + 1] = " "
            i += 2
            continue
        if c == "`":
            return i + 1
        if c == "$" and i + 1 < n and source[i + 1] == "{":
            out[i] = " "
            out[i + 1] = " "
            depths.append(0)
            return i + 2
        if c not in "\r\n":
            out[i] = " "
        i += 1
    return n


def _matching(text: str, open_index: int) -> int:
    """Index of the bracket closing the one at ``open_index``, or -1."""
    pairs = {"(": ")", "[": "]", "{": "}", "<": ">"}
    stack = [pairs[text[open_index]]]
    i = open_index + 1
    n = len(text)
    while i < n:
        c = text[i]
        if c in pairs:
            stack.append(pairs[c])
        elif c in ")]}>":
            if c == stack[-1]:
                stack.pop()
                if not stack:
                    return i
            elif c == ">":
                pass  # an arrow or comparison inside a type; not a bracket
            else:
                return -1
        i += 1
    return -1


def _skip_space(text: str, i: int) -> int:
    n = len(text)
    while i < n and text[i].isspace():
        i += 1
    return i


def _body_after_signature(text: str, i: int) -> tuple[int, int] | None:
    """Given ``i`` at ``(`` or ``<`` of a signature, find the body braces.

    Returns (open_brace, close_brace) or None when there is no body (an
    overload, an abstract method, a call).
    """
    if i < len(text) and text[i] == "<":
        close = _matching(text, i)
        if close < 0:
            return None
        i = _skip_space(text, close + 1)
    if i >= len(text) or text[i] != "(":
        return None
    close = _matching(text, i)
    if close < 0:
        return None
    i = _skip_space(text, close + 1)
    if i < len(text) and text[i] == ":":
        i = _skip_type(text, i + 1)
    return _block_at(text, i)


def _skip_type(text: str, i: int) -> int:
    """Skip a type annotation, returning the index of the body's ``{``.

    Types can contain braces (object types); the body brace is the first
    ``{`` at bracket depth zero whose block is followed by something other
    than another ``{``, or the first one at depth zero after a non-type
    token. In practice: take the first depth-zero ``{``; if the text after
    its matching ``}`` is another ``{``, the first was the type.
    """
    n = len(text)
    depth = 0
    while i < n:
        c = text[i]
        if c in "([<":
            depth += 1
        elif c in ")]>":
            depth -= 1
        elif c == "{" and depth == 0:
            close = _matching(text, i)
            if close < 0:
                return i
            after = _skip_space(text, close + 1)
            if after < n and text[after] == "{":
                return after
            return i
        elif c in ";\n" and depth == 0 and c == ";":
            return i
        i += 1
    return n


def _block_at(text: str, i: int) -> tuple[int, int] | None:
    if i >= len(text) or text[i] != "{":
        return None
    close = _matching(text, i)
    if close < 0:
        return None
    return i, close


def _is_unexplained_empty_block(original: str, masked: str, open_brace: int, close_brace: int) -> bool:
    """Empty in the source itself, not merely empty once comments are blanked."""
    return masked[open_brace + 1:close_brace].strip() == "" and original[open_brace + 1:close_brace].strip() == ""


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def empty_functions(source: str) -> list[tuple[int, str]]:
    """(line, name) for every function whose body does nothing."""
    if any(len(line) > MINIFIED_LINE_LENGTH for line in source.split("\n")):
        return []
    original = source.replace("\r\n", "\n").replace("\r", "\n")
    text = mask_source(original)
    found: dict[int, tuple[int, str]] = {}

    for match in _FUNCTION_DECL.finditer(text):
        name = match.group(2) or _expression_name(text, match.start())
        if name is None:
            continue
        body = _body_after_signature(text, match.end())
        if body and _is_unexplained_empty_block(original, text, *body):
            found.setdefault(body[0], (_line_of(text, match.start()), name))

    for match in _METHOD_LINE.finditer(text):
        name = match.group(2)
        if name in NOT_A_METHOD or FRAMEWORK_HOOK.match(name):
            continue
        body = _body_after_signature(text, match.end())
        if body and _is_unexplained_empty_block(original, text, *body):
            found.setdefault(body[0], (_line_of(text, match.start(2)), name))

    for match in _EXPORTED_ARROW.finditer(text):
        name = match.group(1)
        if NOOP_NAME.match(name):
            continue
        body = _arrow_body(text, match.end())
        if body and _is_unexplained_empty_block(original, text, *body):
            found.setdefault(body[0], (_line_of(text, match.start()), name))

    return sorted(found.values())


_EXPORT_DEFAULT_BEFORE = re.compile(r"export\s+default\s*$")
_EXPORTED_BINDING_BEFORE = re.compile(r"\bexport\s+(?:const|let|var)\s+(" + _IDENT + r")\s*(?::[^=;]*)?=\s*$")


def _expression_name(text: str, function_index: int) -> str | None:
    """Name for an anonymous ``function`` expression, following the arrow
    rule: ``export default`` and exported bindings count, callbacks and
    local bindings do not, and an exported noop is exempt."""
    before = text[max(0, function_index - 200):function_index]
    if _EXPORT_DEFAULT_BEFORE.search(before):
        return "default"
    binding = _EXPORTED_BINDING_BEFORE.search(before)
    if binding and not NOOP_NAME.match(binding.group(1)):
        return binding.group(1)
    return None


def _arrow_body(text: str, i: int) -> tuple[int, int] | None:
    """From just after an exported binding's name, find ``=> {`` at depth
    zero within the statement and return its block."""
    n = len(text)
    depth = 0
    while i < n:
        c = text[i]
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif c == "{" and depth == 0:
            return None  # a block or object before any arrow: not an arrow function
        elif c == ";" and depth == 0:
            return None
        elif c == "=" and depth == 0 and i + 1 < n and text[i + 1] == ">":
            j = _skip_space(text, i + 2)
            return _block_at(text, j)
        i += 1
    return None
