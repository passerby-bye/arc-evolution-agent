"""A deterministic index of an application's source, for locating where a requirement's feature lives. Regex-based,
no model: per file its top-level functions, HTTP routes and the human-facing strings it contains, each with a line
number; and `locate`, which finds the files and lines mentioning the names a feature's text quotes. Generic: nothing
here knows a particular task.

usage: python code_index.py <app dir>                      prints the index
       python code_index.py <app dir> --find "Name" ...    prints where the names occur
"""
import re
import sys
from pathlib import Path

SOURCE_EXT = {".js", ".mjs", ".cjs", ".ts", ".html", ".css"}
SKIP_DIRS = {"node_modules", "dist", ".git", ".arc", "requirements"}
MAX_STRINGS_PER_FILE = 60

_FUNC = re.compile(r"^(?P<indent>[ \t]{0,2})(?:export\s+)?(?:(?:async\s+)?function\s*\*?\s*(?P<f>\w+)"
                   r"|(?:const|let|var)\s+(?P<c>\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)\s*=>|function\b|\w+\s*=>)"
                   r"|(?P<m>\w+)\s*\([^)]*\)\s*\{$"
                   r"|(?P<p>\w+)\s*:\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>))")
_ROUTE_LIT = re.compile(r"""['"](/api/[^'"\s]*)['"]|/\^\\?/(api(?:\\/(?:\([^)]*\)|[^\\/]*))*)\$?/""")  # a regex route: segments are groups or plain text
_METHOD = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\b")
_STR = re.compile(r"""'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\]|\\.)*)`|>([^<>{}\n]{2,60})<""")
_WORDS = re.compile(r"[A-Za-z]")
# a human-facing string: starts with a capital letter or holds a space; not a path, selector, constant or code
_LABEL = re.compile(r"^(?=.*[A-Za-z])(?!.*[/\\<>{}();=]|^[A-Z0-9_\-]+$|^[a-z][\w-]*$)[A-Z0-9][^\n]{1,59}$|^[^\n]{2,60}\s[^\n]*$")
_CODEY = re.compile(r"^\s*$|^[\w\-./:#?&=%]+$|^(?:[a-z][\w-]*\s*=|data-|aria-|class|type|id|src|href|[=/<>\[\]])"
                    r"|&&|\|\||=\"|='|; charset=|^[\w ]+=$")


_TAG_TEXT = re.compile(r">\s*([^<>{}$`]{1,60}?)\s*<")  # <h1>Workbooks</h1> in an HTML file or a template literal


def _strings(line: str) -> list[str]:
    out = []
    for m in _STR.finditer(line):
        s = next(g for g in m.groups() if g is not None)
        for part in (re.split(r"\$\{[^}]*\}", s) if m.group(3) is not None else [s]):
            part = part.strip()
            if _WORDS.search(part) and _LABEL.match(part) and not _CODEY.match(part) and len(part) <= 60:
                out.append(part)
    for m in _TAG_TEXT.finditer(line):  # text between tags is an interface string too (sheet run 10: the "Workbooks"
        part = m.group(1).strip()  # heading was unknown and every test that named it was dropped)
        label = _LABEL.match(part) and (not _CODEY.match(part) or re.match(r"^[A-Z][a-z]+$", part))  # a single
        if _WORDS.search(part) and label and part not in out:  # capitalised word between tags is a heading, not code
            out.append(part)
    return out


def index_app(root: Path) -> dict:
    """{relative path: {"lines": n, "functions": [(name, line)], "routes": [(method, path, line)], "strings": [(s, line)]}}"""
    out = {}
    for f in sorted(root.rglob("*")):
        rel = f.relative_to(root).as_posix()
        if not f.is_file() or f.suffix not in SOURCE_EXT or SKIP_DIRS & set(f.relative_to(root).parts) \
                or f.name.endswith(("package-lock.json",)) or not rel.startswith(("frontend/", "backend/")):
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        funcs, routes, strings, seen = [], [], [], set()
        for i, line in enumerate(lines, 1):
            m = _FUNC.match(line)
            if m and f.suffix != ".css":
                name = next(g for g in (m.group("f"), m.group("c"), m.group("m"), m.group("p")) if g)
                if name not in ("if", "for", "while", "switch", "catch", "function", "return"):
                    funcs.append((name, i))
            for r in _ROUTE_LIT.finditer(line):
                path = (r.group(1) or "/" + r.group(2).replace("\\/", "/")).replace("([^/]+)", ":x").replace("(\\d+)", ":n")
                near = " ".join(lines[max(0, i - 3):i])
                methods = _METHOD.findall(line) or _METHOD.findall(near)
                routes.append((methods[-1] if methods else "?", path, i))
            if f.suffix != ".css":
                for s in _strings(line):
                    if s not in seen and len(strings) < MAX_STRINGS_PER_FILE:
                        seen.add(s)
                        strings.append((s, i))
        out[rel] = {"lines": len(lines), "functions": funcs, "routes": routes, "strings": strings}
    return out


def render(index: dict) -> str:
    parts = []
    for rel, d in index.items():
        parts.append(f"{rel} ({d['lines']} lines)")
        if d["functions"]:
            parts.append("  functions: " + ", ".join(f"{n}:{l}" for n, l in d["functions"]))
        if d["routes"]:
            parts.append("  routes: " + ", ".join(f"{m} {p}:{l}" for m, p, l in d["routes"]))
        if d["strings"]:
            parts.append("  strings: " + ", ".join(f"{s!r}:{l}" for s, l in d["strings"]))
    return "\n".join(parts)


def locate(root: Path, names: list[str], context: int = 0) -> dict[str, list[tuple[int, str]]]:
    """{relative path: [(line number, line)]} for every source line holding one of the names (exact, case-sensitive)."""
    hits: dict[str, list] = {}
    for f in sorted(root.rglob("*")):
        rel = f.relative_to(root).as_posix()
        if not f.is_file() or f.suffix not in SOURCE_EXT or SKIP_DIRS & set(f.relative_to(root).parts) \
                or not rel.startswith(("frontend/", "backend/")):
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines, 1):
            if any(n in line for n in names):
                for j in range(max(1, i - context), min(len(lines), i + context) + 1):
                    if (j, lines[j - 1]) not in hits.setdefault(rel, []):
                        hits[rel].append((j, lines[j - 1]))
    return hits


def quoted_names(text: str) -> list[str]:
    """The names a requirement text quotes (“...”, "..." or `...`), 2-60 chars, once each."""
    out = []
    for m in re.finditer(r"[“\"`]([^”\"`\n]{1,60})[”\"`]", text):  # 1 char too: skipping `8` pairs the wrong quotes
        v = m.group(1)
        if v not in out and _WORDS.search(v) and len(v) >= 2 and v.strip() == v:
            out.append(v)
    return out


def name_patterns(index: dict) -> list:
    """Regexes for the interface strings the code holds: a control that exists in the inherited application is a
    known name for tests even when the new requirement does not quote it ("Worksheet options for Sheet1" comes
    from the template 'Worksheet options for ' + name, so a string that ends a template is a prefix)."""
    out = []
    for d in index.values():
        for s, _ in d["strings"]:
            s = s.strip()
            if len(s) >= 3:
                out.append(re.compile(re.escape(s) + (r".*$" if s.endswith(("for", "for:", ":", " ")) or s.endswith(" for") else "$")))
    return out


if __name__ == "__main__":
    app = Path(sys.argv[1])
    if "--find" in sys.argv:
        names = sys.argv[sys.argv.index("--find") + 1:]
        for rel, rows in locate(app, names).items():
            print(rel)
            for n, line in rows:
                print(f"  {n}: {line.strip()[:140]}")
    else:
        text = render(index_app(app))
        print(text)
        print(f"\n[{len(text)} chars]", file=sys.stderr)
