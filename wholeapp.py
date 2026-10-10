#!/usr/bin/env python3
"""Whole-app agent: design once, build by requirement area, review each area, then check
the running app in a real browser and fix what breaks.

The node-by-node harness (arc/) scored 3-5 per task across v3-v7: each version broke some
step every hidden test shares, and nothing in it used the app the way the tests do. This
agent works on the whole application at once: one design, one foundation, one request per
top-level requirement area (the area sees the whole current app), one review per area
against its requirement text, then deterministic build/start/asset checks and a browser
probe feed up to FIX_ROUNDS repair requests.

Environment: OPENAI_BASE_URL, OPENAI_API_KEY, MODEL (the platform sets them).
Optional: OCTOS_ARC_PLAYWRIGHT_ROOT (a dir holding node_modules/@playwright/test).
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import qa
from pathlib import Path

MAX_OUTPUT = 65536
CONTINUATIONS = 3
FIX_ROUNDS = 3
CHECK_PORT = 34567
IDLE_TIMEOUT = 120  # seconds without any streamed data before a request is retried
STATE_DIR = Path(tempfile.mkdtemp(prefix="oneshot-"))


def log(msg: str) -> None:
    print(f"[oneshot] {msg}", flush=True)


# ---------------------------------------------------------------- requirement

def load_tree(req_dir: Path) -> dict | None:
    f = req_dir / "requirements.yaml"
    if not f.is_file():
        return None
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        return None
    data = yaml.safe_load(f.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "id" not in data:
        for v in data.values():
            if isinstance(v, dict) and "id" in v:
                return v
    return data if isinstance(data, dict) else None


# "all": every distinct scenario step. "given": only the GIVEN steps (the seed data); the WHEN/THEN lines are
# filled templates ("the requested workflow sheet2,the requested workflow") and about half the requirement text.
SCENARIO_STEPS = "all"


def digest(root: dict, context: list[dict] | None = None) -> str:
    """A subtree as text: enclosing folders, every node's id, name and description, then each
    distinct scenario step once (steps repeat heavily and carry the seed data)."""
    nodes, steps = [], {}

    def walk(node: dict, depth: int) -> None:
        nodes.append((depth, node))
        for sc in node.get("scenarios") or []:
            for step in (sc.get("steps") or []) if isinstance(sc, dict) else []:
                if isinstance(step, dict):
                    if SCENARIO_STEPS == "given" and str(step.get("keyword", "")).strip().upper() != "GIVEN":
                        continue
                    t = " ".join(f"{step.get('keyword', '')} {step.get('content', '')}".split())
                    if SCENARIO_STEPS == "given":
                        t = t.replace("The visitor starts at the application home page in a fresh unauthenticated "
                                      "browser session. ", "")
                    if t:
                        steps[t] = steps.get(t, 0) + 1
        for child in node.get("children") or []:
            if isinstance(child, dict):
                walk(child, depth + 1)

    walk(root, 0)
    lines = []
    for anc in context or []:
        lines.append(f"(within {anc.get('id')} {anc.get('name', '')}: {' '.join(str(anc.get('description') or '').split())})")
    lines.append("Requirement tree:")
    for depth, node in nodes:
        deps = node.get("dependencies") or []
        desc = " ".join(str(node.get("description") or "").split())
        lines.append(f"{'  ' * depth}- {node.get('id')} {node.get('name', '')} [{node.get('type', '')}]"
                     + (f" (depends on {', '.join(map(str, deps))})" if deps else "") + (f": {desc}" if desc else ""))
    if steps:
        lines.append("\nSeed data the scenarios describe (each distinct GIVEN once; different features describe "
                     "different seeds, so keep the records they share):" if SCENARIO_STEPS == "given" else
                     "\nScenario steps (each distinct step once, most frequent first):")
        lines += [f"- {s}" for s, _ in sorted(steps.items(), key=lambda kv: -kv[1])]
    return "\n".join(lines) + "\n"


def atomic_ids(node: dict) -> list[str]:
    kids = [c for c in node.get("children") or [] if isinstance(c, dict)]
    if not kids:
        return [str(node.get("id"))]
    return [i for c in kids for i in atomic_ids(c)]


def requirement(req_path: Path) -> tuple[str, list[tuple[str, str, list[str]]], dict | None]:
    """(whole requirement text, [(area label, area text, atomic node ids)], tree)."""
    req_dir = req_path if req_path.is_dir() else req_path.parent
    tree = load_tree(req_dir)
    if tree:
        areas = [c for c in tree.get("children") or [] if isinstance(c, dict)]
        if not areas:
            areas = [tree]
        return (digest(tree), [(f"{a.get('id')} {a.get('name', '')}".strip(), digest(a, [tree]), atomic_ids(a))
                               for a in areas], tree)
    for name in ("requirements.md", "README.md"):
        if (req_dir / name).is_file():
            text = (req_dir / name).read_text(encoding="utf-8")
            return text, [("whole requirement", text, [])], None
    if req_path.is_file():
        text = req_path.read_text(encoding="utf-8")
        return text, [("whole requirement", text, [])], None
    sys.exit(f"no requirement found at {req_path}")


# ---------------------------------------------------------------- prompts

SYSTEM = ("You are a senior full-stack engineer building a web application that automated browser tests will "
          "grade against a written requirement. You are exact about names, roles and messages, and you write "
          "complete, working code.")

PLATFORM = """\
Platform contract (mandatory):
- frontend/package.json: {"name":"frontend","private":true,"scripts":{"build":"node build.js"}} with NO dependencies; \
frontend/build.js copies frontend/src/* recursively into frontend/dist/ using only Node's fs and path.
- frontend/src/: plain HTML, CSS and browser JavaScript (ES modules allowed); no frameworks, bundlers or CDNs.
- backend/package.json: {"name":"backend","private":true,"scripts":{"start":"node server.js"}} with NO dependencies; \
the backend uses only Node built-ins (http, fs, path, url, crypto). It may be split into several modules.
- The server listens on process.env.PORT || 3000, serves ../frontend/dist with correct content types, answers \
/api/* as JSON, and for every other GET path returns index.html, so each in-app address opens or refreshes directly. \
Unknown API paths return 404 JSON; every request gets an answer.
- Pages reference scripts and stylesheets with absolute paths (src="/app.js"), so they load on nested addresses too.
- Data lives in backend/data.json. At startup create it with the seed only when it does not exist; never overwrite \
existing data. Everything that must survive a refresh or reopening is stored on the server, not in browser storage.
"""

QUALITY = """\
Quality rules (the tests drive the app with Playwright, step by step, exactly as the scenarios describe):
- Copy every accessible name, label, button text, heading, message, ARIA role and ARIA attribute from the requirement \
verbatim. Use native elements where possible (button, a, input, select, label[for]); names built from data follow \
the stated pattern exactly.
- Seed: provide exactly the records the scenarios name that every feature needs to exist (for example the named workbook, accounts, repositories); where scenarios of different features describe conflicting seed contents, tests enter those values themselves through labelled controls, so do NOT spread the variants over extra worksheets, records or files, and never add records, worksheets or sample data the requirement does not name.
- Values the tests type or check (names, credentials, messages, labels, defaults) come only from the requirement text: \
never invent one the tests cannot know, and where the requirement gives a value for one record of a kind, reuse it for \
the others of that kind instead of making up a new one.
- Never nest an interactive control inside another (no button inside a tab, link or button). Each dialog is exactly one \
element with role="dialog" and its aria-label; closed dialogs and menus are display:none and never cover the page.
- Complete stylesheet: every interactive element and every grid or table cell has a visible, non-zero size even when \
empty. Surfaces users fill in (grids, lists) render empty, clickable targets beyond the existing data.
- Attach listeners once, by delegation, on containers that are never replaced. Selection, focus and hover update \
existing elements in place; never rebuild the page or grid on mousedown, click, dblclick or after a save response, so \
editors, menus and dialogs opened by a click stay open.
- Focus follows the user: after Enter commits a text box (a formula bar or field editor), keyboard focus returns to \
the selected cell or record, and clicking a cell or record moves focus to it, so keyboard shortcuts act on that \
selection and any box that mirrors it shows the clicked item.
- The text of a cell, list item or option is exactly the value it shows: draw arrows, ticks and other markers with \
CSS (::after, background-image), and give a control placed inside it an aria-label and no text of its own, since a \
child's text (e.g. a "▾" button) becomes part of the cell's text.
- Speed: the tests run on one slow, shared CPU core and wait at most 5 seconds per step. Keep every page under about \
5000 elements: a grid or list renders about 100 rows unless the requirement states more (or only the visible part), \
and after an edit only the changed elements update, never a whole view.
- Tests act immediately: a click selects or focuses at once in local state, the next keystroke goes to that target, and \
a server response that arrives later never overwrites a value the user is typing or moves the selection back.
- Every edit path commits through one save function; Enter in a labelled input saves and persists. Keyboard shortcuts \
compare event.key case-insensitively and accept Ctrl or Meta; native editing inside text inputs is left alone.
- Wait for an earlier request before an action that depends on it. Validation and error messages are visible text with \
the exact wording required, shown next to the control, and a failed operation changes nothing.
- No placeholders, stubs or "not implemented" branches: implement every behaviour the requirement states.
"""

FORMAT = """\
Output format: one block per file you create or change, each file complete, nothing before, between or after the blocks:
<<<FILE relative/path>>>
file contents
<<<END FILE>>>
Paths start with frontend/ or backend/. Keep every file under about 40000 characters; split large areas into more files.
"""

DESIGN_PROMPT = """\
Design the complete web application below before any code is written. Later requests build it area by area and \
each receives this design verbatim, so it is what keeps the parts consistent. Be exact and complete; invent nothing \
the requirement does not imply.

{platform}
REQUIREMENT
{requirement}

Write plain Markdown with these sections:
## Pages and routes - every page/view, its address, how a user reaches it through visible controls.
## Controls - per page: every control, dialog, menu, region, tab, grid and message the requirement names, with ARIA \
role and exact accessible name or text (patterns for data-derived names), and what opens each one.
## Seed data - a table with one row per seeded record: | kind | values tests may type or see (names, username, \
email, password, cell values) | requirement quote |. Include only records the requirement names or every scenario \
needs to exist; each value is copied from the quote in that row, and a field the requirement leaves open for a \
record reuses the value it gives for another record of that kind (say which) instead of an invented one. Where \
features describe conflicting seed contents, keep the shared records only: tests enter the rest themselves.
## Data model and API - collections and fields; every HTTP endpoint with method, path, request, response, errors.
## Frontend architecture - shared state, the one save function, routing, rendering rules (delegated listeners, \
in-place updates, no re-render on click or after save), shared helpers with their exact function signatures.
## Files - every file and what it contains; which files belong to which requirement area ({areas}); which are shared.
"""

FOUNDATION_PROMPT = """\
Build the foundation of the application designed below: the platform files, the complete backend (all data, seed \
and every API endpoint in the design, for all areas), and the frontend shell (pages, routing, shared state, the save \
function, API client, shared helpers and the complete stylesheet). Leave per-area frontend behaviour to later \
requests, but create every shared piece they need exactly as the design names it. The seed contains exactly the rows \
of the design's Seed data table with exactly those values: no further accounts, passwords or records.

{platform}
{quality}
DESIGN
{design}

REQUIREMENT
{requirement}

{format}"""

EXTEND_DESIGN_PROMPT = """\
The application below was built for the earlier stages of this product and already passes their tests. Design how
to extend it for the requirement of this stage. Later requests build it area by area and each receives this design
verbatim. Keep every existing page, route, control name, seed record, account and API endpoint working exactly as it
is; add only what this stage needs and reuse the existing shared code (state, save function, API client, helpers,
styles) instead of replacing it. Invent nothing the requirement does not imply.

{platform}
REQUIREMENT OF THIS STAGE
{requirement}

CURRENT APPLICATION FILES
{sources}

Write plain Markdown with these sections:
## Existing structure - the routes, shared modules and conventions the extension must reuse (one line each).
## New pages and routes - every new page/view, its address, how a user reaches it through visible controls from the
existing pages (for example from the home page or a repository page).
## Controls - per new page: every control, dialog, menu, region, tab and message this stage names, with ARIA role and
exact accessible name or text, and what opens each one.
## Seed data to add - a table with one row per record this stage needs that the seed does not hold yet: | kind |
values tests may type or see (names, username, email, password) | requirement quote |. Existing seed rows stay.
## Data model and API to add - new collections or fields and every new endpoint with method, path, request, response.
## Files - every file to add or change and what changes; which belong to which area ({areas}).
"""

EXTEND_FOUNDATION_PROMPT = """\
Extend the application below with the foundation of this stage, as the design says: the new seed records, data
model and every new API endpoint of this stage in the backend, and the new routes and page shells in the frontend.
Keep every existing file's behaviour: change an existing file only to add to it, keep its existing exports, routes,
control names, seed records and accounts, and output it complete.

{platform}
{quality}
DESIGN
{design}

REQUIREMENT OF THIS STAGE
{requirement}

CURRENT APPLICATION FILES
{sources}

{format}"""


AREA_PROMPT = """\
Implement requirement area {area} completely in the application below: every node, every scenario, every control, \
message and persistence rule of this area. Follow the design; put this area's code in its own files per the design and \
change shared files only where needed, keeping everything that already works for other areas.

{platform}
{quality}
DESIGN
{design}

AREA REQUIREMENT
{area_text}

CURRENT APPLICATION FILES
{sources}

{format}"""

REVIEW_PROMPT = """\
Review the application below against requirement area {area}. Go through the area's requirement text and every \
scenario step one by one and check the code really supports it end to end in a browser: the exact accessible names, \
roles, ARIA attributes and texts; what each click, double click, key press and dialog does; validation messages; \
persistence after refresh and reopening; the seed data the scenarios assume; and that nothing covers or replaces \
controls while they are used. Then fix every gap.

{platform}
{quality}
DESIGN
{design}

AREA REQUIREMENT
{area_text}

CURRENT APPLICATION FILES
{sources}

First list the gaps you found in at most 25 short lines, then output every file you change, complete. If nothing \
needs to change, reply with the single word OK.
{format}"""

FIX_PROMPT = """\
The application below fails these checks, run on the built and started app (a real browser opened its pages):

{problems}

Fix the causes without removing features. Keep every accessible name and behaviour the requirement states.

{platform}
{quality}
DESIGN
{design}

CURRENT APPLICATION FILES
{sources}

{format}"""


# ---------------------------------------------------------------- model

class Model:
    def __init__(self) -> None:
        base = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
        self.url = base + ("" if base.endswith("/chat/completions") else "/chat/completions")
        self.key = os.environ.get("OPENAI_API_KEY", "")
        self.model = os.environ.get("MODEL") or "deepseek-v4-flash"
        self.tokens = {"prompt": 0, "cached": 0, "completion": 0, "requests": 0}
        self.no_reasoning = False
        self.effort = "high"  # the evolution agent lowers it: vision-exp spends 65k tokens reasoning on edits
        if not base or not self.key:
            sys.exit("OPENAI_BASE_URL / OPENAI_API_KEY are not set")

    def chat(self, messages: list[dict], label: str, think: bool = True) -> tuple[str, str]:
        """One streamed request. A connection that sends nothing (not even reasoning) for
        IDLE_TIMEOUT seconds is dropped and retried; progress is logged every minute."""
        body = {"model": self.model, "messages": messages, "max_tokens": MAX_OUTPUT, "stream": True,
                "stream_options": {"include_usage": True}}
        if "deepseek" in self.model.lower():
            think = think and not self.no_reasoning
            body["thinking"] = {"type": "enabled" if think else "disabled"}
            if think:
                body["reasoning_effort"] = self.effort
        attempt, waited = -1, 0
        while attempt < 3:
            attempt += 1
            t = time.time()
            content, reasoning, finish, usage = [], 0, "", {}
            try:
                req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST",
                                             headers={"Content-Type": "application/json",
                                                      "Authorization": "Bearer " + self.key})
                beat = time.time()
                with urllib.request.urlopen(req, timeout=IDLE_TIMEOUT) as r:
                    for raw in r:
                        line = raw.decode("utf-8", "replace").strip()
                        if time.time() - beat >= 300:  # the platform keeps ~26k chars of stdout: a heartbeat a minute hid the second half of every run
                            beat = time.time()
                            log(f"{label}: still running, {int(beat - t)}s, received "
                                f"{sum(map(len, content))} chars of output, {reasoning} chars of reasoning")
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        usage = chunk.get("usage") or usage
                        for ch in chunk.get("choices") or []:
                            delta = ch.get("delta") or {}
                            if delta.get("content"):
                                content.append(delta["content"])
                            if delta.get("reasoning_content"):
                                reasoning += len(delta["reasoning_content"])
                            finish = ch.get("finish_reason") or finish
            except urllib.error.HTTPError as e:
                detail = e.read()[:300].decode("utf-8", "replace")
                if e.code == 429 and qa.wait_quota(label, waited):
                    waited += 1
                    attempt -= 1  # a quota wait is not a failed attempt
                    continue
                log(f"{label}: HTTP {e.code} {detail}; attempt {attempt + 1}/4")
                if e.code == 400 and ("thinking" in body or "reasoning_effort" in body):
                    body.pop("thinking", None)  # an endpoint that rejects the reasoning fields
                    body.pop("reasoning_effort", None)
                time.sleep(15 * (attempt + 1))
                continue
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                # no network at all (DNS failing): wait it out like a quota pause instead of spending the retries
                # in 40 s (a local run lost every request after the design to a short outage)
                if re.search(r"name resolution|getaddrinfo|Name or service not known|Network is unreachable", str(e)) \
                        and qa.wait_quota(label, waited):
                    waited += 1
                    attempt -= 1
                    continue
                log(f"{label}: connection stalled or failed after {int(time.time() - t)}s ({str(e)[:120]}); "
                    f"attempt {attempt + 1}/4")
                if attempt >= 1 and body.get("thinking", {}).get("type") == "enabled" and "timed out" in str(e):
                    body["thinking"] = {"type": "disabled"}  # a stall during a long reasoning phase: same remedy
                    body.pop("reasoning_effort", None)
                    self.no_reasoning = True
                    log(f"{label}: retrying without the reasoning phase (and for the rest of the run)")
                time.sleep(10)
                continue
            text = "".join(content)
            if not text and finish in ("", "length"):  # no content: a cut stream, or the output budget spent on reasoning
                log(f"{label}: empty stream after {int(time.time() - t)}s; attempt {attempt + 1}/4")
                # A review is optional: v4i lost 14-16 requests per run to empty streams (each up to 15 min and
                # billed), mostly on reviews; give a review one retry, then move on.
                if label.startswith("review") and attempt >= 1:
                    break
                # after two empty streams, answer without the reasoning phase: the streams were cut after 5-8 min of
                # reasoning on large extend-mode prompts (local stage 2: REQ-3 lost all four attempts)
                if attempt >= 1 and body.get("thinking", {}).get("type") == "enabled":
                    body["thinking"] = {"type": "disabled"}
                    body.pop("reasoning_effort", None)
                    self.no_reasoning = True  # this model spends the output budget on reasoning: skip it from now on
                    log(f"{label}: retrying without the reasoning phase (and for the rest of the run)")
                continue
            self.tokens["requests"] += 1
            self.tokens["prompt"] += usage.get("prompt_tokens", 0)
            self.tokens["cached"] += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
            self.tokens["completion"] += usage.get("completion_tokens", 0)
            log(f"{label}: {len(text)} chars in {int(time.time() - t)}s, finish={finish}, "
                f"tokens in/out={usage.get('prompt_tokens')}/{usage.get('completion_tokens')}")
            return text, finish
        return "", "error"


# A block runs from its header to the next header; the end marker is optional and matched
# loosely. github run 904755d695c8 scored 0: the model closed blocks with "<<<END FILE>>"
# (two >), a strict parser read index.html until the next exact marker, and every later
# file's source rendered as page text (duplicate controls in every test).
FILE_HEADER = re.compile(r"^<<<\s*FILE\s+([^<>\n]+?)\s*>{2,3}[ \t]*\n", re.M)
END_MARKER = re.compile(r"\n?<<<\s*END\s*FILE\s*>{0,3}")


# A block whose whole content says "no change" instead of giving the file. Run 7302676d041f scored 0:
# the model wrote "// unchanged" for 12 files (server.js, index.html, both package.json) and every one
# replaced a complete file.
PLACEHOLDER = re.compile(
    r"^\s*(?:(?://+|/\*+|<!--|#|\*)\s*)?[(\[]?\s*(?:\.{3}|…|unchanged|no changes?\b|not changed|"
    r"same as (?:before|above|previous|existing)|(?:keep|kept) (?:as is|the same)|(?:rest|remainder) of|"
    r"existing (?:code|content|file)|omitted|identical to)", re.I)


def is_placeholder(body: str) -> bool:
    """Only a note, no code: at most three lines, each a comment or an ellipsis."""
    s = body.strip()
    lines = s.splitlines()
    return (len(s) < 200 and len(lines) <= 3 and bool(PLACEHOLDER.match(s))
            and all(re.match(r"\s*(?://|/\*|\*|<!--|-->|#|\.{3}|…|\(?unchanged)", l, re.I) for l in lines))


def parse_blocks(text: str, last_complete: bool = True) -> tuple[dict[str, str], str | None]:
    """{path: content} for every file block, and the path of a trailing block the reply
    was cut off in (dropped, so a continuation rewrites it), if any."""
    heads = list(FILE_HEADER.finditer(text))
    files, cut = {}, None
    for i, h in enumerate(heads):
        body = text[h.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        end = END_MARKER.search(body)
        if end:
            body = body[:end.start()]
        elif i == len(heads) - 1 and not last_complete:
            cut = h.group(1).strip()
            continue
        if h.group(1).strip() in files and is_placeholder(body):
            continue  # a repeated block saying "unchanged" never replaces the full one given earlier
        files[h.group(1).strip()] = body.rstrip("\n") + "\n"
    return files, cut


def files_from(model: Model, prompt: str, label: str, think: bool = True) -> tuple[dict[str, str], str]:
    """One request (continued when cut off); every complete file block, plus the reply text."""
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    files: dict[str, str] = {}
    first = ""
    for part in range(1 + CONTINUATIONS):
        text, finish = model.chat(messages, f"{label}" + (f" (part {part + 1})" if part else ""), think)
        first = first or text
        # a stream that ended without a finish reason was cut off as well (v4o sheet: the foundation stopped at
        # 13,814 chars after 1,200 s and its half-written last file went into the app): keep the complete files
        # and ask for the rest, as for the output limit
        cut_off = finish == "length" or (finish == "" and bool(text))
        complete, cut = parse_blocks(text, last_complete=not cut_off)
        files.update(complete)
        if not cut_off:
            break
        if finish == "":
            log(f"{label}: the stream ended without a finish reason; continuing from the complete files")
        done = ", ".join(sorted(files)) or "none"
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": "Your reply was cut off by the output limit. Complete files so far: "
                      f"{done}. " + (f"Output {cut} again complete, then " if cut else "Now output ")
                      + "every remaining file, in the same format. Do not repeat complete files."}]
    return files, first


# ---------------------------------------------------------------- files

def safe_rel(raw: str) -> str | None:
    parts = [p for p in raw.strip().strip("`'\"").replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts or raw.strip().startswith("/"):
        return None
    rel = "/".join(parts)
    return rel if rel.startswith(("frontend/", "backend/")) else None


def write(out: Path, files: dict[str, str]) -> list[str]:
    written = []
    for raw, body in list(files.items()):
        if FILE_HEADER.search(body):  # another file's block leaked into this one: split it out
            inner, _ = parse_blocks(f"<<<FILE {raw}>>>\n" + body)
            files.update({k: v for k, v in inner.items() if k != raw})
            files[raw] = inner.get(raw, "")
    for raw, body in files.items():
        rel = safe_rel(raw)
        if not rel or rel.endswith("data.json") or "/node_modules/" in rel or rel.startswith("frontend/dist/"):
            continue
        if is_placeholder(body):
            log(f"skipped {rel}: the block only says {body.strip()[:60]!r}, not the file")
            continue
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(body, encoding="utf-8")
        written.append(rel)
    return written


def sources(out: Path) -> str:
    parts = []
    for f in sorted(out.rglob("*")):
        rel = f.relative_to(out).as_posix()
        if (f.is_file() and rel.startswith(("frontend/", "backend/")) and "node_modules" not in f.parts
                and not rel.startswith("frontend/dist/") and not rel.endswith(("package-lock.json", "data.json"))
                and f.suffix in (".js", ".mjs", ".cjs", ".html", ".css", ".json")):
            parts.append(f"<<<FILE {rel}>>>\n{f.read_text(encoding='utf-8', errors='replace')}\n<<<END FILE>>>\n")
    return "".join(parts)


DEFAULT_BUILD = """const fs = require('fs'); const path = require('path');
const src = path.join(__dirname, 'src'), dist = path.join(__dirname, 'dist');
fs.rmSync(dist, { recursive: true, force: true }); fs.cpSync(src, dist, { recursive: true });
"""


def ensure_manifests(out: Path) -> None:
    fe, be = out / "frontend", out / "backend"
    fe.mkdir(parents=True, exist_ok=True)
    be.mkdir(parents=True, exist_ok=True)
    for pkg in (fe / "package.json", be / "package.json"):
        try:
            if pkg.is_file() and not isinstance(json.loads(pkg.read_text(encoding="utf-8")), dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            pkg.unlink()  # unreadable: rewritten from the platform contract below (run 7302676d041f crashed on one)
    if not (fe / "package.json").is_file():
        (fe / "package.json").write_text(json.dumps({"name": "frontend", "private": True,
                                                     "scripts": {"build": "node build.js"}}, indent=2))
    if not (fe / "build.js").is_file() and "build.js" in (fe / "package.json").read_text():
        (fe / "build.js").write_text(DEFAULT_BUILD)
    if not (be / "package.json").is_file():
        (be / "package.json").write_text(json.dumps({"name": "backend", "private": True,
                                                     "scripts": {"start": "node server.js"}}, indent=2))


# ---------------------------------------------------------------- checks

def run(cmd: list[str], cwd: Path, timeout: int = 300, env: dict | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, (r.stdout + r.stderr)[-4000:]
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def asset_problems(page: str) -> list[str]:
    """Scripts and stylesheets of the page served at `page`, resolved like a browser: each must load as a file."""
    base = f"http://127.0.0.1:{CHECK_PORT}{page}"
    try:
        with urllib.request.urlopen(base, timeout=5) as r:
            html = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # a made-up nested address may be a 404; only a page that is served must load its assets
        return [] if page != "/" else [f"GET {page} returned HTTP {e.code}"]
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        return [f"GET {page} failed: {e}"]
    out = []
    for ref in re.findall(r"""<(?:script[^>]+src|link[^>]+href)=["']([^"'#?]+)""", html):
        if ref.startswith(("http:", "https:", "//", "data:")):
            continue
        url = urllib.parse.urljoin(base, ref)
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                kind, head = r.headers.get("Content-Type", ""), r.read(200).lstrip().lower()
            if "html" in kind or head.startswith((b"<!doctype", b"<html")):
                out.append(f"on {page} the page references {ref}; the browser requests "
                           f"{urllib.parse.urlparse(url).path}, which returns an HTML page instead of the file "
                           f"(use absolute paths such as /{ref.lstrip('./')})")
        except urllib.error.HTTPError as e:
            out.append(f"on {page} the page references {ref}, which returns HTTP {e.code}")
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            out.append(f"on {page} the page references {ref}, which does not load")
    return out


def playwright_root() -> Path | None:
    cands = [os.environ.get("OCTOS_ARC_PLAYWRIGHT_ROOT"), "/opt/arcbench", "/workspace", "/opt/playwright", "/app",
             "/usr/local/lib", "/usr/lib", str(Path.home())]
    for c in cands:
        if c and (Path(c) / "node_modules" / "@playwright" / "test" / "package.json").is_file():
            return Path(c)
    code, text = run(["find", "/", "-maxdepth", "6", "-type", "d", "-path", "*/node_modules/@playwright/test",
                      "-not", "-path", "/proc/*", "-not", "-path", "/sys/*"], Path("/"), 30)
    for hit in sorted(text.split(), key=len):
        root = Path(hit).parents[2]
        if (root / "node_modules" / "@playwright" / "test" / "package.json").is_file():
            return root
    return None


PROBE_JS = r"""
const { chromium } = require('@playwright/test');
const BASE = process.argv[2];
(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext()).newPage();
  const report = [];
  const visit = async (url) => {
    const problems = [];
    const onErr = (e) => problems.push('uncaught exception: ' + e.message);
    const onCon = (m) => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) problems.push('console.error: ' + m.text()); };
    const onRes = (r) => { const u = new URL(r.url()); if (r.status() >= 400 && u.origin === new URL(BASE).origin && !u.pathname.endsWith('favicon.ico')) problems.push(`HTTP ${r.status()} for ${u.pathname}`); };
    page.on('pageerror', onErr); page.on('console', onCon); page.on('response', onRes);
    try {
      await page.goto(url); await page.waitForLoadState('load'); await page.waitForTimeout(1000);
      const info = await page.evaluate(() => {
        const out = { text: document.body.innerText.trim().length, blocker: null, nested: [], zero: [], links: [] };
        const w = innerWidth, h = innerHeight;
        const hit = document.elementFromPoint(w / 2, h / 2);
        for (let e = hit; e && e !== document.body; e = e.parentElement) {
          const s = getComputedStyle(e), r = e.getBoundingClientRect();
          if ((s.position === 'fixed' || s.position === 'absolute') && r.width >= w * 0.9 && r.height >= h * 0.9) { out.blocker = e.outerHTML.slice(0, 120); break; }
        }
        const SEL = 'a[href],button,input,select,textarea,[role=button],[role=link],[role=tab],[role=menuitem],[role=option],[role=checkbox],[role=radio],[role=combobox]';
        const name = (e) => (e.getAttribute('aria-label') || e.innerText || e.getAttribute('name') || '').trim().replace(/\s+/g, ' ').slice(0, 50);
        for (const o of document.querySelectorAll(SEL)) {
          const i = o.querySelector(SEL);
          if (i && o.offsetParent !== null && out.nested.length < 4) out.nested.push(`${i.tagName.toLowerCase()} "${name(i)}" inside ${o.getAttribute('role') || o.tagName.toLowerCase()} "${name(o)}"`);
        }
        const cells = [...document.querySelectorAll('[role=gridcell],[role=cell],td,[role=tab],[role=button],button,[role=row]')]
          .filter((e) => getComputedStyle(e).display !== 'none' && getComputedStyle(e).visibility !== 'hidden' && e.closest('[hidden]') === null);
        const zero = cells.filter((e) => { const r = e.getBoundingClientRect(); return r.width < 2 || r.height < 2; });
        if (cells.length && zero.length >= Math.max(3, cells.length * 0.2))
          out.zero.push(`${zero.length} of ${cells.length} cells/buttons/tabs render with zero width or height (missing layout styles), e.g. ${zero[0].outerHTML.slice(0, 100)}`);
        out.links = [...document.querySelectorAll('a[href]')].map((a) => a.href);
        return out;
      });
      if (info.text === 0) problems.push('the page renders no visible text');
      if (info.blocker) problems.push('an element covers the whole viewport on load: ' + info.blocker);
      for (const n of info.nested) problems.push('nested interactive control: ' + n + ' (its accessible name absorbs the inner one)');
      for (const z of info.zero) problems.push(z);
      return { problems, links: info.links };
    } catch (e) {
      problems.push('could not open the page: ' + e.message.split('\n')[0]);
      return { problems, links: [] };
    } finally { page.off('pageerror', onErr); page.off('console', onCon); page.off('response', onRes); }
  };
  const home = await visit(BASE + '/');
  for (const p of home.problems) report.push('home page: ' + p);
  const seen = new Set(), origin = new URL(BASE).origin;
  const targets = home.links.filter((h) => { const u = new URL(h); const key = u.pathname.replace(/[0-9a-f-]{6,}|\d+/gi, ':id');
    if (u.origin !== origin || seen.has(key) || /\.(csv|json|zip)$/i.test(u.pathname)) return false; seen.add(key); return true; }).slice(0, 8);
  for (const t of targets) { const r = await visit(t); for (const p of r.problems) report.push(`${new URL(t).pathname} (opened directly): ${p}`); }
  console.log(JSON.stringify(report));
  await browser.close();
})().catch((e) => { console.log(JSON.stringify(['browser probe failed: ' + e.message.split('\n')[0]])); });
"""


def browser_problems(root: Path | None) -> list[str]:
    if root is None:
        return []
    script = STATE_DIR / "probe.cjs"
    script.write_text(PROBE_JS, encoding="utf-8")
    env = dict(os.environ, NODE_PATH=str(root / "node_modules"))
    code, text = run(["node", str(script), f"http://127.0.0.1:{CHECK_PORT}"], STATE_DIR, 180, env)
    for line in reversed(text.strip().splitlines()):
        if line.startswith("["):
            try:
                found = json.loads(line)
            except json.JSONDecodeError:
                break
            if found and all("browser probe failed" in p for p in found):
                log(f"browser probe unavailable: {found[0][:200]}")
                return []
            return found
    log(f"browser probe gave no report: {text[-300:]}")
    return []


def check(out: Path, pw: Path | None) -> str | None:
    """Build, start, fetch pages and their assets, probe them in a browser; None when all is well."""
    problems = []
    for f in sorted(out.glob("frontend/src/**/*.js")) + sorted(p for p in out.glob("backend/**/*.js") if "node_modules" not in p.parts):
        target = f
        if f.suffix == ".js" and "import " in f.read_text(encoding="utf-8", errors="replace")[:4000] and "frontend" in f.parts:
            target = Path(tempfile.gettempdir()) / "oneshot-check.mjs"  # ES module: checked as .mjs, so the error names the file
            shutil.copyfile(f, target)
        code, text = run(["node", "--check", str(target)], out)
        if code:
            problems.append(f"syntax error in {f.relative_to(out).as_posix()}:\n{text[-800:]}")
    for part in ("frontend", "backend"):
        # A review once wrote an invalid package.json and json.loads crashed the whole run (7302676d041f, 0/100):
        # report it so the fix step rewrites the file from the platform contract.
        pkg_file = out / part / "package.json"
        try:
            pkg = json.loads(pkg_file.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as e:
            head = pkg_file.read_text(encoding="utf-8", errors="replace")[:300] if pkg_file.exists() else "(missing)"
            problems.append(f"{part}/package.json is not valid JSON ({e}); it must be exactly the platform contract. "
                            f"Current content:\n{head}")
            continue
        if not isinstance(pkg, dict):
            problems.append(f"{part}/package.json must be a JSON object, as the platform contract states")
            continue
        if pkg.get("dependencies") or pkg.get("devDependencies"):
            code, text = run(["npm", "install", "--no-audit", "--no-fund"], out / part, 600)
            if code:
                problems.append(f"npm install failed in {part}:\n{text[-1200:]}")
    code, text = run(["npm", "run", "build"], out / "frontend")
    if code:
        problems.append(f"frontend build failed:\n{text[-1200:]}")
    if problems:
        return "\n\n".join(problems)
    data_file = out / "backend" / "data.json"
    had_data = data_file.exists()
    log_path = STATE_DIR / "server.log"
    with open(log_path, "w", encoding="utf-8") as lf:
        proc = subprocess.Popen(["npm", "start"], cwd=out / "backend", stdout=lf, stderr=subprocess.STDOUT,
                                env=dict(os.environ, PORT=str(CHECK_PORT)), start_new_session=True)
    try:
        status = 0
        for _ in range(60):
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{CHECK_PORT}/", timeout=3) as r:
                    status = r.status
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.5)
        if status != 200:
            problems.append("the server did not serve the home page (HTTP 200). Server output:\n"
                            + log_path.read_text(encoding="utf-8", errors="replace")[-1500:])
        else:
            for page in ("/", "/check/nested/address"):
                problems += asset_problems(page)
            problems += browser_problems(pw)
            if proc.poll() is not None:  # github v4k: three fix rounds saw only "connection refused", never the crash
                problems.insert(0, f"the server process exited (code {proc.returncode}) while the pages were opened; "
                                "its output:\n" + log_path.read_text(encoding="utf-8", errors="replace")[-2000:])
            errs = [l for l in log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    if re.search(r"\b(ReferenceError|TypeError|SyntaxError|RangeError|Unhandled)", l)]
            if errs:
                problems.append("the server logged errors:\n" + "\n".join(errs[:8]))
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, AttributeError):
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not had_data:
            data_file.unlink(missing_ok=True)  # the first real start recreates the clean seed
    return "\n".join(f"- {p}" for p in problems) or None


# ---------------------------------------------------------------- platform progress events

class Progress:
    """Requirement-node states for the platform's canvas (arcbench_agent_runtime, bundled).
    Purely informative: any failure here is ignored so it never affects the generated app."""

    def __init__(self, out: Path, tree: dict | None) -> None:
        self.events = None
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from arcbench_agent_runtime import AgentRuntime  # noqa: PLC0415
            runtime = AgentRuntime.from_env(project_dir=str(out))
            if tree:
                runtime.traceability.store_requirement_tree(tree)
            self.events = runtime.events
        except Exception as e:  # noqa: BLE001
            log(f"progress events unavailable ({str(e)[:120]})")

    def call(self, name: str, *args) -> None:
        if self.events is not None:
            try:
                getattr(self.events, name)(*args)
            except Exception:  # noqa: BLE001
                pass

    def nodes(self, name: str, ids: list[str], message: str) -> None:
        for nid in ids:
            self.call(name, nid, message)


# ---------------------------------------------------------------- main

# Per-area code reviews: about an hour per run and a third of the cost; runs that lost most reviews still
# scored 43-48 on sheet, so the no-review package (v4l) tests whether they pay for themselves.
REVIEWS = False


def existing_app_lines(out: Path) -> int:
    """Source lines of an application already in the output directory (a blank template has almost none)."""
    n = 0
    for part in ("frontend/src", "backend"):
        for f in (out / part).rglob("*") if (out / part).is_dir() else []:
            if f.is_file() and "node_modules" not in f.parts and f.suffix in (".js", ".mjs", ".cjs", ".html", ".css") \
                    and f.name != "package-lock.json":
                n += len(f.read_text(encoding="utf-8", errors="replace").splitlines())
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("requirement_path", nargs="?", default=os.environ.get("ARCBENCH_REQUIREMENTS_DIR", "."))
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--type", "--app-type", dest="app_type", default="web")
    ap.add_argument("--web-port", default=None)
    args, _ = ap.parse_known_args()
    out = Path(args.output_dir or os.environ.get("ARCBENCH_TEMPLATE_DIR") or "workspace/app").resolve()
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    whole, areas, tree = requirement(Path(args.requirement_path).resolve())
    qa.start_audit(tree, whole)  # requirement checks for QA, written while the app is built
    log(f"requirement: {len(whole)} chars, {len(areas)} areas: {[a for a, _, _ in areas]}; output {out}")
    progress = Progress(out, tree)
    progress.call("mark_run_started", "whole-app agent started")
    model = Model()
    pw = playwright_root()
    log(f"playwright: {pw or 'not found (browser probe skipped)'}")

    # a progressive stage after the first receives the previous stage's application in the output directory
    extend = existing_app_lines(out) >= 300
    if extend:
        log(f"extend mode: the output directory holds an application ({existing_app_lines(out)} source lines)")
        design, _ = model.chat([{"role": "system", "content": SYSTEM},
                                {"role": "user", "content": EXTEND_DESIGN_PROMPT.format(
                                    platform=PLATFORM, requirement=whole, sources=sources(out),
                                    areas=", ".join(a for a, _, _ in areas))}], "design")
    else:
        design, _ = model.chat([{"role": "system", "content": SYSTEM},
                                {"role": "user", "content": DESIGN_PROMPT.format(
                                    platform=PLATFORM, requirement=whole, areas=", ".join(a for a, _, _ in areas))}], "design")
    design = design.strip() or "(no design; follow the requirement)"
    (STATE_DIR / "design.md").write_text(design, encoding="utf-8")
    log(f"design: {len(design)} chars")
    common = {"platform": PLATFORM, "quality": QUALITY, "design": design, "format": FORMAT}

    if extend:
        files, _ = files_from(model, EXTEND_FOUNDATION_PROMPT.format(requirement=whole, sources=sources(out), **common),
                              "foundation")
    else:
        files, _ = files_from(model, FOUNDATION_PROMPT.format(requirement=whole, **common), "foundation")
    log(f"foundation wrote {write(out, files)}")
    ensure_manifests(out)

    for label, text, ids in areas:
        progress.nodes("mark_implementation_started", ids, f"building {label}")
        prompt = AREA_PROMPT.format(area=label, area_text=text, sources=sources(out), **common)
        files, _ = files_from(model, prompt, f"area {label}")
        written = write(out, files)
        if not written:  # v5 stage 3: a 226k-char reply ended normally without one file block; 25 of 41 scenarios lost
            log(f"area {label}: the reply held no file; asking again")
            files, _ = files_from(model, prompt + "\n\nReply with the complete files in the file-block format above; "
                                  "a reply without file blocks changes nothing.", f"area {label} (again)")
            written = write(out, files)
        log(f"area {label} wrote {written}")
        progress.nodes("mark_implementation_done" if written else "mark_implementation_failed", ids,
                       f"{label}: {len(written)} files")
        ensure_manifests(out)

    for label, text, _ in (areas if REVIEWS else []):
        files, reply = files_from(model, REVIEW_PROMPT.format(area=label, area_text=text, sources=sources(out), **common),
                                  f"review {label}")
        first_head = FILE_HEADER.search(reply)
        gaps = (reply[:first_head.start()] if first_head else reply).strip()[:600]
        log(f"review {label}: {gaps!r}; wrote {write(out, files)}")
        ensure_manifests(out)

    # Everything after generation improves an app that already exists; an unexpected error here must not
    # fail the run (7302676d041f exited 1 in check and scored 0 with a complete app on disk).
    try:
        for round_ in range(1, FIX_ROUNDS + 1):
            problems = check(out, pw)
            if problems is None:
                log("check passed: builds, starts, assets load, browser probe clean")
                break
            log(f"check round {round_} found:\n{problems[:1500]}")
            files, _ = files_from(model, FIX_PROMPT.format(problems=problems, sources=sources(out), **common), f"fix {round_}")
            log(f"fix {round_} wrote {write(out, files)}")
            ensure_manifests(out)
        else:
            problems = check(out, pw)
            log("check passed after fixes" if problems is None else f"check still failing:\n{problems[:800]}")
    except Exception as e:  # noqa: BLE001
        log(f"check/fix stopped by an error ({type(e).__name__}: {str(e)[:200]}); keeping the app as it is")
    ensure_manifests(out)

    req_path = Path(args.requirement_path).resolve()
    qa.SKIP_SEED_PASSWORDS = extend
    qa.run_qa(out, load_tree(req_path if req_path.is_dir() else req_path.parent), whole, pw, time.time() + qa.QA_SECONDS)

    shutil.rmtree(out / "frontend" / "dist", ignore_errors=True)
    for part in ("frontend", "backend"):
        shutil.rmtree(out / part / "node_modules", ignore_errors=True)
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=agent@local", "-c", "user.name=oneshot", "commit", "-qm", "whole-app agent"]):
        run(cmd, out)
    progress.call("mark_run_completed", "whole-app agent finished")
    t = model.tokens
    cost = ((t["prompt"] - t["cached"]) * 2.51 + t["cached"] * 0.251 + t["completion"] * 9.49) / 1e6
    log(f"done in {int(time.time() - started)}s; requests {t['requests']}, prompt {t['prompt']} (cached {t['cached']}), "
        f"completion {t['completion']}, est. CNY {cost:.2f}")


if __name__ == "__main__":
    main()
