"""A function-level graph of the application, built by regex (no model): functions with their line spans, call
edges between them (same file or through imports), edges from frontend code to the backend route that serves the
API path it calls, and the functions that render each interface string. Used to walk from the controls a feature
names to the code paths behind them (locate), and to tell edits outside those paths (scope). Generic."""
import re
from pathlib import Path

import code_index
import repair

_DECL = re.compile(r"^(?P<indent>[ \t]{0,2})(?:export\s+(?:default\s+)?)?(?:(?:async\s+)?function\s*\*?\s*(?P<f>\w+)"
                   r"|(?:const|let|var)\s+(?P<c>\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)\s*=>|function\b|\w+\s*=>)"
                   r"|(?P<m>\w+)\s*\([^)]*\)\s*\{$|(?P<p>\w+)\s*:\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>))")
_CALL = re.compile(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(")
_MEMBER_CALL = re.compile(r"\b([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\s*\(")
_API_PATH = re.compile(r"""['"`](/api/[^'"`\s?]*)""")
_METHOD_NEAR = re.compile(r"""['"](GET|POST|PUT|PATCH|DELETE)['"]""")
_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "function", "async", "await", "typeof", "new", "else"}


class Graph:
    def __init__(self, root: Path):
        self.root = root
        self.index = code_index.index_app(root)
        self.funcs: dict[str, list[tuple[str, int, int]]] = {}  # rel -> [(name, start, end)]
        self.text: dict[str, list[str]] = {}
        self.calls: dict[tuple[str, str], set[tuple[str, str]]] = {}  # (rel, fn) -> {(rel2, fn2)}
        self.strings: dict[str, list[tuple[str, str]]] = {}  # interface string -> [(rel, fn)]
        self.routes: list[tuple[str, str, str, str]] = []  # (method, path, rel, fn)
        self._build()

    # -- building
    def _build(self) -> None:
        src_root = self.root / "frontend" / "src"
        for rel in self.index:
            lines = (self.root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
            self.text[rel] = lines
            starts = []
            for i, line in enumerate(lines, 1):
                m = _DECL.match(line)
                if m and (self.root / rel).suffix != ".css":
                    name = next(g for g in (m.group("f"), m.group("c"), m.group("m"), m.group("p")) if g)
                    if name not in _KEYWORDS:
                        starts.append((name, i))
            spans = []
            for k, (name, start) in enumerate(starts):
                end = starts[k + 1][1] - 1 if k + 1 < len(starts) else len(lines)
                spans.append((name, start, end))
            self.funcs[rel] = spans
        # imports: alias -> (module rel, imported name)
        imports: dict[str, dict[str, tuple[str, str]]] = {}
        for rel in self.index:
            f = self.root / rel
            table = {}
            for mod, _spec, names in repair.imports_of(f, self.root, src_root):
                if mod is None:
                    continue
                mrel = mod.relative_to(self.root).as_posix()
                for n in names:
                    table[n] = (mrel, n)
            for alias, spec in repair._REQUIRE_AS.findall("\n".join(self.text[rel])):
                mod = repair._resolve(spec, f, self.root, src_root)
                if mod is not None:
                    table[alias] = (mod.relative_to(self.root).as_posix(), "*")
            for m in re.finditer(r"import\s+\*\s+as\s+(\w+)\s+from\s+['\"]([^'\"]+)['\"]", "\n".join(self.text[rel])):
                mod = repair._resolve(m.group(2), f, self.root, src_root)
                if mod is not None:
                    table[m.group(1)] = (mod.relative_to(self.root).as_posix(), "*")
            imports[rel] = table
        # routes from the index: the function enclosing the route line, or the handler it names
        for rel, d in self.index.items():
            for method, path, line in d["routes"]:
                text_line = self.text[rel][line - 1] if line - 1 < len(self.text[rel]) else ""
                h = re.search(r"handler:\s*(?:\w+\()?(\w+)\.(\w+)", text_line)  # also `handler: wrap(mod.fn, ...)`
                if h and h.group(1) in imports.get(rel, {}):
                    self.routes.append((method, path, imports[rel][h.group(1)][0], h.group(2)))
                else:
                    fn = self.enclosing(rel, line)
                    if fn:
                        self.routes.append((method, path, rel, fn))
        # call edges and strings
        local_names = {rel: {n for n, _, _ in spans} for rel, spans in self.funcs.items()}
        for rel, spans in self.funcs.items():
            for name, start, end in spans:
                body = "\n".join(self.text[rel][start - 1:end])
                edges = set()
                for callee in set(_CALL.findall(body)):
                    if callee in _KEYWORDS or callee == name:
                        continue
                    if callee in local_names[rel]:
                        edges.add((rel, callee))
                    elif callee in imports.get(rel, {}):
                        mrel, imported = imports[rel][callee]
                        edges.add((mrel, imported if imported != "*" else callee))
                for alias, member in set(_MEMBER_CALL.findall(body)):
                    if alias in imports.get(rel, {}):
                        edges.add((imports[rel][alias][0], member))
                for api_path in set(_API_PATH.findall(body)):
                    for method, path, rrel, rfn in self.routes:
                        if self._route_matches(path, api_path):
                            edges.add((rrel, rfn))
                self.calls[(rel, name)] = edges
            for s, line in self.index[rel]["strings"]:
                fn = self.enclosing(rel, line)
                if fn:
                    self.strings.setdefault(s, []).append((rel, fn))

    @staticmethod
    def _route_matches(pattern: str, api_path: str) -> bool:
        """`pattern` comes from the index (a literal path or a regex source reduced to /api/x/:x); stray
        backslashes and anchors are dropped, placeholders become wildcards."""
        clean = pattern.replace("\\", "").rstrip("$").rstrip("/") or "/"
        rx = re.escape(clean).replace(":x", "[^/]+").replace(":n", r"\d+")
        try:
            return re.fullmatch(rx, api_path.rstrip("/")) is not None
        except re.error:
            return api_path.startswith(clean)

    def enclosing(self, rel: str, line: int) -> str | None:
        for name, start, end in self.funcs.get(rel, []):
            if start <= line <= end:
                return name
        return None

    # -- queries
    def functions_showing(self, control_names: list[str]) -> set[tuple[str, str]]:
        """Functions whose source holds one of the control names (exact substring)."""
        found = set()
        for rel, spans in self.funcs.items():
            for name, start, end in spans:
                body = "\n".join(self.text[rel][start - 1:end])
                if any(c in body for c in control_names):
                    found.add((rel, name))
        return found

    def handlers_of(self, api_paths: list[str]) -> list[tuple[str, str]]:
        """The route handlers serving the given API paths (paths a problem line names: "HTTP 404 /api/x/y")."""
        out = []
        for api_path in api_paths:
            for _method, path, rel, fn in self.routes:
                if self._route_matches(path, api_path.split("?")[0]) and (rel, fn) not in out:
                    out.append((rel, fn))
        return out

    def chain(self, control_names: list[str], depth: int = 3, limit: int = 24, api_paths: list[str] = ()) -> list[tuple[str, str]]:
        """The code paths behind the named controls: the functions rendering or handling them, their direct callers
        (the page that mounts a component usually fetches what it shows), then callees (through imports and API
        calls to backend handlers) up to `depth` steps; closest first. `api_paths` named by problems add their
        route handlers."""
        frontier = sorted(self.functions_showing(control_names))
        callers = [node for node, edges in self.calls.items() if any(c in edges for c in frontier) and node not in frontier]
        frontier = frontier + sorted(callers)[:6] + [h for h in self.handlers_of(list(api_paths)) if h not in frontier]
        seen, order = set(frontier), list(frontier)
        for _ in range(depth):
            nxt = []
            for node in frontier:
                for callee in sorted(self.calls.get(node, ())):
                    if callee not in seen and callee[0] in self.funcs and any(n == callee[1] for n, _, _ in self.funcs[callee[0]]):
                        seen.add(callee)
                        nxt.append(callee)
            order += nxt
            frontier = nxt
            if len(order) >= limit:
                break
        return order[:limit]

    def unserved_calls(self) -> list[str]:
        """Frontend functions calling an /api path that no backend route serves: a page from one tree talking to
        the server of another fails only at runtime, with a 404 the build check does not see."""
        table = [(m, p) for rel, d in self.index.items() if rel.startswith("backend/") for m, p, _ in d["routes"]
                 if p.rstrip("$").strip("/").count("/") >= 1]  # "/api/" alone is a prefix test, not a route
        out = []
        for rel, spans in self.funcs.items():
            if not rel.startswith("frontend/"):
                continue
            for name, start, end in spans:
                for line in self.text[rel][start - 1:end]:
                    for api_path in _API_PATH.findall(line):
                        path = re.sub(r"\$\{[^}]*\}", "1", api_path)
                        if "${" in path:  # an expression with spaces or quotes cut the literal short: what precedes it is the path
                            path = path[:path.index("${")]
                        methods = _METHOD_NEAR.findall(line)
                        if path.endswith("/"):  # a prefix joined with a variable
                            served = any(p.startswith(path) for _, p in table)
                        else:
                            served = any((self._route_matches(p, path) or (p.endswith("/") and path.startswith(p)))
                                         and (not methods or m in ("?", methods[-1])) for m, p in table)
                        if not served:
                            out.append(f"{rel}:{name} calls {methods[-1] + ' ' if methods else ''}{api_path}, which no server route serves")
        return sorted(set(out))

    def describe(self, nodes: list[tuple[str, str]]) -> str:
        by_file: dict[str, list[str]] = {}
        for rel, fn in nodes:
            span = next(((s, e) for n, s, e in self.funcs.get(rel, []) if n == fn), None)
            by_file.setdefault(rel, []).append(f"{fn}" + (f" (lines {span[0]}-{span[1]})" if span else ""))
        return "\n".join(f"{rel}: {', '.join(fns)}" for rel, fns in by_file.items())


def changed_functions(before: dict[str, list[str]], root: Path, rels: list[str]) -> list[tuple[str, str]]:
    """Functions whose lines differ between `before` (rel -> lines) and the files on disk now."""
    import difflib
    g = Graph(root)
    out = []
    for rel in rels:
        old = before.get(rel)
        if old is None or rel not in g.text:
            continue
        for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old, g.text[rel]).get_opcodes():
            if tag == "equal":
                continue
            for line in range(j1 + 1, max(j2, j1 + 1) + 1):
                fn = g.enclosing(rel, line)
                if fn and (rel, fn) not in out:
                    out.append((rel, fn))
    return out
