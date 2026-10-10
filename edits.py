"""Line-anchored edits: the model is shown numbered excerpts of the functions it named, and answers with line
ranges to replace or lines to insert after, never by copying the original text. Code applies the edits from the
bottom of each file up, and refuses a range outside what was shown. Generic: nothing here knows a task.

Reply format (also accepted: the search/replace and file blocks of evo_main):
<<<REPLACE path LINES a-b>>>
...new lines (replace lines a..b inclusive, as numbered in the excerpt)...
<<<END>>>
<<<INSERT path AFTER LINE n>>>
...new lines...
<<<END>>>
"""
import re
from pathlib import Path

LINE_EDIT = re.compile(r"<<<\s*(REPLACE|INSERT)\s+([^<>\n]+?)\s+(?:LINES\s+(\d+)\s*-\s*(\d+)|AFTER\s+LINE\s+(\d+))\s*>>>[ \t]*\n?(.*?)\n?<<<\s*END\s*(?:EDIT)?\s*>>>",
                       re.S)

FORMAT = """\
Output format (nothing before or after the blocks, no explanations). Lines are numbered as in the excerpts above;
never retype the existing lines, name them:
<<<REPLACE path/from/app/root LINES a-b>>>
...the new lines that take the place of lines a to b (inclusive)...
<<<END>>>
<<<INSERT path/from/app/root AFTER LINE n>>>
...the new lines inserted after line n (AFTER LINE 0 inserts at the top)...
<<<END>>>
To add a new file:
<<<FILE path/from/app/root>>>
...complete content...
<<<END FILE>>>
Only lines shown in the excerpts may be replaced; insert points must be shown lines or line 0. Keep changes
minimal and local; never remove or rename existing controls, routes, functions or data fields other features use;
a new script must also be registered wherever the page and the build list scripts."""


def numbered(path: Path, rel: str, spans: list[tuple[int, int]] | None = None, context: int = 6) -> tuple[str, set[int]]:
    """The file (or the given 1-based line spans, widened by `context` and merged) with line numbers, and the set
    of line numbers shown."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    n = len(lines)
    if not spans:
        spans = [(1, n)]
    merged: list[list[int]] = []
    for a, b in sorted(spans):
        a, b = max(1, a - context), min(n, b + context)
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    shown: set[int] = set()
    parts = [f"<<<EXCERPT {rel} ({n} lines)>>>"]
    for a, b in merged:
        if parts[-1] != f"<<<EXCERPT {rel} ({n} lines)>>>":
            parts.append("    ...")
        for i in range(a, b + 1):
            parts.append(f"{i:5d}| {lines[i - 1]}")
            shown.add(i)
    parts.append("<<<END EXCERPT>>>")
    return "\n".join(parts), shown


def function_spans(text: str, names: list[str]) -> list[tuple[int, int]]:
    """1-based line spans of the named functions (a declaration line to its closing brace at the same indent)."""
    lines = text.splitlines()
    spans = []
    for i, line in enumerate(lines):
        for name in names:
            if re.search(r"\b(?:function\s*\*?\s*" + re.escape(name) + r"\b|(?:const|let|var)\s+" + re.escape(name) + r"\s*=|"
                         + re.escape(name) + r"\s*\([^)]*\)\s*\{|" + re.escape(name) + r"\s*:\s*(?:async\s*)?(?:function|\())", line):
                indent = len(line) - len(line.lstrip())
                j = i + 1
                while j < len(lines):
                    l = lines[j]
                    if l.strip() and (len(l) - len(l.lstrip())) <= indent and re.match(r"\s*[}\]]", l):
                        break
                    if l.strip() and (len(l) - len(l.lstrip())) <= indent and j > i + 1 and not re.match(r"\s*[})\],;]", l):
                        j -= 1
                        break
                    j += 1
                spans.append((i + 1, min(len(lines), j + 1)))
                break
    return spans


def apply_line_edits(out: Path, reply: str, shown: dict[str, set[int]]) -> tuple[list[str], list[str]]:
    """Apply REPLACE/INSERT blocks. Returns (changed paths, problems). Edits of one file are applied bottom-up so
    the numbers of the excerpt stay valid; a range outside the shown lines is refused."""
    per_file: dict[str, list] = {}
    problems: list[str] = []
    for m in LINE_EDIT.finditer(reply):
        kind, raw, a, b, n, body = m.groups()
        rel = raw.strip().strip("`'\"")
        if not rel.startswith(("frontend/", "backend/")) or ".." in rel:
            problems.append(f"{kind} of {rel!r}: not a path under frontend/ or backend/")
            continue
        if not (out / rel).is_file():
            problems.append(f"{kind} of {rel}: the file does not exist (use a FILE block to add it)")
            continue
        body_lines = body.strip("\n").splitlines() if body.strip() else []
        if kind == "REPLACE":
            a, b = int(a), int(b)
            if a > b or not shown.get(rel) or any(i not in shown[rel] for i in range(a, b + 1)):
                problems.append(f"REPLACE {rel} LINES {a}-{b}: outside the lines shown for that file")
                continue
            per_file.setdefault(rel, []).append((a, b, body_lines))
        else:
            n = int(n)
            total = len((out / rel).read_text(encoding="utf-8", errors="replace").splitlines())
            if n > 1 and n != total and (not shown.get(rel) or n not in shown[rel]):  # top and end of a file always open
                problems.append(f"INSERT {rel} AFTER LINE {n}: not a line shown for that file")
                continue
            per_file.setdefault(rel, []).append((n + 1, n, body_lines))  # insert before line n+1
    changed = []
    for rel, edits in per_file.items():
        f = out / rel
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        ends_nl = f.read_text(encoding="utf-8", errors="replace").endswith("\n")
        # overlapping replacements are a model error: keep the first, refuse the rest
        taken: set[int] = set()
        ordered = []
        for a, b, body in sorted(edits, key=lambda e: (-e[0], -e[1])):
            rng = set(range(a, b + 1)) if b >= a else set()
            if rng & taken:
                problems.append(f"REPLACE {rel} LINES {a}-{b}: overlaps another edit of this reply")
                continue
            taken |= rng
            ordered.append((a, b, body))
        for a, b, body in ordered:  # bottom-up
            if b >= a:
                lines[a - 1:b] = body
            else:
                lines[a - 1:a - 1] = body
        original = f.read_text(encoding="utf-8", errors="replace")
        new_text = "\n".join(lines) + ("\n" if ends_nl or lines else "")
        before_bal, after_bal = _brace_balance(original), _brace_balance(new_text)
        if f.suffix in (".js", ".mjs", ".cjs", ".ts", ".css") and before_bal == 0 and after_bal != 0:
            # a REPLACE that swallowed a closing brace or added an unclosed block: refused as a whole, the model
            # re-edits with the excerpt (sheet run 9: "Unexpected end of input" from a fix round)
            problems.append(f"edits of {rel}: refused, they leave the braces unbalanced ({after_bal:+d} '{{'); replace whole statements or functions")
            continue
        if f.suffix in (".js", ".mjs", ".cjs"):
            err = _syntax_error(new_text, rel)
            if err and not _syntax_error(original, rel):  # a file that parsed before must parse after (sheet run 10: an
                problems.append(f"edits of {rel}: refused, the file no longer parses: {err}")  # import list cut mid-way)
                continue
        f.write_text(new_text, encoding="utf-8")
        changed.append(rel)
    return changed, problems


def _syntax_error(text: str, rel: str) -> str:
    """The first line of node's syntax error for `text`, or '' when it parses (ES modules checked as .mjs)."""
    import subprocess, tempfile  # noqa: PLC0415
    suffix = ".mjs" if ("import " in text[:4000] or "export " in text[:4000]) else ".cjs"
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8") as tmp:
        tmp.write(text)
    try:
        r = subprocess.run(["node", "--check", tmp.name], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    if r.returncode == 0:
        return ""
    lines = [l for l in r.stderr.splitlines() if l.strip() and tmp.name not in l]
    return " / ".join(lines[:3])[:240].replace(tmp.name, rel)


def _brace_balance(text: str) -> int:
    """Count of '{' minus '}' outside strings and comments (approximate: template literals are treated as strings)."""
    stripped = re.sub(r"//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`", "", text, flags=re.S)
    return stripped.count("{") - stripped.count("}")
