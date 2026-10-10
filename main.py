#!/usr/bin/env python3
"""Evolution agent: extends an inherited application with the features a requirement changes or adds, one feature at
a time, keeping the rest as it is.

1. prepare: drop the data file left by earlier runs and the old request log; git baseline.
2. index the source (code_index: functions, routes, interface strings per file; no model).
3. pick the target features: those whose description carries a modified-feature section, and those whose quoted
   control names are mostly absent from the code (new features). Everything else is left alone.
4. seeds: one request writes the pre-provisioned records the target scenarios' GIVEN clauses name, into the app's
   own seed module; code checks every named record is there.
5. per target feature: locate the files by the names the feature quotes, ask for search/replace edits (or whole
   files for new ones), build and start, run one checked test per scenario, fix up to FIX_ROUNDS; a feature that
   leaves the app broken is reverted to the previous git commit.
6. QA pass from wholeapp (checks, scenario tests, model-written requirement checks), then commit.

Environment: OPENAI_BASE_URL, OPENAI_API_KEY, MODEL (the platform sets them).
"""
import argparse
import difflib
import html
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import wholeapp  # the package names the whole-app module wholeapp.py
except ImportError:  # the source tree names it main.py
    import main as wholeapp
import change
import code_index
import qa
import repair
import snapshot
import structured_tests

FIX_ROUNDS = 2
MAX_CONTEXT_CHARS = 64000  # located files given in full up to this size, else as windows around the hits
WINDOW = 70
REPAIR_CONTEXT_CHARS = 150000  # the unreachable modules shown in full to the repair request
TOTAL_MINUTES = 110
FEATURE_MINUTES = 9


def log(msg: str) -> None:
    print(f"[evo] {msg}", flush=True)


# ---------------------------------------------------------------- requirement pieces

def atomics(tree: dict) -> list[dict]:
    out = []

    def walk(n: dict) -> None:
        if n.get("scenarios") or str(n.get("type", "")).upper() == "ATOMIC":
            out.append(n)
        for c in n.get("children") or []:
            if isinstance(c, dict):
                walk(c)
    walk(tree)
    return out


def node_text(n: dict) -> str:
    """Name, description and scenarios of one feature as the requirement states them."""
    parts = [f"{n.get('id')} {n.get('name', '')}", str(n.get("description") or "").strip()]
    for i, sc in enumerate(n.get("scenarios") or [], 1):
        parts.append(f"Scenario {i}: {sc.get('name', '')}")
        for st in sc.get("steps") or []:
            if isinstance(st, dict):
                parts.append(f"  {st.get('keyword', '')}: {' '.join(str(st.get('content', '')).split())}")
    return "\n".join(parts)


def givens(nodes: list[dict]) -> list[str]:
    out = []
    for n in nodes:
        for sc in n.get("scenarios") or []:
            for st in sc.get("steps") or []:
                if isinstance(st, dict) and str(st.get("keyword", "")).upper() == "GIVEN":
                    out.append(f"{n.get('id')}: {' '.join(str(st.get('content', '')).split())}")
    return out


_RECORD = re.compile(r"`([^`\n]{1,80})`")  # 1 char too: skipping `8` would pair the wrong backticks
_NOT_RECORD = re.compile(r"^(?:[A-Z]{1,3}\d{1,5}|[A-Z]{1,3}\d*:[A-Z]{1,3}\d*|-?[\d.,%]+|=.*|.{0,1})$")


def record_names(texts: list[str]) -> list[str]:
    """Backtick-quoted values of GIVEN clauses that name records or their contents (not coordinates or numbers)."""
    out = []
    for t in texts:
        for sentence in re.split(r"(?<=[.;])\s+", t):
            if _NEGATED.search(sentence):  # "`evo_user_01` ... are not registered", "none contains the query `x`"
                continue
            for m in _RECORD.finditer(sentence):
                v = m.group(1).strip()
                if not _NOT_RECORD.match(v) and v not in out:
                    out.append(v)
    return out


_NEGATED = re.compile(r"\b(?:not registered|not exist|does not|do not|none (?:contains|of|has|have)|unregistered|"
                      r"are not|is not|unused|no (?:account|user|record|repository|workbook|organization|match))\b", re.I)


def present(name: str, text: str) -> bool:
    """A named record is in the seed source as a whole, or as each of its parts (`Atlas/Queued/17` is a row of
    three cells; `Ready, Holding, Released` a list of options)."""
    if name in text:
        return True
    parts = [p.strip() for p in re.split(r"/|,\s*", name) if p.strip()]
    return len(parts) > 1 and all(p in text for p in parts)


def focus_tree(tree: dict, targets: list[dict]) -> dict:
    """The requirement tree reduced to the target features (areas kept for their descriptions)."""
    ids = {n.get("id") for n in targets}

    def prune(n: dict) -> dict | None:
        if n.get("id") in ids:
            return n
        kids = [k for k in (prune(c) for c in n.get("children") or [] if isinstance(c, dict)) if k]
        if not kids:
            return None
        return dict(n, children=kids)
    return prune(tree) or dict(tree, children=[])


def areas_of(tree: dict) -> list[tuple[str, str]]:
    return qa.areas_from_tree(tree)


# ---------------------------------------------------------------- selecting the features to build

def select_targets(tree: dict, out: Path) -> list[dict]:
    """The features to build: those marked as modified, and the new ones. A new feature is recognised by its seed
    records: the marked features show the naming the evolution's pre-provisioned records share (a prefix such as
    EVO-), and a feature whose scenarios name records with that naming is part of the same evolution. Without such a
    naming, a feature whose quoted control names are mostly absent from the code counts as new."""
    nodes = atomics(tree)
    modified = [n for n in nodes if re.search(r"modified feature description", str(n.get("description") or ""), re.I)]
    seeds = [r.lower() for n in modified for r in record_names(givens([n])) if len(r) > 3]
    prefix = os.path.commonprefix(seeds) if seeds else ""
    if len(prefix) < 3:
        prefix = ""
    chosen = []
    for n in nodes:
        if n in modified:
            log(f"target {n.get('id')} {n.get('name', '')}: modified feature")
            chosen.append(n)
            continue
        recs = [r for r in record_names(givens([n])) if len(r) > 3]
        if prefix:
            alike = [r for r in recs if r.lower().startswith(prefix)]
            if recs and len(alike) >= max(1, len(recs) // 2):
                log(f"target {n.get('id')} {n.get('name', '')}: new, {len(alike)}/{len(recs)} of its records are {prefix!r}-named")
                chosen.append(n)
            continue
        names = [x for x in code_index.quoted_names(str(n.get("description") or "")) if not code_index._CODEY.match(x)]
        absent = [x for x in names if not code_index.locate(out, [x])]
        if names and len(absent) >= max(1, len(names) // 2):
            log(f"target {n.get('id')} {n.get('name', '')}: new, {len(absent)}/{len(names)} of its control names are not in the code")
            chosen.append(n)
    if prefix:
        log(f"evolution records are named with the prefix {prefix!r}")
    return chosen


# ---------------------------------------------------------------- edits

EDIT_FORMAT = """\
Output format (nothing else before or after the blocks, no explanations):
- To change an existing file, one or more edit blocks; SEARCH is copied verbatim from the file (enough lines to be
  unique, 3-30), REPLACE is what those lines become:
<<<EDIT path/from/app/root>>>
<<<SEARCH>>>
...exact existing lines...
<<<REPLACE>>>
...new lines...
<<<END EDIT>>>
- To add a file (or rewrite a short one entirely), a file block:
<<<FILE path/from/app/root>>>
...complete content...
<<<END FILE>>>
Edits are applied in order to the file as it then is: one edit per region, never two edits with the same or
overlapping SEARCH text (put everything that region needs into one REPLACE).
Keep edits minimal and local; never remove or rename existing controls, routes, functions or data fields that other
features use; add rather than restructure. Paths are relative to the app root (frontend/..., backend/...).
Edit only files whose content is shown under "Relevant source" (SEARCH is copied from there; the source index
lists names and line numbers, not source text). A file shown in full may instead be rewritten whole with a FILE
block when most of it changes (a router or route table gaining many entries); keep everything it had. A file not
shown can only be added whole with a FILE block, and
a new script must also be registered wherever the page and the build list scripts."""

EDIT_BLOCK = re.compile(r"<<<\s*EDIT\s+([^<>\n]+?)\s*>>>[ \t]*\n?(?:<<<\s*SEARCH\s*>>>[ \t]*\n?)?(.*?)\n?<<<\s*REPLACE\s*>>>[ \t]*\n?(.*?)\n?<<<\s*END\s*EDIT\s*>>>",
                        re.S)  # the SEARCH marker is optional: some replies put the search text right after the header


def _norm_ws(s: str) -> str:
    return "\n".join(l.rstrip() for l in s.strip("\n").splitlines())


_DECL = re.compile(r"^\s*(?:(?:const|let|var)\s+[\w$]+\s*=\s*require\(.*\)|import\s.*\sfrom\s.*);?\s*$")


_LEAKED_MARKER = re.compile(r"^[ \t]*<<<\s*(?:SEARCH|REPLACE|EDIT|FILE|END)\b.*\n?", re.M)


def strip_markers(text: str) -> str:
    """Block-marker lines that reached a source file (no source line begins with <<<)."""
    return _LEAKED_MARKER.sub("", text)


def dedupe_declarations(text: str) -> str:
    """Drop a second copy of an identical require/import line (two edits each added the same one; in JavaScript a
    repeated const is a syntax error that then costs fix rounds)."""
    seen, out = set(), []
    for line in text.splitlines(keepends=True):
        key = line.strip()
        if _DECL.match(line) and key in seen:
            continue
        seen.add(key)
        out.append(line)
    return "".join(out)


_BLOCK_START = re.compile(r"^<<<\s*(EDIT|FILE)\b", re.M)
_MARKER_LINE = re.compile(r"\n?^<<<\s*(?:SEARCH|REPLACE|EDIT|FILE|END)\b.*$", re.M)


def close_open_blocks(reply: str) -> str:
    """An edit block without its END marker before the next block starts (the model forgot it) would swallow the
    next block's header lines into its REPLACE text and write them into the file: close it first."""
    parts = _BLOCK_START.split(reply)  # text, kind, body, kind, body, ...
    out = [parts[0]]
    for i in range(1, len(parts), 2):
        kind, body = parts[i], parts[i + 1]
        end = "<<<END EDIT>>>" if kind == "EDIT" else "<<<END FILE>>>"
        if not re.search(r"<<<\s*END\s*" + kind + r"\s*>>>", body):
            body = body.rstrip("\n") + "\n" + end + "\n"
        out.append("<<<" + kind + body)
    return "".join(out)


def apply_edits(out: Path, reply: str, shown: set = frozenset()) -> tuple[list[str], list[str]]:
    """Apply the reply's edit and file blocks. Returns (changed paths, problems to tell the model)."""
    changed, problems = [], []
    # some replies HTML-escape the markers, some of them, and then the code inside too: the marker tokens are
    # normalised here, a block's text is unescaped only when its literal SEARCH is not in the file
    reply = reply.replace("&lt;&lt;&lt;", "<<<").replace("&gt;&gt;&gt;", ">>>")
    reply = close_open_blocks(reply)
    for m in EDIT_BLOCK.finditer(reply):
        rel = wholeapp.safe_rel(m.group(1))
        if not rel:
            problems.append(f"edit of {m.group(1)!r}: not a path under frontend/ or backend/")
            continue
        f = out / rel
        if not f.exists():
            problems.append(f"edit of {rel}: the file does not exist (use a FILE block to add it)")
            continue
        text = f.read_text(encoding="utf-8")
        search, replace = m.group(2), m.group(3)
        # a marker line inside the texts (a second SEARCH/REPLACE pair the model began without a new header) ends them
        search = _MARKER_LINE.split(search)[0]
        replace = _MARKER_LINE.split(replace)[0]
        if not search.strip():
            problems.append(f"edit of {rel}: empty SEARCH text (to add a file, use a FILE block; to insert, SEARCH a neighbouring line)")
            continue
        if search not in text and html.unescape(search) in text:
            search, replace = html.unescape(search), html.unescape(replace)
        n = text.count(search)
        if n == 1:
            text = text.replace(search, replace)
        else:
            # tolerate trailing-space, indentation and blank-line differences: match the non-blank lines, stripped
            lines = text.splitlines()
            want = [l.strip() for l in search.strip("\n").splitlines() if l.strip()]
            nonblank = [(i, l.strip()) for i, l in enumerate(lines) if l.strip()]
            hits = [k for k in range(len(nonblank) - len(want) + 1) if [s for _, s in nonblank[k:k + len(want)]] == want]
            span = len(want)  # non-blank file lines the match covers
            if not hits and want:
                # the same text split over different lines (SEARCH "module.exports = {\n data,\n ... };" against the
                # file's one-line "module.exports = { data, ... };"): compare with all whitespace collapsed
                flat = re.sub(r"\s+", " ", " ".join(want)).strip()
                found = []
                for k in range(len(nonblank)):
                    acc = ""
                    for j in range(k, min(len(nonblank), k + len(want) + 6)):
                        acc = (acc + " " + nonblank[j][1]).strip()
                        if len(acc) > len(flat):
                            break
                        if re.sub(r"\s+", " ", acc) == flat:
                            found.append((k, j - k + 1))
                            break
                if len(found) == 1:
                    hits, span = [found[0][0]], found[0][1]
            if not hits and len(want) == 1:
                # one line whose tokens the file has in another order ("{ data, load, nextId, save }" vs the file's
                # "{ data, load, save, nextId }"), when exactly one file line matches that way
                bag = sorted(re.findall(r"\w+|[^\s\w]", want[0]))
                same = [k for k, (_, s) in enumerate(nonblank) if sorted(re.findall(r"\w+|[^\s\w]", s)) == bag]
                if len(same) == 1:
                    hits = same
            if not hits and want:  # a single misquoted line too ("{ data, load, nextId, save }" vs the file's order)
                # last resort: a near-identical window (the model misquotes a name or a space); accepted only when
                # clearly unique, so a wrong region is never edited
                target = "\n".join(want)
                scored = sorted(((difflib.SequenceMatcher(None, target, "\n".join(s for _, s in nonblank[k:k + len(want)])).ratio(), k)
                                 for k in range(len(nonblank) - len(want) + 1)), reverse=True)
                if scored and scored[0][0] >= 0.92 and (len(scored) == 1 or scored[1][0] < 0.85):
                    hits = [scored[0][1]]
            if len(hits) == 1 and want:
                first, last = nonblank[hits[0]][0], nonblank[hits[0] + span - 1][0]
                indent = re.match(r"\s*", lines[first]).group(0)
                new_lines = [(indent + l.lstrip() if l.strip() else l) for l in replace.strip("\n").splitlines()]
                lines[first:last + 1] = new_lines
                text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
            else:
                problems.append(f"edit of {rel}: SEARCH text {'matches ' + str(max(n, len(hits))) + ' places' if (n or hits) else 'was not found'}; "
                                f"first line: {search.strip().splitlines()[0][:100]!r}")
                continue
        f.write_text(strip_markers(dedupe_declarations(text)), encoding="utf-8")
        changed.append(rel)
    files, _ = wholeapp.parse_blocks(reply)
    for rel, body in list(files.items()):
        if ("&lt;" in body or "&gt;" in body) and "<" not in body and ">" not in body:
            files[rel] = html.unescape(body)  # a whole file written with escaped code: entities, no raw brackets
        files[rel] = strip_markers(files[rel])
        safe = wholeapp.safe_rel(rel)
        existing = out / safe if safe else None
        # a long existing file rewritten whole without having been shown loses its implementation (the repair reply
        # rewrote 17 unreachable pages it had only seen summarised); a file shown in full may be rewritten (a router
        # or server that needs many registrations is cleaner whole than as colliding edits)
        if existing and existing.is_file() and safe not in shown \
                and len(existing.read_text(encoding="utf-8", errors="replace").splitlines()) > 80:
            problems.append(f"FILE block for {safe}: this file was not shown; change it with EDIT blocks, not a rewrite")
            files.pop(rel)
    for rel in wholeapp.write(out, files):
        changed.append(rel)
    return list(dict.fromkeys(changed)), problems


# ---------------------------------------------------------------- context for one feature

_STOP = {"through", "should", "display", "displays", "remains", "remain", "current", "selected", "existing", "visible",
         "scenario", "visitor", "unauthenticated", "provisions", "workbook", "worksheet", "refresh", "exactly", "another",
         "between", "without", "message", "original", "modified", "feature", "description", "requirement", "clicking", "clicks"}


def keywords(text: str) -> list[str]:
    """Stems (first 7 letters) of the longer words of a feature text, to match function names in the index."""
    words = {w.lower() for w in re.findall(r"[A-Za-z]{7,}", text)} - _STOP
    return sorted({w[:7] for w in words})


def files_by_keyword(index: dict, stems: list[str]) -> list[str]:
    """Files whose function names contain a feature's word stems, most matches first."""
    score = {}
    for rel, d in index.items():
        n = sum(1 for name, _ in d["functions"] if any(k in name.lower() for k in stems))
        if n:
            score[rel] = n
    return sorted(score, key=lambda r: -score[r])


_PATH_IN_TEXT = re.compile(r"(?:frontend/src|backend)/[\w./-]+\.(?:js|mjs|cjs|html|css)|(?<=['\"` /])/[\w./-]+\.js\b")


def files_mentioned(out: Path, text: str) -> list[str]:
    """Source files a problem report names (module paths in build and runtime errors), as app-relative paths."""
    out_rels = []
    for m in _PATH_IN_TEXT.findall(text):
        rel = m if m.startswith(("frontend/", "backend/")) else "frontend/src" + m
        if (out / rel).is_file() and rel not in out_rels:
            out_rels.append(rel)
    return out_rels


def context_for(out: Path, index: dict, names: list[str], always: list[str] = (), fallback: list[str] = (),
                stems: list[str] = ()) -> str:
    """The source the model needs for a feature: the files holding its names, in full while they fit, else windows.
    A new feature's names are not in the code yet: then the files holding the `fallback` names (the shared
    conventions' controls) and the file with the most interface strings (the UI shell) stand in."""
    hits = code_index.locate(out, names) if names else {}
    # the files a caller insists on (named in the problems being fixed, the seed module) come first: the budget
    # must not run out before them (a fix round could not rewrite store.js, which its problems named)
    ranked = [r for r in always if r in index] + [r for r in sorted(hits, key=lambda r: -len(hits[r])) if r not in always]
    for rel in files_by_keyword(index, stems)[:4]:  # the functions a feature's words name (recalculates -> recalculateWorkbook)
        if rel not in ranked:
            ranked.append(rel)
            hits.setdefault(rel, [])
    if len(ranked) < 2:
        more = code_index.locate(out, fallback) if fallback else {}
        shell = max(index, key=lambda r: len(index[r]["strings"]), default=None)
        for rel in sorted(more, key=lambda r: -len(more[r])) + ([shell] if shell else []):
            if rel not in ranked:
                ranked.append(rel)
                hits.setdefault(rel, more.get(rel, []))
    for rel in always:
        if rel in index and rel not in ranked:
            ranked.append(rel)
    parts, used = [], 0
    for rel in ranked:
        text = (out / rel).read_text(encoding="utf-8", errors="replace")
        if used + len(text) <= MAX_CONTEXT_CHARS:
            parts.append(f"<<<FILE {rel}>>>\n{text}\n<<<END FILE>>>")
            used += len(text)
        else:
            lines = text.splitlines()
            rows = sorted({n for n, _ in hits.get(rel, [])})
            spans, cur = [], None
            for r in rows:
                lo, hi = max(1, r - WINDOW), min(len(lines), r + WINDOW)
                if cur and lo <= cur[1] + 1:
                    cur[1] = hi
                else:
                    cur = [lo, hi]
                    spans.append(cur)
            body = "\n".join(f"--- lines {lo}-{hi} of {rel} ---\n" + "\n".join(lines[lo - 1:hi]) for lo, hi in spans[:6])
            if used + len(body) > MAX_CONTEXT_CHARS:
                break
            parts.append(f"<<<EXCERPT {rel} ({len(lines)} lines)>>>\n{body}\n<<<END EXCERPT>>>")
            used += len(body)
    return "\n\n".join(parts)


def seed_files(index: dict, record_hints: list[str], out: Path) -> list[str]:
    """Files that define the seed data: named seed*, holding a seed function, or holding the known seed records."""
    files = [r for r, d in index.items() if re.search(r"seed", r, re.I) or any(re.search(r"seed", n, re.I) for n, _ in d["functions"])]
    if not files and record_hints:
        hits = code_index.locate(out, record_hints)
        files = sorted(hits, key=lambda r: -len(hits[r]))[:2]
    return files


# ---------------------------------------------------------------- prompts

SYSTEM = (wholeapp.SYSTEM + "\n\nYou are extending an application that already exists and already passes tests for its "
          "current behaviour: change only what the requirement changes, keep everything else working.\n\n" + wholeapp.PLATFORM)

SEED_PROMPT = """\
The requirement's scenarios start from pre-provisioned records (their GIVEN clauses below). Add every record they
name to the application's seed data, exactly as stated: names, worksheets or members, cell values, formulas,
accounts with their emails and passwords, visibility and relationships. Records of different scenarios are separate
records. Keep the existing seed records.

GIVEN clauses
{givens}

Shared conventions of the requirement
{conventions}

Source index
{index}

Seed source
{sources}

{format}
"""

ACCESSIBILITY_RULES = '\nAccessible structure (standard ARIA practice; the tests find controls by role and accessible name):\n- Status and error messages (validation errors, "already exists", rejections) are also announced in ONE page-level\n  live region with role="alert" (keep a single alert region; inline field errors may stay next to their fields).\n- A menu is opened by a button with the menu\'s name ("Data", "View", "Edit", "Format", "Insert", "Account menu"...);\n  its entries are elements with role="menuitem" (or links inside an element with role="menu").\n- Dialogs have role="dialog" and an accessible name equal to their title; their fields have visible labels.\n- Grid cells have role="gridcell" and the cell coordinate (A1, B7...) as accessible name; an inline cell editor is\n  a textbox named "Edit <coordinate>"; selected cells expose aria-selected="true".\n- The global search is an <input type="search"> named "Search" inside the page header (banner); primary page\n  actions and entry links (for example "Sign in" when signed out) are also reachable inside the <main> region.\n- Tabs use role="tab" with aria-selected; choices in a list use role="option".\n'

FEATURE_GOAL = """\
Implement this feature in the existing application. If it has an "Original Feature Description", that behaviour
already exists in the code and only the "Modified Feature Description" is new; otherwise the feature is new and
must be added through the existing menus, pages and APIs. Every quoted name is the exact accessible name or label
the tests look up; messages are shown verbatim. State persists through the existing save path and survives a
reload. Seed records the scenarios name already exist. Keep every existing control, route and behaviour.

Accessible structure (standard ARIA practice; the tests find controls by role and accessible name):
- Status and error messages (validation errors, "already exists", rejections) are also announced in ONE page-level
  live region with role="alert" (keep a single alert region; inline field errors may stay next to their fields).
- A menu is opened by a button with the menu's name ("Data", "View", "Edit", "Format", "Insert", "Account menu"...);
  its entries are elements with role="menuitem" (or links inside an element with role="menu").
- Dialogs have role="dialog" and an accessible name equal to their title; their fields have visible labels.
- Grid cells have role="gridcell" and the cell coordinate (A1, B7...) as accessible name; an inline cell editor is
  a textbox named "Edit <coordinate>"; selected cells expose aria-selected="true".
- The global search is an <input type="search"> named "Search" inside the page header (banner); primary page
  actions and entry links (for example "Sign in" when signed out) are also reachable inside the <main> region.
- Tabs use role="tab" with aria-selected; choices in a list use role="option".

FEATURE
{feature}

Shared conventions of the requirement
{conventions}
"""

FIX_GOAL = """\
After the edits for the feature below, these problems were found. Fix them with further edits.

PROBLEMS
{problems}

FEATURE
{feature}
"""

FIX_PROMPT = """\
After your edits for the feature below, these problems were found. Fix them with further edits.

PROBLEMS
{problems}

FEATURE
{feature}

Relevant source (current state)
{sources}

{format}
"""


REPAIR_PROMPT = """\
The inherited application has modules that nothing reaches from its entry points: an earlier generation rewrote
the router, the API client, shared helpers or the server and left these pages, components and backend handlers
behind. The requirement's features use controls that exist only in those modules (listed below), so they must be
reachable again. Re-integrate the unreachable modules into the live application:
- register their pages in the live router and their handlers in the live server route table, next to the live ones
  (keep every live page, route and behaviour working);
- add to the providers the names the modules import that the providers no longer export (listed below), with the
  behaviour their callers expect, or adapt the modules to the live providers;
- make the pages reachable through the live navigation (header links, menus) as the requirement describes them;
- where an unreachable module and a live module serve the same page, the one holding more of the requirement's
  controls becomes the page the router shows (listed below with the counts); the other stays mounted only for the
  routes it alone provides. Do not merge the two into one file.

CONTROLS THE REQUIREMENT NAMES THAT EXIST ONLY IN UNREACHABLE MODULES
{only}

SAME PAGE IN TWO MODULES (requirement controls each holds)
{pairs}

UNREACHABLE MODULES (their current source; adapt them with EDIT blocks where their imports or calls no longer match
the live providers; never rewrite them)
{orphans}

NAMES THEY IMPORT THAT THEIR PROVIDERS LACK
{missing}

Source index (functions:line, routes, interface strings per file)
{index}

Live entry, router, shared and provider modules (current source)
{sources}

{format}
"""


FATAL = re.compile(r"did not serve|did not start|server process exited|build failed|npm (install|run) failed|Cannot find module|"
                   r"syntax error|home page: uncaught exception|lost most of the controls", re.I)
REPAIR_FIX_ROUNDS = 5  # cheap (about 70 s each) and the errors are precise


REPAIR_GOAL = """\
Re-integrate modules of this application that nothing reaches from its entry points: an earlier generation rewrote
the router, the API client, shared helpers or the server and left these pages, components and backend handlers
behind. The requirement's features use controls that exist only in those modules, so they must be reachable:
- register their pages in the live router and their handlers in the live server route table, next to the live ones
  (keep every live page, route and behaviour working);
- give the providers the names the modules import that they no longer export, with the behaviour the callers
  expect (or adapt the callers);
- the two generations pass request data in different shapes (a handler written for (req, res, body, params) or
  for URLSearchParams receives (req, res, matches) or a plain object from the live dispatcher): adapt at the
  registration point with one wrapper that converts, rather than editing each handler;
- the two generations may address the same records differently (page URLs /owner/name vs /name, API paths
  /api/x/:owner/:name vs /api/y/:name, records stored with or without an owner field): both shapes must resolve
  the same records, so a lookup accepts either and a link from one tree opens the page of the other; every
  seeded record must be reachable through both;
- make the pages reachable through the live navigation (header links, menus) as the requirement describes them;
- where an unreachable module and a live module serve the same page, the router shows the one holding more of the
  requirement's controls (listed with counts); the other stays mounted only for the routes it alone provides. The
  two are not merged."""


def _save_factory(prefix: str):
    replies = qa.STATE / "replies"
    replies.mkdir(parents=True, exist_ok=True)

    def save(label: str, prompt: str, reply: str) -> None:
        (replies / f"{prefix}_{label}.txt").write_text(prompt + "\n\n=== REPLY ===\n" + reply, encoding="utf-8")
    return save


def repair_phase(out: Path, index: dict, rendered: str, targets: list[dict], model, pw: Path | None) -> None:
    """Re-integrate modules that nothing reaches when the requirement's controls live only there: locate, then
    line-anchored edits (change.py), verified by the build check, the reachability analysis and the regression
    guard; up to REPAIR_FIX_ROUNDS further rounds with the remaining problems."""
    a = repair.analyse(out)
    if not a["orphans"]:
        return
    wanted = sorted({x for n in targets for x in code_index.quoted_names(node_text(n)) if not code_index._CODEY.match(x)})
    only = [x for x in wanted if (h := code_index.locate(out, [x])) and all(r in a["orphans"] for r in h)]
    log(f"repair: {len(a['orphans'])} unreachable modules; {len(only)} requirement controls exist only there"
        + (f": {', '.join(only[:10])}" if only else ""))
    if not only:
        return
    hits_by_file: dict = {}
    for x in wanted:
        for rel in code_index.locate(out, [x]):
            hits_by_file[rel] = hits_by_file.get(rel, 0) + 1
    dir_hits: dict = {}
    for rel, n in hits_by_file.items():
        dir_hits[str(Path(rel).parent)] = dir_hits.get(str(Path(rel).parent), 0) + n
    score = lambda rel: hits_by_file.get(rel, 0) + dir_hits.get(str(Path(rel).parent), 0)  # noqa: E731
    pairs = []
    for rel in a["orphans"]:
        for twin in [r for r in index if r not in a["orphans"] and Path(r).name == Path(rel).name and r != rel
                     and r.split("/")[0] == rel.split("/")[0]]:
            pairs.append(f"{rel} ({score(rel)} controls in it and its directory) and live {twin} ({score(twin)})"
                         + (f" -> show {rel}" if score(rel) > score(twin) else f" -> keep {twin}"))
    live = [e.relative_to(out).as_posix() for e in repair.entries(out)]
    for e in repair.entries(out):
        live += [m.relative_to(out).as_posix() for m, _, _ in repair.imports_of(e, out, out / "frontend" / "src") if m]
    live += [prov for lst in a["missing"].values() for prov, _ in lst]
    live = [r for r in dict.fromkeys(live) if r not in a["orphans"]]

    def material(still: dict, problems: str | None, left: list[str], unapplied: list[str]) -> str:
        parts = ["CONTROLS THE REQUIREMENT NAMES THAT EXIST ONLY IN UNREACHABLE MODULES\n" + "\n".join(left or only)]
        if pairs:
            parts.append("SAME PAGE IN TWO MODULES (requirement controls each holds)\n" + "\n".join(pairs))
        parts.append("UNREACHABLE MODULES (exports, imports, links)\n" + "\n".join(repair.summary(out, r) for r in still["orphans"])[:20000])
        lacking = [f"{rel} imports {', '.join(ns)} from {prov}" for rel, lst in still["missing"].items() for prov, ns in lst]
        if lacking:
            parts.append("IMPORTS WITHOUT A MATCHING EXPORT (each one fails at load time; fix every one now: import the names "
                         "the module exports, e.g. `import * as x` or `import { f }`, or add the export)\n" + "\n".join(lacking[:40]))
        if still.get("members"):
            parts.append("MEMBERS USED ON A REQUIRED MODULE THAT IT DOES NOT EXPORT\n" + "\n".join(still["members"][:30]))
        unserved = [l for l in unserved_calls(out) if l not in BASELINE]
        if unserved:
            parts.append("API PATHS THE FRONTEND CALLS THAT NO SERVER ROUTE SERVES (each fails at runtime with a 404: register a route "
                         "for it or point the call at the route that exists, translating the fields)\n" + "\n".join(unserved[:30]))
        if problems:
            parts.append("PROBLEMS AFTER THE LAST ROUND\n" + problems[:4000])
        if unapplied:
            parts.append("EDITS OF THE LAST ROUND THAT WERE NOT APPLIED\n" + "\n".join(unapplied[:12]))
        return "\n\n".join(parts)

    save = _save_factory("repair")
    changed, unapplied = change.ask_change(model, out, REPAIR_GOAL, rendered, material(a, None, only, []), log, save,
                                           extra_files=live[:6], wholeapp=wholeapp, controls=only)
    if unapplied:
        log(f"repair: {len(unapplied)} edit(s) not applied: " + " | ".join(p[:110] for p in unapplied[:4]))
    touched = set(changed)  # shown whole in later rounds: a router patched three times by function reads as three routers
    for round_ in range(REPAIR_FIX_ROUNDS + 1):
        problems = check(out, pw)
        still = repair.analyse(out)
        left = [x for x in only if (h := code_index.locate(out, [x])) and all(r in still["orphans"] for r in h)]
        log(f"repair: build {'ok' if problems is None else 'failed'}; {len(still['orphans'])} modules still unreachable; "
            f"{len(left)} controls still only there")
        if (problems is None and not left) or round_ == REPAIR_FIX_ROUNDS:
            break
        save = _save_factory(f"repair_fix_{round_ + 1}")
        extra = files_mentioned(out, problems or "") + live[:4]
        changed, unapplied = change.ask_change(model, out, REPAIR_GOAL, code_index.render(code_index.index_app(out)),
                                               material(still, problems, left, unapplied), log, save, extra_files=extra, wholeapp=wholeapp,
                                               controls=only, whole=touched)
        touched.update(changed)
        if unapplied:
            log(f"repair fix {round_ + 1}: {len(unapplied)} edit(s) not applied: " + " | ".join(p[:110] for p in unapplied[:4]))
    problems = check(out, pw)
    if problems is None or not FATAL.search(problems):
        commit(out, "evolution: unreachable modules re-integrated")
        if problems:
            log(f"repair kept with remaining problems:{chr(10)}{problems[:600]}")
            accept(problems)
    else:
        log(f"repair reverted (the app no longer builds or starts):{chr(10)}{problems[:600]}")
        revert(out)


def ask_edits(model, prompt: str, label: str, out: Path, think: bool = True) -> tuple[list[str], str]:
    shown = {wholeapp.safe_rel(p) for p in re.findall(r"<<<FILE ([^<>\n]+?)\s*>>>", prompt)}  # given in full
    replies = qa.STATE / "replies"
    replies.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        reply, finish = model.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}], label, think)
        (replies / (re.sub(r"[^\w.-]+", "_", label) + ("_retry" if attempt else "") + ".txt")).write_text(
            prompt + "\n\n=== REPLY ===\n" + reply, encoding="utf-8")
        changed, problems = apply_edits(out, reply, shown)
        wholeapp.ensure_manifests(out)
        if changed or problems:
            break
        # no block at all: the same prompt answered with 141k chars of edits once and with a four-second "I need
        # to see the actual files" the next time, so one more sample is worth more than any diagnosis
        log(f"{label}: the reply held no edit; it starts: {reply.strip()[:300]!r}" + ("" if attempt else "; asking again"))
    if problems:
        log(f"{label}: {len(problems)} edit(s) not applied: " + " | ".join(p[:120] for p in problems[:4]))
    return changed, "\n".join(problems)


# ---------------------------------------------------------------- build check relative to the inherited app

BASELINE: set = set()
BEFORE: dict = {}  # the inherited application's controls per page, taken before any change


def take_snapshot(out: Path, pw: Path | None) -> dict:
    if pw is None:
        return {}
    app = qa.App(out)
    try:
        if app.start():
            return {}
        known = [k for k in BEFORE if " #" not in k]  # a page nothing links to any more is opened, not counted lost
        data = snapshot.take(qa._node, pw, f"http://127.0.0.1:{qa.QA_PORT}", known)
        CRASH.clear()
        if app.proc is not None and app.proc.poll() is not None:  # a handler threw synchronously: every later request is refused
            CRASH.append("the server process exited while the pages were opened (every test after that fails with connection "
                         "refused); its output:\n" + (qa.STATE / "server.log").read_text(errors="replace")[-1500:])
        return data
    finally:
        app.stop()


CRASH: list[str] = []  # set by take_snapshot when the server died under it


def regressions(out: Path, pw: Path | None) -> list[str]:
    """Controls the inherited application showed that a change removed (a rewritten router whose home route now
    renders "Not found" passes the build check; this does not)."""
    if not BEFORE:
        return []
    after = take_snapshot(out, pw)
    if not after:
        return list(CRASH)
    known_dups = set(snapshot.duplicates(BEFORE))  # the inherited app's own duplicates are not a regression
    lines = CRASH + snapshot.lost(BEFORE, after) + [l for l in snapshot.duplicates(after) if l not in known_dups]
    if snapshot.fatal(BEFORE, after):
        lines.insert(0, "the home page lost most of the controls the inherited application showed")
    for name in MAIN_CONTENT_LINKS:
        if after.get("/ #mainLinks") is not None and name not in after.get("/ #mainLinks"):
            lines.append(f'the home page has no link "{name}" inside its <main> region: the requirement locates the visible '
                         f'link named "{name}" in the main content (signed-out visitors start there)')
    if REQUIRE_BANNER_SEARCH and after.get("/ #bannerSearch") is False:
        lines.append('the home page has no global search box in its header: the requirement names a searchbox "Search"; '
                     'it is an <input type="search"> with the accessible name "Search" inside the page header (banner), '
                     'present on every page (opening a repository or record starts from it)')
    return lines


REQUIRE_BANNER_SEARCH = False  # set when the requirement names a searchbox "Search"
MAIN_CONTENT_LINKS: list = []  # links the requirement says are in the main content of the home page


KNOWN: set = set()  # problem lines an earlier phase was kept with: not charged to later phases (the cleanup pass takes them)
FOCUS = True


def accept(problems: str | None) -> None:
    if problems:
        KNOWN.update(l.strip() for l in problems.splitlines() if l.strip())


def unserved_calls(out: Path, reachable_only: bool = False) -> list[str]:
    """Frontend API calls no server route serves (graph.py); empty when the graph cannot be built. The inherited
    baseline takes only the reachable modules' calls: an unreachable module calling a route of the tree that
    replaced its server is what the repair phase exists to fix."""
    try:
        import graph  # noqa: PLC0415
        lines = graph.Graph(out).unserved_calls()
        if reachable_only:
            reachable = set(repair.analyse(out)["reachable"])
            lines = [l for l in lines if l.split(":", 1)[0] in reachable]
        return lines
    except Exception as e:  # noqa: BLE001
        log(f"graph: {e}")
        return []


def baseline(out: Path, pw: Path | None) -> None:
    """The inherited application's own problems: not charged to any phase."""
    base = wholeapp.check(out, pw)
    if base:
        BASELINE.update(l.strip() for l in base.splitlines() if l.strip())
    BASELINE.update(repair.module_style_mismatch(out))
    BASELINE.update(unserved_calls(out, reachable_only=True))
    if BASELINE:
        log(f"the inherited application already has {len(BASELINE)} problem(s), not counted against the features:\n"
            + "\n".join(sorted(BASELINE))[:600])


def check(out: Path, pw: Path | None) -> str | None:
    """wholeapp.check, minus the problems the inherited application already had (a github stage-3 app failed
    the probe on arrival, so every feature was reverted as "broken"), plus the controls a change removed; a server
    that does not start always counts."""
    problems = wholeapp.check(out, pw)
    new = [] if problems is None else [l for l in problems.splitlines() if l.strip() and l.strip() not in BASELINE]
    new += [l for l in repair.module_style_mismatch(out) if l not in BASELINE]  # names the file a syntax error hides
    new += [l for l in unserved_calls(out) if l not in BASELINE]
    if problems is None or not FATAL.search(problems):
        new += regressions(out, pw)
    if FOCUS:
        new = [l for l in new if l.strip() not in KNOWN]
    new = [l for l in new if l.strip()]
    return "\n".join(new) or None


# ---------------------------------------------------------------- git

def git(out: Path, *args: str) -> tuple[int, str]:
    r = subprocess.run(["git", "-c", "user.email=agent@local", "-c", "user.name=evo", *args], cwd=out,
                       capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def commit(out: Path, message: str) -> None:
    git(out, "add", "-A")
    git(out, "commit", "-qm", message)


def revert(out: Path) -> None:
    git(out, "checkout", "--", ".")
    git(out, "clean", "-fdq", "--", "frontend", "backend")


# ---------------------------------------------------------------- feature tests

def feature_spec(node: dict, conventions: str, requirement: str, model) -> Path | None:
    mini = {"id": "ROOT", "description": conventions, "children": [node]}
    text = structured_tests.generate_from_scenarios(mini, requirement, qa._chat, qa.log,
                                                    qa.requirement_name_patterns(requirement) + qa.EXTRA_NAME_PATTERNS)
    if not text:
        return None
    path = qa.STATE / "tests" / f"{node.get('id')}.spec.ts"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def failure_title(line: str) -> str:
    m = re.search(r'common-path test "([^"]+)"', line)
    return m.group(1) if m else line[:120]


def run_spec(out: Path, pw: Path | None, spec: Path | None, requirement: str) -> list[str]:
    if spec is None or pw is None:
        return []
    app = qa.App(out)
    try:
        if (err := app.start()):
            return [err]
        return qa.Checks(app, pw, [], spec, requirement).spec_problems()
    finally:
        app.stop()


# ---------------------------------------------------------------- main

LOG_BLOCK = re.compile(r"// Request and memory log to stdout, added by the agent.*?\n  \} \}\n", re.S)


def prepare(out: Path) -> None:
    for f in (out / "backend").glob("data.json*"):
        f.unlink()
        log(f"removed {f.name} left by an earlier run")
    qa.FRESH_DATA = True  # from here on the data file is the server's own: dropped before every start, re-seeded
    server = out / "backend" / "server.js"
    if server.exists():
        text, n = LOG_BLOCK.subn("", server.read_text(encoding="utf-8"))
        if n:
            server.write_text(text, encoding="utf-8")
            log("removed the injected request log from backend/server.js")
    for part in ("frontend", "backend"):
        shutil.rmtree(out / part / "node_modules", ignore_errors=True)
    shutil.rmtree(out / "frontend" / "dist", ignore_errors=True)
    wholeapp.ensure_manifests(out)  # package.json files the platform contract requires (a local copy had lost them)
    if not (out / ".git").exists():
        git(out, "init", "-q")
    commit(out, "evolution: inherited application")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("requirement_path", nargs="?", default=os.environ.get("ARCBENCH_REQUIREMENTS_DIR", "."))
    ap.add_argument("--output-dir", default=None)
    args, _ = ap.parse_known_args()
    out = Path(args.output_dir or os.environ.get("ARCBENCH_TEMPLATE_DIR") or "workspace/app").resolve()
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    deadline = started + TOTAL_MINUTES * 60
    whole, _, tree = wholeapp.requirement(Path(args.requirement_path).resolve())
    if tree is None:
        sys.exit("the requirement tree (requirements.yaml) is needed")
    progress = wholeapp.Progress(out, tree)
    progress.call("mark_run_started", "evolution agent started")
    model = wholeapp.Model()
    model.effort = "high"  # inputs are small since the two-step edit (locate, then excerpts); "low" was set when whole files were sent
    pw = wholeapp.playwright_root()
    conventions = " ".join(str(tree.get("description") or "").split())
    log(f"requirement: {len(whole)} chars; playwright: {pw or 'not found'}; output {out}")

    lines = wholeapp.existing_app_lines(out)
    log(f"inherited application: {lines} source lines")
    if lines < 300:
        log("no inherited application: building from scratch with the whole-app agent")
        sys.argv = [sys.argv[0], args.requirement_path, "--output-dir", str(out)]
        wholeapp.main()
        return
    prepare(out)
    baseline(out, pw)
    global REQUIRE_BANNER_SEARCH, MAIN_CONTENT_LINKS
    MAIN_CONTENT_LINKS = sorted(set(re.findall(r"link named [\"“]([^\"”]{1,40})[\"”] in the main content", whole)))
    if MAIN_CONTENT_LINKS:
        log(f"links the requirement places in the main content: {MAIN_CONTENT_LINKS}")
    REQUIRE_BANNER_SEARCH = bool(re.search(r"searchbox", whole, re.I) and re.search(r"[\"“]Search[\"”]", whole))
    if REQUIRE_BANNER_SEARCH:
        log("the requirement names a searchbox \"Search\": the header search box is checked on every build")
    BEFORE.update(take_snapshot(out, pw))
    pages = {k: v for k, v in BEFORE.items() if " #" not in k and isinstance(v, list)}  # page entries only (flags too)
    log(f"inherited controls: {sum(len(v) for v in pages.values())} on {len(pages)} pages (kept as the regression guard)")
    index = code_index.index_app(out)
    rendered = code_index.render(index)
    log(f"index: {len(index)} files, {len(rendered)} chars")
    qa.EXTRA_NAME_PATTERNS = code_index.name_patterns(index)  # the inherited app's controls are known names

    targets = select_targets(tree, out)
    log(f"{len(targets)} target feature(s) of {len(atomics(tree))}")
    if not targets:
        targets = atomics(tree)
    focus = focus_tree(tree, targets)
    focus_text = conventions + "\n\n" + "\n\n".join(node_text(n) for n in targets)
    qa.start_audit(focus, focus_text)  # model-written requirement checks for QA, in the background
    try:
        repair_phase(out, index, rendered, targets, model, pw)  # pages an earlier generation left unreachable
    except Exception as e:  # noqa: BLE001
        log(f"repair stopped by an error ({type(e).__name__}: {str(e)[:200]}); reverting to the last commit")
        revert(out)
    index = code_index.index_app(out)
    rendered = code_index.render(index)

    # seeds
    try:
        names = record_names(givens(targets))
        files = seed_files(index, names, out)
        log(f"seed records named by the scenarios: {len(names)}; seed files: {files}")
        gv = "\n".join(givens(targets))
        missing: list[str] = []
        for attempt in range(2):
            srcs = "\n\n".join(f"<<<FILE {r}>>>\n{(out / r).read_text(encoding='utf-8', errors='replace')}\n<<<END FILE>>>" for r in files)
            prompt = SEED_PROMPT.format(givens=gv, conventions=conventions, index=rendered, sources=srcs, format=EDIT_FORMAT)
            if attempt:
                prompt += f"\n\nThese records are still missing from the seed source: {', '.join(missing[:40])}"
            changed, _ = ask_edits(model, prompt, "seeds" + (" (again)" if attempt else ""), out)
            files = list(dict.fromkeys(files + [c for c in changed if c.startswith("backend/")]))
            text = "\n".join((out / r).read_text(encoding="utf-8", errors="replace") for r in files if (out / r).exists())
            missing = [n for n in names if not present(n, text)]
            log(f"seeds: {len(names) - len(missing)}/{len(names)} named records present" + (f"; missing {missing[:8]}" if missing else ""))
            if not missing:
                break
        problems = check(out, pw)
        for round_ in range(FIX_ROUNDS):
            if problems is None:
                break
            log(f"seeds broke the build/start:\n{problems[:800]}")
            if "more than once" in problems:  # a record seeded twice: the tests open records by their exact name
                problems += ("\n\nA control name appearing more than once on the home page means a seed record was added "
                             "twice (or added again although it already existed). Every named record must exist exactly once: "
                             "remove the duplicate entries from the seed data.")
            srcs = context_for(out, index, [], always=files)
            ask_edits(model, FIX_PROMPT.format(problems=problems[:4000], feature="seed data", sources=srcs, format=EDIT_FORMAT), f"seed fix {round_ + 1}", out)
            problems = check(out, pw)
        if problems is None or not FATAL.search(problems):
            commit(out, "evolution: seed records")
            if problems:
                log(f"seeds kept with remaining problems:{chr(10)}{problems[:400]}")
                accept(problems)
        else:
            log("seed edits reverted (the app no longer builds or starts)")
            revert(out)

        # features
    except Exception as e:  # noqa: BLE001
        log(f"seed step stopped by an error ({type(e).__name__}: {str(e)[:200]}); reverting to the last commit")
        revert(out)

    limit = int(os.environ.get("EVO_MAX_FEATURES", "0") or 0)  # local experiments: only the first N features
    kept_specs: list[tuple[str, Path, set]] = []  # (feature, its scenario spec, the titles failing when it was kept)
    only = {x.strip() for x in os.environ.get("EVO_ONLY", "").split(",") if x.strip()}  # local experiments: chosen features
    # new features first: when time runs short, the skipped ones are the modified features, which the inherited
    # application partly covers already (v4v: four new features skipped out of time)
    targets = sorted(targets, key=lambda t: 0 if "Original Feature Description" not in node_text(t) else 1)
    for n in [t for t in (targets[:limit] if limit else targets) if not only or t.get("id") in only]:
        try:
            fid = n.get("id")
            if time.time() > deadline - 20 * 60:
                log(f"{fid}: skipped, out of time")
                continue
            progress.nodes("mark_implementation_started", [fid], f"evolving {fid}")
            feature = node_text(n)
            qn = [x for x in code_index.quoted_names(feature) if not code_index._CODEY.match(x) and x not in names]
            log(f"{fid}: {len(qn)} quoted names")
            # two steps (change.py): locate by the index and the code paths behind the feature's controls, then
            # edit numbered excerpts of the located functions by line number
            octos = None
            if os.environ.get("EVO_OCTOS", "1") == "1" and "Original Feature Description" not in feature:
                import octos_feature  # noqa: PLC0415  # a new feature: the Octos kernel's own tool loop implements it
                try:
                    octos = octos_feature.Implementer(out, log)
                except Exception as e:  # noqa: BLE001
                    log(f"{fid}: octos did not start ({type(e).__name__}: {str(e)[:160]}); using the two-step edit")
            if octos is not None and not octos.turn(octos_feature.PROMPT.format(feature=feature, conventions=conventions), fid):
                log(f"{fid}: octos did not finish; the two-step edit takes over")  # 3: fallback after a failed turn
                octos.close()
                octos = None
            if octos is not None:
                changed, not_applied = [], []
            else:
                changed, not_applied = change.ask_change(model, out, FEATURE_GOAL.format(feature=feature, conventions=conventions),
                                                         rendered, "", log, _save_factory(f"feature_{fid}"), extra_files=files[:1],
                                                         wholeapp=wholeapp, controls=qn)
            unapplied = "\n".join(not_applied)
            touched = set(changed)  # shown whole in the fix rounds
            feature_deadline = time.time() + FEATURE_MINUTES * 60  # fix rounds; the first reply may wait out empty streams
            spec = None
            for round_ in range(FIX_ROUNDS + 1):
                problems = check(out, pw)
                if problems is None or not FATAL.search(problems):
                    # the app runs: the feature's own tests decide; page-level problems (a 404 left by the seed step)
                    # are added for the fix rounds but do not block a feature whose tests pass
                    nonfatal = problems or ""
                    if spec is None:
                        spec = feature_spec(n, conventions, focus_text, model)
                    failures = run_spec(out, pw, spec, focus_text)
                    log(f"{fid}: app runs; scenario tests failing: {len(failures)}" + ("" if spec else " (no test kept)")
                        + (f"; other problems: {len(nonfatal.splitlines())}" if nonfatal else ""))
                    problems = "\n".join(failures)
                    if unapplied:  # a refused edit left part of the feature unimplemented
                        problems = "These edits were not applied (use line numbers inside the shown excerpts; FILE blocks only for new files):\n" + unapplied \
                            + ("\n\n" + problems if problems else "")
                    if not problems and (spec is not None or not nonfatal):
                        break
                    problems = (problems + "\n\n" + nonfatal).strip() if nonfatal else problems
                else:
                    log(f"{fid}: build/start failed:\n{problems[:600]}")
                if round_ == FIX_ROUNDS or time.time() > feature_deadline:
                    break
                index = code_index.index_app(out)
                if octos is not None and not octos.turn(octos_feature.FIX_PROMPT.format(problems=problems[:5000]), f"{fid} fix {round_ + 1}"):
                    log(f"{fid}: octos did not finish the fix; the two-step edit takes over")
                    octos.close()
                    octos = None
                if octos is not None:
                    changed, not_applied = [], []
                else:
                    changed, not_applied = change.ask_change(model, out, FIX_GOAL.format(problems=problems[:5000], feature=feature),
                                                             code_index.render(index), "", log, _save_factory(f"fix_{fid}_{round_ + 1}"),
                                                             extra_files=files_mentioned(out, problems) + files[:1], wholeapp=wholeapp,
                                                             controls=qn, whole=touched)
                touched.update(changed)
                unapplied = "\n".join(not_applied)
            # a modified feature whose two-step rounds left scenario tests failing gets an Octos session too: its own
            # tool loop can read the code behind a runtime error and rebuild (it only adds to what the rounds did)
            if octos is None and spec is not None and problems and os.environ.get("EVO_OCTOS_AFTER_TWO_STEP") == "1" \
                    and time.time() < deadline - 25 * 60:
                import octos_feature  # noqa: PLC0415
                try:
                    octos = octos_feature.Implementer(out, log)
                    log(f"{fid}: scenario tests still failing after the two-step rounds; octos takes a turn")
                    if octos.turn(octos_feature.PROMPT.format(feature=feature, conventions=conventions)
                                  + "\nThe feature is partly implemented already. These scenario tests still fail:\n" + problems[:4000],
                                  f"{fid} (octos after two-step)"):
                        log(f"{fid}: scenario tests failing after octos: {len(run_spec(out, pw, spec, focus_text))}")
                except Exception as e:  # noqa: BLE001
                    log(f"{fid}: octos turn after the two-step rounds failed ({type(e).__name__}: {str(e)[:160]})")
            if octos is not None:
                octos.close()
            final = check(out, pw)
            # 1: a feature may not break the scenario tests of the features kept before it (a later change emptied
            # the grid in a platform run and three passing tests of an earlier feature were lost)
            regressed = []
            if final is None or not FATAL.search(final):
                for pfid, pspec, pfail in kept_specs:
                    now_fail = {failure_title(x) for x in run_spec(out, pw, pspec, focus_text)}
                    broken = now_fail - pfail
                    if broken:
                        regressed.append(f"{pfid}: {', '.join(sorted(broken))[:200]}")
            if regressed:
                log(f"{fid}: reverted, it broke earlier features' scenario tests: " + " | ".join(regressed)[:500])
                revert(out)
                progress.nodes("mark_implementation_failed", [fid], f"{fid} reverted (regression)")
            elif final is None or not FATAL.search(final):
                commit(out, f"evolution: {fid}")
                if spec is not None:
                    kept_specs.append((fid, spec, {failure_title(x) for x in run_spec(out, pw, spec, focus_text)}))
                if final:
                    log(f"{fid}: kept with remaining problems:{chr(10)}{final[:400]}")
                    accept(final)
                progress.nodes("mark_implementation_done", [fid], f"{fid} built")
                index = code_index.index_app(out)
                rendered = code_index.render(index)
            else:
                log(f"{fid}: reverted, the app no longer builds or starts")
                revert(out)
                progress.nodes("mark_implementation_failed", [fid], f"{fid} reverted")

        except Exception as e:  # noqa: BLE001
            log(f"{n.get('id')}: stopped by an error ({type(e).__name__}: {str(e)[:200]}); reverting to the last commit")
            revert(out)

    # cleanup: every problem the phases were kept with, in one place, up to three rounds
    global FOCUS
    FOCUS = False
    try:
        for round_ in range(3):
            problems = check(out, pw)
            if problems is None or time.time() > deadline - 15 * 60:
                break
            log(f"cleanup round {round_ + 1}: {len(problems.splitlines())} problem(s) remain")
            change.ask_change(model, out, FIX_GOAL.format(problems=problems[:5000], feature="the problems the earlier steps left"),
                              code_index.render(code_index.index_app(out)), "", log, _save_factory(f"cleanup_{round_ + 1}"),
                              extra_files=files_mentioned(out, problems) + files[:1], wholeapp=wholeapp)
            after = check(out, pw)
            if after is None or not FATAL.search(after):
                commit(out, f"evolution: cleanup {round_ + 1}")
            else:
                log("cleanup reverted (the app no longer builds or starts)")
                revert(out)
                break
    except Exception as e:  # noqa: BLE001
        log(f"cleanup stopped by an error ({type(e).__name__}: {str(e)[:200]}); reverting to the last commit")
        revert(out)

    # QA on the target features
    qa.SKIP_SEED_PASSWORDS = True  # inherited accounts have passwords this requirement does not state
    if not os.environ.get("EVO_SKIP_QA"):  # the closing QA audit stays on (its effect on the score is not measured)
        qa.run_qa(out, focus, focus_text, pw, min(deadline, time.time() + qa.QA_SECONDS))

    shutil.rmtree(out / "frontend" / "dist", ignore_errors=True)
    for part in ("frontend", "backend"):
        shutil.rmtree(out / part / "node_modules", ignore_errors=True)
    commit(out, "evolution agent")
    progress.call("mark_run_completed", "evolution agent finished")
    t = model.tokens
    log(f"done in {int(time.time() - started)}s; requests {t['requests']}, prompt {t['prompt']} (cached {t['cached']}), completion {t['completion']}")


if __name__ == "__main__":
    main()
