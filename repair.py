"""Finding the parts of an inherited application that are no longer reachable: modules nothing imports from the
entry points (a later generation rewrote the router, the API client or the server and left earlier pages behind),
and the names those modules import that their providers no longer export. Code only, no model; the result is given
to the model to re-integrate. Generic: nothing here knows a particular task."""
import re
from pathlib import Path

_ES_IMPORT = re.compile(r"""(?:^|\n)\s*import\s+(?:([\w$]+)\s*,?\s*)?(?:\{([^}]*)\})?\s*(?:\*\s+as\s+[\w$]+\s*)?(?:from\s+)?['"]([^'"]+)['"]""")
_DYN_IMPORT = re.compile(r"""import\(\s*['"]([^'"]+)['"]\s*\)""")
_REQUIRE = re.compile(r"""require\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)""")
_EXPORT = re.compile(r"export\s+(?:async\s+)?(?:function\s*\*?\s*|const\s+|let\s+|var\s+|class\s+)([\w$]+)")
_EXPORT_LIST = re.compile(r"export\s*\{([^}]*)\}")
_CJS_EXPORTS = re.compile(r"module\.exports\s*=\s*\{([^}]*)\}|exports\.([\w$]+)\s*=")
_HREF = re.compile(r"""href\s*[:=]\s*[`'"]([^`'"\s]{1,80})[`'"]|\bnavigate\(\s*[`'"]([^`'"\s]{1,80})[`'"]""")
_SCRIPT_SRC = re.compile(r"""<script[^>]+src\s*=\s*['"]([^'"]+)['"]""")


def _read(f: Path) -> str:
    return f.read_text(encoding="utf-8", errors="replace")


def _resolve(spec: str, from_file: Path, root: Path, src_root: Path) -> Path | None:
    if spec.startswith("/"):
        cand = src_root / spec.lstrip("/")
    elif spec.startswith("."):
        cand = (from_file.parent / spec).resolve()
    else:
        return None  # a bare module name
    for c in (cand, cand.with_suffix(cand.suffix or ".js"), Path(str(cand) + ".js"), cand / "index.js"):
        if c.is_file():
            return c
    return None


def imports_of(f: Path, root: Path, src_root: Path) -> list[tuple[Path | None, str, list[str]]]:
    """(resolved module or None, spec, imported names) per import statement of a file."""
    text = _read(f)
    out = []
    for m in _ES_IMPORT.finditer(text):
        names = [n.strip().split(" as ")[0].strip() for n in (m.group(2) or "").split(",") if n.strip()]
        if m.group(1):  # `import X from` needs a default export
            names.append("default")
        out.append((_resolve(m.group(3), f, root, src_root), m.group(3), names))
    for m in _DYN_IMPORT.finditer(text):
        out.append((_resolve(m.group(1), f, root, src_root), m.group(1), []))
    for m in _REQUIRE.finditer(text):
        out.append((_resolve(m.group(1), f, root, src_root), m.group(1), []))
    return out


def exports_of(f: Path) -> set[str]:
    text = _read(f)
    names = set(_EXPORT.findall(text))
    if re.search(r"export\s+default", text):
        names.add("default")
    for m in _EXPORT_LIST.finditer(text):
        names |= {n.strip().split(" as ")[-1].strip() for n in m.group(1).split(",") if n.strip()}
    for m in _CJS_EXPORTS.finditer(text):
        if m.group(1):
            names |= {n.strip().split(":")[0].strip() for n in m.group(1).split(",") if n.strip()}
        elif m.group(2):
            names.add(m.group(2))
    return names


def entries(root: Path) -> list[Path]:
    out = []
    html = root / "frontend" / "src" / "index.html"
    if html.is_file():
        for src in _SCRIPT_SRC.findall(_read(html)):
            p = _resolve(src, html, root, root / "frontend" / "src")
            if p:
                out.append(p)
    for cand in ("frontend/src/app.js", "frontend/src/main.js", "frontend/src/index.js", "backend/server.js", "backend/index.js"):
        p = root / cand
        if p.is_file() and p not in out:
            out.append(p)
    return out


def analyse(root: Path) -> dict:
    """{"orphans": [rel], "missing": {rel: [(provider rel, [names])]}, "reachable": [rel]} over the source files;
    missing covers every module, reachable or not."""
    srcs = []
    for part in ("frontend/src", "backend"):
        base = root / part
        if base.is_dir():
            srcs += [f for f in base.rglob("*.js") if "node_modules" not in f.parts and "dist" not in f.parts]
    src_root = root / "frontend" / "src"
    graph = {f: [m for m, _, _ in imports_of(f, root, src_root) if m] for f in srcs}
    seen, todo = set(), list(entries(root))
    while todo:
        f = todo.pop()
        if f in seen or f not in graph:
            continue
        seen.add(f)
        todo += graph[f]
    orphans = sorted(f for f in srcs if f not in seen and f.name not in ("build.js",))
    missing = {}
    for f in srcs:  # every module: a re-integrated page that imports a name its provider lacks fails at load time
        for mod, _spec, names in imports_of(f, root, src_root):
            if mod is None or not names:
                continue
            lacking = [n for n in names if n not in exports_of(mod)]
            if lacking:
                missing.setdefault(f.relative_to(root).as_posix(), []).append((mod.relative_to(root).as_posix(), lacking))
    return {"orphans": [f.relative_to(root).as_posix() for f in orphans],
            "missing": missing,
            "members": member_misuse(srcs, root),
            "reachable": sorted(f.relative_to(root).as_posix() for f in seen)}


_REQUIRE_AS = re.compile(r"""(?:const|let|var)\s+([\w$]+)\s*=\s*require\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)""")


def member_misuse(srcs: list, root: Path) -> list[str]:
    """`X.name` where X is a required module that does not export name ("route.handler is not a function" at run
    time: a route table naming org.listOrgs while org.js exports list)."""
    out = []
    for f in srcs:
        text = _read(f)
        for alias, spec in _REQUIRE_AS.findall(text):
            mod = _resolve(spec, f, root, root / "frontend" / "src")
            if mod is None:
                continue
            exported = exports_of(mod)
            if not exported:
                continue
            used = sorted({m for m in re.findall(r"\b" + re.escape(alias) + r"\.([\w$]+)", text)})
            bad = [u for u in used if u not in exported]
            if bad:
                out.append(f"{f.relative_to(root).as_posix()} uses {alias}.{', '.join(bad)} but {mod.relative_to(root).as_posix()} "
                           f"exports only: {', '.join(sorted(exported))}")
    return out


def summary(root: Path, rel: str) -> str:
    """One orphan module for the model: its exports, what it imports (and from where), the addresses it links to."""
    f = root / rel
    text = _read(f)
    exports = sorted(exports_of(f))
    imps = [f"{', '.join(names) or '*'} from {spec}" for _, spec, names in imports_of(f, root, root / "frontend" / "src")]
    hrefs = sorted({(a or b) for a, b in _HREF.findall(text)})[:12]
    sig = [l.strip()[:110] for l in text.splitlines() if re.match(r"\s*(?:export\s+)?(?:async\s+)?function\s+\w+", l)][:14]
    return (f"{rel} ({len(text.splitlines())} lines)\n  exports: {', '.join(exports) or '-'}\n  imports: {'; '.join(imps) or '-'}\n"
            + (f"  links to: {', '.join(hrefs)}\n" if hrefs else "") + "  functions: " + " | ".join(sig))


def module_style_mismatch(root: Path) -> list[str]:
    """ES `export`/`import` statements in a CommonJS backend (one that uses require), and `require` in an ES-module
    frontend: Node reports "Unexpected token 'export'" without a file name, which fix rounds then guess at."""
    out = []
    backend = root / "backend"
    if backend.is_dir():
        files = [f for f in backend.rglob("*.js") if "node_modules" not in f.parts]
        if any(re.search(r"\brequire\(", _read(f)) for f in files):
            for f in files:
                for i, line in enumerate(_read(f).splitlines(), 1):
                    if re.match(r"\s*(?:export\s+(?:default|const|let|var|function|class|\{)|import\s+[\w{*].*\sfrom\s)", line):
                        out.append(f"{f.relative_to(root).as_posix()}:{i} uses an ES module statement in a CommonJS backend "
                                   f"(use module.exports / require): {line.strip()[:80]}")
                        break
    src = root / "frontend" / "src"
    if src.is_dir():
        for f in src.rglob("*.js"):
            for i, line in enumerate(_read(f).splitlines(), 1):
                if re.match(r"\s*(?:const|let|var)\s+[\w{}\s,$]+=\s*require\(", line):
                    out.append(f"{f.relative_to(root).as_posix()}:{i} uses require() in the browser (use import): {line.strip()[:80]}")
                    break
    return out
