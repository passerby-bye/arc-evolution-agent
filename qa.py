"""QA phase of the whole-app agent: after generation, check the running app the way the grader
uses it and let the model fix what is wrong, within hard limits.

1. Code picks the scenario text most tests share (tests are one per scenario) and the model writes a
   few common-path Playwright tests from it (one request). The tests are gated (names must come from
   the requirement, no typing into grid cells) and every name lookup is made exact.
2. Checks: every control the requirement names (button "X", tab "Y", ...) must have that role where it
   appears, plus the common-path tests, each on freshly restored seed data.
3. A tool-using model (run_checks, browser, list_files, read_file, search_code, edit_file,
   search_requirement, finish) fixes the problems. At most QA_CALLS tool calls and QA_SECONDS; it
   stops when the checks pass, and every edit is reverted if the checks end worse than they began.

Evidence (sheet): wholeapp-v2 scored 56 and 5 with the same code; the 5 rendered "New blank workbook"
as a link, and the hidden tests start from that button (the 56 app's data held 93 workbooks they
created). A prototype of this phase found and fixed it in 66 s for about CNY 0.2.
"""
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

QA_CALLS, QA_SECONDS, QA_PORT = 40, 1500, int(os.environ.get("EVO_QA_PORT", "34601"))
# QA's tests are all written by the model from the public requirement (structured_tests.py, common_spec); the
# hand-written per-task checks of v4o and earlier were removed, since which statements they checked was chosen
# partly from observing graded runs.
# An extended app holds the earlier stages' accounts, whose passwords this stage's requirement does not quote
# (local stage 2 spent all 40 QA calls "fixing" New-password-456! and Replacement-password-456!).
SKIP_SEED_PASSWORDS = False
STATE = Path(tempfile.mkdtemp(prefix="oneshot-qa-"))
FRESH_DATA = False
SPEC_WORKERS = int(os.environ.get("EVO_SPEC_WORKERS", "3"))  # set by the evolution agent: the backend's data file is dropped before every start
CHECKED_ROLES = {"button", "link", "tab", "checkbox", "menuitem", "radio", "combobox"}


def log(msg: str) -> None:
    print(f"[oneshot] qa: {msg}", flush=True)


# ---------------------------------------------------------------- copied from arc/main.py
_ROLES = (r"button|link|text ?box|check ?box|combo ?box|tab|dialog|region|search ?box|menu ?item|option|radio"
          r"(?: button)?|grid|heading|field|select|column ?header|row ?header|grid ?cell|list|input")
_QUOTED = r"[“\"]([^”\"\n]{1,60})[”\"]"
_ROLE_THEN_NAME = re.compile(rf"\b({_ROLES})s?\s+(?:named|labell?ed|called|titled|with the (?:exact )?accessible name(?: of)?|"
                             rf"whose (?:exact )?accessible name is)\s+{_QUOTED}", re.I)
# "“Delete worksheet” confirmation button": up to two words between name and role
_NAME_THEN_ROLE = re.compile(rf"{_QUOTED}\s+(?:[a-z-]+\s+){{0,2}}?({_ROLES})\b", re.I)
# "has the accessible name “Worksheet grid”": the role is said elsewhere in the sentence
_ANY_NAME = re.compile(rf"accessible name(?: is| of)?\s+{_QUOTED}", re.I)
_ARIA_STATE = re.compile(r"\baria-[a-z]+=\s*[“\"][^”\"\n]{1,30}[”\"]", re.I)


def named_controls(text: str, limit: int = 40) -> list[str]:
    """`role "name"` pairs and aria-* states the requirement text fixes, in order, once each.

    hackathon--sheet rose from 3/100 to 8/100 on four such contracts alone (a button that
    was a link, a tab without aria-selected, ...), all stated in the text codegen had seen."""
    found: list[str] = []
    def add(item: str) -> None:
        if item not in found:
            found.append(item)
    pattern = rf"{_ROLE_THEN_NAME.pattern}|{_NAME_THEN_ROLE.pattern}|{_ANY_NAME.pattern}|{_ARIA_STATE.pattern}"
    for m in re.finditer(pattern, text, re.I):
        whole = m.group(0)
        a = _ROLE_THEN_NAME.match(whole)
        b = _NAME_THEN_ROLE.match(whole)
        c = _ANY_NAME.match(whole)
        if a:
            add(f'{re.sub(r" ", "", a.group(1).lower())} "{a.group(2)}"')
        elif b:
            add(f'{re.sub(r" ", "", b.group(2).lower())} "{b.group(1)}"')
        elif c:
            if not any(f'"{c.group(1)}"' in item for item in found):
                add(f'accessible name "{c.group(1)}"')
        else:
            add(re.sub(r"[“”]", '"', whole))
    return found[:limit]


# Oracle quality gate for generated scenario tests. A wrong test is worse than none: in
# gh-mini-scen-1 a test looked for "Email" where the requirement says "Username or email",
# and repairs bent the app toward the test. Every literal a test locates by must appear
# verbatim in the requirement text; a test that invents a name is dropped, not obeyed.
_LOCATOR_LITERAL = re.compile(
    r"""(?:\bname\s*:\s*|getBy(?:Label|Text|Placeholder|Title)\(\s*)(['"`])((?:\\.|(?!\1).)*)\1""")
_GOTO_LITERAL = re.compile(r"""\.goto\(\s*(['"`])((?:\\.|(?!\1).)*)\1""")
_TEST_START = re.compile(r"""^\s*test\(\s*['"`]""", re.M)
# Only unindented tests are cut apart: dropping an indented one could leave its enclosing block
# half-closed (mini-scen-v8 REQ-1-2-2 lost its whole spec to a syntax error that way).
_TOP_TEST_START = re.compile(r"""^test\(\s*['"`]""", re.M)
_INPUT_LITERAL = re.compile(r"""\.(?:fill|type|pressSequentially|selectOption)\(\s*(['"`])((?:\\.|(?!\1).)*)\1""")
# Cell coordinates, row numbers and column letters: names a grid derives, never invented wording.
_GRID_NAME = re.compile(r"^(?:[A-Z]{1,3}\d{1,5}(?::[A-Z]{1,3}\d{1,5})?|[A-Z]{1,3}|\d{1,5})$")
_FILE_NAME = re.compile(r"^[^/\\\s][^/\\]*\.(?:csv|tsv|txt|json|xlsx?|png|jpe?g|gif|pdf|md|zip)$", re.I)
_FILL_ON_CELL = re.compile(
    r"""getByRole\(\s*['"`](?:gridcell|cell|row|rowheader|columnheader)['"`][^;\n]*?\)\s*\.(?:fill|type|pressSequentially)\(""")
_LABEL_FILL = re.compile(r"""getByLabel\(\s*(['"`])([^'"`]+)\1[^)]*\)\s*\.(?:fill|type|pressSequentially)\(""")
_QUOTED_TEMPLATE = re.compile(r"""["“`]([^"”`\n]*<[^>\n]+>[^"”`\n]*)["”`]""")


def requirement_name_patterns(text: str) -> list[re.Pattern]:
    """Quoted names with placeholders ("Worksheet options for <worksheet name>") as regexes."""
    out = []
    for tpl in set(_QUOTED_TEMPLATE.findall(text)):
        parts = re.split(r"<[^>]+>", tpl)
        if len(re.findall(r"[A-Za-z]", "".join(parts))) < 3:
            continue  # a bare placeholder ("<cell range>") would admit any string
        out.append(re.compile("^" + ".+".join(re.escape(p) for p in parts) + "$"))
    return out


def invented_literals(test_text: str, allowed: str, patterns: list[re.Pattern] | None = None) -> list[str]:
    """Locator strings in `test_text` that the requirement text never states, plus any
    navigation to a URL other than the home page. Dynamic template strings, grid
    coordinates, values the test typed itself and digit-only texts are not names."""
    typed = {lit for _, lit in _INPUT_LITERAL.findall(test_text)}
    # file names a test uploads ({ name: 'sales.csv', ... }) and their stems (apps often name
    # an imported record after the file) are the test's own data, not control names
    for _, lit in _LOCATOR_LITERAL.findall(test_text):
        if _FILE_NAME.match(lit):
            typed.update({lit, lit.rsplit(".", 1)[0]})
    if patterns is None:
        patterns = requirement_name_patterns(allowed)
    bad = []
    for _, lit in _LOCATOR_LITERAL.findall(test_text):
        lit = lit.replace("\\'", "'").replace('\\"', '"')
        if ("${" in lit or not lit.strip() or lit in allowed or lit in typed or _GRID_NAME.match(lit)
                or not re.search(r"[A-Za-z]", lit) or any(p.match(lit) for p in patterns)):
            continue
        bad.append(lit)
    for _, url in _GOTO_LITERAL.findall(test_text):
        if url not in ("/", "", "#/", "/#/", "/#") and "${" not in url:
            bad.append(f"goto {url}")
    # typing straight into a cell guesses the implementation (the requirement names an editor for that)
    if _FILL_ON_CELL.search(test_text):
        bad.append("fill/type on a grid cell")
    for _, lit in _LABEL_FILL.findall(test_text):
        if _GRID_NAME.match(lit):
            bad.append(f"fill on getByLabel('{lit}') (a cell, not an input)")
    return bad


def gate_scenario_spec(body: str, allowed: str) -> tuple[str | None, list[str]]:
    """Keep the tests of a generated spec whose locators all come from the requirement.
    Returns (spec with only those tests, or None when none remain; rejection notes).
    A spec that groups tests in describe blocks is judged as a whole."""
    if not _TEST_START.search(body):
        return None, ["no test() found"]
    starts = [m.start() for m in _TOP_TEST_START.finditer(body)]
    patterns = requirement_name_patterns(allowed)
    if "test.describe" in body or not starts or len(starts) < len(_TEST_START.findall(body)):
        bad = invented_literals(body, allowed, patterns)
        return (None, [f"whole file: {', '.join(bad)}"]) if bad else (body, [])
    head, kept, notes = body[:starts[0]], [], []
    for i, s in enumerate(starts):
        block = body[s:starts[i + 1] if i + 1 < len(starts) else len(body)]
        bad = invented_literals(block, allowed, patterns)
        title = block.strip().splitlines()[0][:80]
        if bad:
            notes.append(f"{title}: {', '.join(bad)}")
        else:
            kept.append(block)
    return (head + "".join(kept) if kept else None), notes


# ---------------------------------------------------------------- step 1: what most tests share (code)

def _sentences(text: str) -> list[str]:
    text = " ".join(str(text).split())
    return [s.strip() for s in re.split(r"(?<=[.;])\s+(?=[A-Z])", text) if s.strip()]


def _opening(sentence: str, words: int = 12) -> str:
    head = re.split(r",? and (?:follows|then|the requested)|; ", sentence)[0]
    return " ".join(head.split()[:words])


def common_text(tree: dict) -> tuple[int, str]:
    """(scenario count, the most shared scenario openings, outcomes and seed sentences with counts)."""
    def leaves(n):
        kids = [c for c in n.get("children") or [] if isinstance(c, dict)]
        if not kids:
            yield n
        for c in kids:
            yield from leaves(c)
    counts = {"GIVEN": {}, "WHEN": {}, "THEN": {}, "SEED": {}}
    total = 0
    for node in leaves(tree):
        for sc in node.get("scenarios") or []:
            total += 1
            seen = set()
            for st in (sc.get("steps") or []) if isinstance(sc, dict) else []:
                kind = str(st.get("keyword", "")).upper().strip()
                for s in _sentences(st.get("content", "")):
                    for key, text in ((kind, _opening(s) if kind in ("GIVEN", "WHEN") else s),
                                      ("SEED", s if re.search(r"\bseed", s, re.I) else None)):
                        if text and key in counts and (key, text) not in seen:
                            seen.add((key, text))
                            counts[key][text] = counts[key].get(text, 0) + 1
    lines = []
    for key, k in (("GIVEN", 4), ("WHEN", 4), ("THEN", 3), ("SEED", 8)):
        for text, n in sorted(counts[key].items(), key=lambda kv: -kv[1])[:k]:
            lines.append(f"- [{key} x{n}] {text}")
    return total, "\n".join(lines)


# ---------------------------------------------------------------- step 1b: common-path tests (model)

COMMON_PROMPT = """\
Automated browser tests will grade a web application built from the requirement below, one test per
scenario ({total} scenarios). Code has counted which scenario text most tests share:

{common}

REQUIREMENT
{requirement}

Write one Playwright test file with 4 to 8 tests that check, on the running app, the steps most tests go
through before their own feature (entry points, sign-in, creating or opening the record they work on,
entering values, persistence after reload) and the exact observable result the requirement states for
each (quote names verbatim: if it says the account menu displays the username, assert that). Start every
test from page.goto('/'); locate elements only by role with the exact accessible name, or by label or text
quoted in the requirement; never type into a grid or table cell directly (use the editor the requirement
names); give records you create a unique suffix (Date.now()); assert only what the requirement states.
Seed descriptions differ between features and cannot all hold at once: only assume that the records every
scenario uses exist (for example that a named record is listed); for anything else, first create your own
record through the visible controls (as the grader's tests do) and enter the values you assert.
Write every test at the top level of the file as test('...', async ({{ page }}) => {{ ... }}); no test.describe.

Reply with exactly one block:
<<<FILE common.spec.ts>>>
...
<<<END FILE>>>
"""


QUOTA_WAIT = [1800]  # seconds the whole run may spend waiting out HTTP 429 (shared by main.py and QA)


def wait_quota(label: str, waited: int) -> bool:
    """Sleep through an HTTP 429 without spending a retry. wholeapp-v4c: the endpoint answered
    "Free allocated quota exceeded" for ~20 minutes and 4 quick retries lost every review and fix,
    so github shipped an app whose scripts 404ed (1/100). Returns False once the budget is spent."""
    pause = min(300, 60 * (waited + 1), QUOTA_WAIT[0])
    if pause <= 0:
        return False
    log(f"{label}: rate limited or out of quota; waiting {pause}s ({QUOTA_WAIT[0]}s of waiting left)")
    QUOTA_WAIT[0] -= pause
    time.sleep(pause)
    return True


def _chat(body: dict, timeout: int, label: str) -> dict | None:
    base = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
    url = base + ("" if base.endswith("/chat/completions") else "/chat/completions")
    body = dict(body, model=os.environ.get("MODEL") or "deepseek-v4-flash")
    attempt, waited = 0, 0
    while attempt < 3:
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": "Bearer " + os.environ.get("OPENAI_API_KEY", "")})
            return json.load(urllib.request.urlopen(req, timeout=timeout))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError) as e:
            if isinstance(e, urllib.error.HTTPError) and e.code == 429 and wait_quota(label, waited):
                waited += 1
                continue
            attempt += 1
            log(f"{label}: request failed ({str(e)[:120]}); attempt {attempt}/3")
            time.sleep(10)
    return None


def scenarios_are_steps(tree: dict) -> bool:
    """True when the WHEN steps are concrete clicks and inputs, not filled templates ("the requested workflow
    sheet2,the requested workflow", "follows the visible controls for the ... workflow"): sheet 100/100 templates,
    the old github 36/100, the github rewritten on 2026-10-02 1/100."""
    whens = []

    def walk(n: dict) -> None:
        for sc in n.get("scenarios") or []:
            for st in sc.get("steps") or [] if isinstance(sc, dict) else []:
                if isinstance(st, dict) and str(st.get("keyword", "")).strip().upper() == "WHEN":
                    whens.append(str(st.get("content", "")))
        for c in n.get("children") or []:
            if isinstance(c, dict):
                walk(c)
    walk(tree)
    templated = [w for w in whens if re.search(r"requested workflow|follows the visible controls for", w)]
    return bool(whens) and len(templated) <= 0.2 * len(whens)


def areas_from_tree(tree: dict) -> list[tuple[str, str]]:
    """(label, text) per top-level area: the shared conventions plus every node's description, no scenarios."""
    root = " ".join(str(tree.get("description") or "").split())

    def walk(n: dict, depth: int, out: list) -> list:
        desc = " ".join(str(n.get("description") or "").split())
        out.append(f"{'  ' * depth}- {n.get('id')} {n.get('name', '')}: {desc}")
        for c in n.get("children") or []:
            if isinstance(c, dict):
                walk(c, depth + 1, out)
        return out
    areas = [c for c in tree.get("children") or [] if isinstance(c, dict)] or [tree]
    return [(f"{a.get('id')} {a.get('name', '')}", f"Conventions: {root}\n" + "\n".join(walk(a, 0, []))) for a in areas]


def description_spec(tree: dict, requirement: str) -> Path | None:
    import structured_tests  # noqa: PLC0415
    text = structured_tests.generate(areas_from_tree(tree), requirement, _chat, log, requirement_name_patterns(requirement))
    if not text:
        return None
    path = STATE / "tests" / "common.spec.ts"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    log(f"description tests: {len(_TEST_START.findall(text))} kept")
    return path


EXTRA_NAME_PATTERNS: list = []  # names known from elsewhere (the inherited application's controls)


def scenario_spec(tree: dict, requirement: str) -> Path | None:
    import structured_tests  # noqa: PLC0415
    text = structured_tests.generate_from_scenarios(tree, requirement, _chat, log, requirement_name_patterns(requirement) + EXTRA_NAME_PATTERNS)
    if not text:
        return None
    path = STATE / "tests" / "common.spec.ts"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    log(f"scenario tests: {len(_TEST_START.findall(text))} kept")
    return path


_ROLE_OPTS = re.compile(r"getByRole\(\s*([^,()]+?)\s*,\s*\{([^{}]*?)\}\s*\)")


def _exact_role(m: re.Match) -> str:
    """getByRole(role, { name: x }) -> getByRole(role, { name: x, exact: true }) unless exact is already given."""
    opts = m.group(2).strip().rstrip(",")
    if not re.search(r"\bname\s*:", opts) or re.search(r"\bexact\s*:", opts):
        return m.group(0)
    return f"getByRole({m.group(1)}, {{ {opts}, exact: true }})"


def common_spec(tree: dict, requirement: str) -> Path | None:
    total, common = common_text(tree)
    r = _chat({"messages": [{"role": "user", "content": COMMON_PROMPT.format(total=total, common=common, requirement=requirement)}],
               "max_tokens": 32768, "thinking": {"type": "enabled"}, "reasoning_effort": "low"}, 900, "common-path tests")
    reply = ((r or {}).get("choices") or [{}])[0].get("message", {}).get("content") or ""
    body = reply.split("<<<FILE", 1)[-1].split(">>>", 1)[-1] if "<<<FILE" in reply else reply
    body = re.split(r"\n?<<<\s*END\s*FILE", body)[0].strip()
    body = re.sub(r"^```[a-z]*\n|```$", "", body.strip())
    if "@playwright/test" not in body or "test(" not in body:
        log("no usable common-path tests in the reply")
        return None
    gated, notes = gate_scenario_spec(body, requirement)
    for n in notes:
        log(f"dropped a common-path test: {n[:200]}")
    if not gated:
        return None
    # exact names: a partial match ("acme-docs" also hits "acme-docs-fork") makes repairs bend
    # correct behaviour toward the test; the grader looks controls up by their exact names
    # (also when the name is a variable: sheet tests used getByRole('gridcell', { name: cell }), and "A1" also
    # matched A10..A19, so 7 of 8 tests failed in strict mode on a 58-point app)
    gated = _ROLE_OPTS.sub(_exact_role, gated)
    path = STATE / "tests" / "common.spec.ts"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(gated, encoding="utf-8")
    log(f"common-path tests: {len(_TEST_START.findall(gated))} kept")
    return path


# ---------------------------------------------------------------- the running app

def free_port(port: int) -> None:
    """Stop whatever listens on `port` (Linux: the inode of the listening socket in /proc/net/tcp*, then the process
    holding it). Platform run: Octos started the app on the test port for its own checks and left it running."""
    try:
        hexport = f"{port:04X}"
        inodes = set()
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                rows = Path(table).read_text().splitlines()[1:]
            except OSError:
                continue
            for row in rows:
                cols = row.split()
                if len(cols) > 9 and cols[1].endswith(":" + hexport) and cols[3] == "0A":  # 0A = LISTEN
                    inodes.add(cols[9])
        if not inodes:
            return
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit() or int(pid_dir.name) == os.getpid():
                continue
            try:
                fds = list((pid_dir / "fd").iterdir())
            except OSError:
                continue
            for fd in fds:
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if target.startswith("socket:[") and target[8:-1] in inodes:
                    try:
                        os.kill(int(pid_dir.name), signal.SIGKILL)
                        log(f"stopped process {pid_dir.name} that held port {port}")
                    except OSError:
                        pass
                    break
        time.sleep(0.5)
    except Exception:  # noqa: BLE001  # best effort: never fail a check over this
        pass


class App:
    """Builds and serves the app; rebuilds when sources change; restores the startup data before
    every run so one attempt's writes cannot mislead the next."""

    def __init__(self, root: Path) -> None:
        self.root, self.proc, self.stamp = root, None, None
        data = root / "backend" / "data.json"
        # FRESH_DATA: the data file is the server's own (written on its first start, before the seed step), so
        # every start drops it and the server re-seeds from the current seed source (evolution: sheet run 6 tested
        # against a data.json from before the seeds, and no EVO record was ever found)
        self.pristine = data.read_bytes() if data.exists() and not FRESH_DATA else None

    def _stamp(self) -> str:
        h = hashlib.sha1()
        for f in sorted(self.root.rglob("*")):
            if f.is_file() and "node_modules" not in f.parts and "dist" not in f.parts and f.name != "data.json":
                h.update(f.as_posix().encode()); h.update(str(f.stat().st_mtime_ns).encode())
        return h.hexdigest()

    def stop(self) -> None:
        if self.proc:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def restore_data(self) -> None:
        data = self.root / "backend" / "data.json"
        if self.pristine is None:
            data.unlink(missing_ok=True)
        else:
            data.write_bytes(self.pristine)

    def start(self) -> str | None:
        self.stop()
        stamp = self._stamp()
        if stamp != self.stamp:
            r = subprocess.run(["npm", "run", "build"], cwd=self.root / "frontend", capture_output=True, text=True, timeout=300)
            if r.returncode:
                return "frontend build failed:\n" + (r.stdout + r.stderr)[-1500:]
            self.stamp = stamp
        self.restore_data()
        free_port(QA_PORT)  # a server another tool left running there would make every check fail (EADDRINUSE)
        lf = open(STATE / "server.log", "w")
        self.proc = subprocess.Popen(["npm", "start"], cwd=self.root / "backend", stdout=lf, stderr=subprocess.STDOUT,
                                     env=dict(os.environ, PORT=str(QA_PORT)), start_new_session=True)
        for _ in range(60):
            if self.proc.poll() is not None:
                break
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{QA_PORT}/", timeout=2)
                return None
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.5)
        return "server did not start:\n" + (STATE / "server.log").read_text(errors="replace")[-1500:]


def _node(pw: Path, script: str, args: list[str], timeout: int) -> str:
    path = STATE / f"s{time.time_ns()}.cjs"
    path.write_text(script, encoding="utf-8")
    try:
        r = subprocess.run(["node", str(path), *args], capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, NODE_PATH=str(pw / "node_modules")))
        out = r.stdout.strip().splitlines()
        return out[-1] if out else (r.stderr[-800:] or "no output")
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": f"timed out after {timeout}s"})


# ---------------------------------------------------------------- checks

ROLE_CHECK_JS = r"""
const { chromium } = require('@playwright/test');
const [BASE, pairsJson] = process.argv.slice(2);
const pairs = JSON.parse(pairsJson);
// Only interactive roles count as a wrong role: a heading or dialog title may share a control's name.
// Every page where the name has a wrong role is reported, even if the right role exists elsewhere
// (wholeapp s05: the home page had link "New blank workbook", the create page a heading of that name).
const ROLES = ['button', 'link', 'tab', 'checkbox', 'menuitem', 'radio', 'combobox', 'textbox'];
(async () => {
  const browser = await chromium.launch(); const page = await (await browser.newContext()).newPage();
  page.setDefaultTimeout(5000);
  const errors = []; page.on('pageerror', (e) => errors.push(`uncaught exception on ${new URL(page.url()).pathname}: ${e.message}`));
  const mismatch = {};
  const scan = async () => {
    const where = new URL(page.url()).pathname;
    for (const [allowed, name] of pairs) {
      for (const other of ROLES) {
        if (!allowed.includes(other) && await page.getByRole(other, { name, exact: true }).count()) {
          const key = `${allowed.join(' or ')} "${name}"`;
          const seen = `${other} "${name}" on ${where}`;
          mismatch[key] = mismatch[key] ? (mismatch[key].includes(seen) ? mismatch[key] : `${mismatch[key]}; ${seen}`) : seen;
        }
      }
    }
  };
  await page.goto(BASE + '/'); await page.waitForTimeout(800); await scan();
  const origin = new URL(BASE).origin, seen = new Set();
  const links = (await page.locator('a[href]').evaluateAll((els) => els.map((a) => a.href))).filter((h) => {
    const u = new URL(h); const k = u.pathname.replace(/[0-9a-f-]{6,}|\d+/gi, ':id');
    if (u.origin !== origin || seen.has(k)) return false; seen.add(k); return true; }).slice(0, 8);
  for (const h of links) { try { await page.goto(h); await page.waitForTimeout(600); await scan(); } catch (e) {} }
  // Speed on a slow CPU (the grader: one shared core, 5 s per step). wholeapp v4b: the same code scored 58 with a
  // 100-row grid and 28 with a 1000-row one (54k elements, 6.5 s per cell edit at 6x throttling).
  const slow = [];
  const sp = await (await browser.newContext()).newPage();
  await (await sp.context().newCDPSession(sp)).send('Emulation.setCPUThrottlingRate', { rate: 6 });
  for (const h of [BASE + '/', ...links]) {
    try {
      const t = Date.now(); await sp.goto(h); await sp.waitForLoadState('networkidle', { timeout: 20000 }).catch(() => {});
      const ms = Date.now() - t, n = await sp.evaluate(() => document.getElementsByTagName('*').length);
      if (n > 15000 || ms > 3000) slow.push(`page ${new URL(sp.url()).pathname} has ${n} elements and takes ${(ms / 1000).toFixed(1)} s to open on a slow CPU`);
    } catch (e) {}
  }
  console.log(JSON.stringify({ mismatch, slow: slow.slice(0, 2), errors: [...new Set(errors)].slice(0, 5) }));
  await browser.close();
})().catch((e) => console.log(JSON.stringify({ mismatch: {}, errors: ['check failed: ' + e.message.split('\n')[0]] })));
"""


# Click every entry point on the home page, then one requirement-named button on the page it leads to, and report
# requests whose path carries undefined/null/NaN and uncaught exceptions. v4h sheet scored 5: "Create" called
# save(path, "POST", {}) against save(path, {method, body}), so it fetched the list and opened /app?id=undefined;
# the hidden tests start from that button, and no page-load check clicks anything.
CLICK_PROBE_JS = r"""
const { chromium } = require('@playwright/test');
const [BASE, buttonsJson, itemsJson] = process.argv.slice(2);
const buttons = JSON.parse(buttonsJson), items = JSON.parse(itemsJson || '[]');
const DESTRUCTIVE = /delete|remove|sign out|log out|discard|reset|clear|cancel|close/i;
const BAD = /(^|[\/=])(undefined|null|NaN)([\/&?#]|$)/;
(async () => {
  const browser = await chromium.launch();
  const problems = [];
  let clicks = 0;
  // one fresh page per attempt, collecting undefined/null/NaN requests and uncaught exceptions
  async function attempt(url, steps) {
    const ctx = await browser.newContext(); const page = await ctx.newPage(); page.setDefaultTimeout(4000);
    const bad = [];
    page.on('request', (r) => { const u = new URL(r.url()); if (BAD.test(u.pathname + u.search)) bad.push(`${r.method()} ${u.pathname}${u.search}`); });
    page.on('framenavigated', (f) => { if (f === page.mainFrame()) { const u = new URL(f.url()); if (BAD.test(u.pathname + u.search)) bad.push(`opened ${u.pathname}${u.search}`); } });
    page.on('pageerror', (e) => bad.push(`uncaught exception: ${e.message}`));
    let done = [];
    try {
      await page.goto(url); await page.waitForTimeout(500);
      done = await steps(page);
    } catch (e) {}
    const at = page.url();
    await ctx.close();
    return { bad: [...new Set(bad)], done, at };
  }
  const named = (page, role, n) => page.getByRole(role, { name: n, exact: true });
  const report = (where, done, bad) => { if (bad.length) problems.push(`clicking ${done.map((s) => `"${s}"`).join(' then ')} ${where}: ${bad.slice(0, 3).join('; ')}`); };

  // 1. every entry point of the home page, then one requirement-named button on the page it opens
  const home = await (await browser.newContext()).newPage();
  await home.goto(BASE + '/'); await home.waitForTimeout(800);
  const entries = [...new Set(await home.evaluate(() => [...document.querySelectorAll('button, a[href], [role=button], [role=link]')]
    .filter((e) => e.offsetParent !== null)
    .map((e) => (e.getAttribute('aria-label') || e.textContent || '').trim().replace(/\s+/g, ' '))
    .filter((t) => t && t.length <= 60)))].filter((t) => !DESTRUCTIVE.test(t)).slice(0, 8);
  const links = await home.locator('a[href]').evaluateAll((es) => es.map((a) => a.href));
  let work = null;
  for (const entry of entries) {
    const r = await attempt(BASE + '/', async (page) => {
      const done = [entry];
      await named(page, 'button', entry).or(named(page, 'link', entry)).first().click(); await page.waitForTimeout(1000);
      for (const n of buttons) {
        if (n === entry || DESTRUCTIVE.test(n)) continue;
        const b = named(page, 'button', n);
        if ((await b.count()) === 1 && (await b.isVisible())) { await b.click(); done.push(n); await page.waitForTimeout(1200); break; }
      }
      return done;
    });
    clicks += r.done.length;
    report('from the home page', r.done, r.bad);
    if (!work && !r.bad.length && r.done.length === 2 && r.at.split('#')[0] !== BASE + '/') work = r.at;  // e.g. the record just created
  }
  if (!work) work = links.find((h) => h.startsWith(BASE) && new URL(h).pathname !== '/') || null;

  // 2. on that work page: every requirement-named button, and every named menu item it opens
  if (work) {
    const where = `on ${new URL(work).pathname}`;
    for (const n of buttons) {
      if (clicks >= 40 || DESTRUCTIVE.test(n)) continue;
      const r = await attempt(work, async (page) => {
        const b = named(page, 'button', n);
        if ((await b.count()) !== 1 || !(await b.isVisible())) return [];
        await b.click(); await page.waitForTimeout(800); return [n];
      });
      if (!r.done.length) continue;
      clicks += 1; report(where, r.done, r.bad);
      const opened = await attempt(work, async (page) => {
        await named(page, 'button', n).click(); await page.waitForTimeout(500);
        // every visible menu item it opens: the requirement often names them only in prose ("choose Sort range from the Data menu")
        const shown = await page.getByRole('menuitem').evaluateAll((es) => es.filter((e) => e.offsetParent !== null)
          .map((e) => (e.getAttribute('aria-label') || e.textContent || '').trim().replace(/\s+/g, ' ')));
        return [...new Set([...items, ...shown])].filter((m) => m && shown.includes(m) && !DESTRUCTIVE.test(m)).slice(0, 8);
      });
      for (const m of opened.done) {
        if (clicks >= 40) break;
        const r2 = await attempt(work, async (page) => {
          await named(page, 'button', n).click(); await page.waitForTimeout(400);
          await named(page, 'menuitem', m).first().click(); await page.waitForTimeout(900); return [n, m];
        });
        clicks += 2; report(where, r2.done, r2.bad);
      }
    }
  }
  console.log(JSON.stringify({ problems, work, clicks }));
  await browser.close();
})().catch((e) => console.log(JSON.stringify({ problems: [] })));
"""


def requirement_pairs(text: str) -> list[list]:
    """[[allowed roles], name] for every control the requirement names. A name the requirement gives
    to several roles ("Sign in" is a link on the home page and the form's button) accepts all of them:
    checking each role alone flagged both, and the QA model flipped them back and forth."""
    roles: dict[str, list[str]] = {}
    for c in named_controls(text, limit=200):
        mm = re.match(r'(\w+) "(.+)"$', c)
        if mm and "<" not in mm.group(2):
            role = {"textbox": "textbox", "searchbox": "textbox", "input": "textbox", "field": "textbox",
                    "select": "combobox"}.get(mm.group(1), mm.group(1))
            roles.setdefault(mm.group(2), [])
            if role not in roles[mm.group(2)]:
                roles[mm.group(2)].append(role)
    return [[r, name] for name, r in roles.items() if any(x in CHECKED_ROLES for x in r)]


# Seed values the tests would have to type or find: identity-like fields of the startup data, and password-like
# literals in seed sources. v4d gave bob-reviewer "Bob-password-123!" (the requirement only gives alice-dev's
# password), g12 added carol-maintainer, dana-admin and others; a test can know none of them.
# Only credentials: a test must type them. Invented record titles (g12 had 20, e.g. "Merge ready") support
# scenarios and are never typed, so removing them would risk more than it gains.
_SEED_KEYS = re.compile(r"^(?:user_?name|login|password|handle)$", re.I)
_PASSWORD_LIKE = re.compile(r"""["'`]((?=[^"'`\s]*\d)(?=[^"'`\s]*[!@#$%^&*])[^"'`\s]{8,64})["'`]""")
_NOT_A_NAME = re.compile(r"^(?:[\w.+-]+@[\w.-]+|\d[\d\s.:/-]*|[0-9a-f]{8,}|\w*[_-]?[0-9a-z]*\d[0-9a-z]{5,}|.{0,2}|"
                         r"\d{4}-\d\d-\d\d.*)$", re.I)


def invented_seed_values(root: Path, requirement: str) -> list[str]:
    """Seed usernames and passwords that never appear in the requirement (read after the app has served pages)."""
    found: dict[str, str] = {}

    def walk(x, where: str) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(v, str) and _SEED_KEYS.match(str(k)):
                    found.setdefault(v.strip(), f"{where} field {k}")
                else:
                    walk(v, where)
        elif isinstance(x, list):
            for v in x:
                walk(v, where)

    backend = root / "backend"
    for f in sorted(backend.rglob("*.json")):
        if "node_modules" in f.parts or f.name.startswith("package"):
            continue
        try:
            walk(json.loads(f.read_text(encoding="utf-8")), f.relative_to(root).as_posix())
        except (OSError, ValueError):
            continue
    for f in sorted(backend.rglob("*.js")):
        if "node_modules" not in f.parts and "seed" in f.name.lower():
            for m in _PASSWORD_LIKE.finditer(f.read_text(encoding="utf-8", errors="replace")):
                found.setdefault(m.group(1), f"{f.relative_to(root).as_posix()} (a password-like literal)")
    slug = lambda s: re.sub(r"[^a-z0-9]+", "-", s.lower())
    known = slug(requirement)
    out = []
    for value, where in found.items():
        if _NOT_A_NAME.match(value) or value in requirement or f"-{slug(value).strip('-')}-" in f"-{known}-":
            continue
        if "/" in value and all(p in requirement for p in value.split("/") if p):
            continue  # owner/name composed of names the requirement gives
        out.append(f'"{value}" ({where})')
    return out


class Checks:
    def __init__(self, app: App, pw: Path, pairs: list[list[str]], spec: Path | None, requirement: str = "",
                 audit: str | None = None) -> None:
        self.app, self.pw, self.pairs, self.spec, self.requirement = app, pw, pairs, spec, requirement
        self.audit = audit  # requirement checks written by the model from the requirement (audit_gen.py)
        self.audit_failing: list[str] | None = None

    def audit_problems(self, full: bool) -> list[str]:
        """Run the requirement checks: all of them at the start and the end, only the ones that failed at the start
        while the QA model works (the whole set takes minutes)."""
        if not self.audit:
            return []
        only = None if full or self.audit_failing is None else self.audit_failing
        if only == []:
            return []
        if (err := self.app.start()):
            return [err]
        args = [f"http://127.0.0.1:{QA_PORT}"] + ([json.dumps(only)] if only is not None else [])
        try:
            res = json.loads(_node(self.pw, self.audit, args, 1200))
        except json.JSONDecodeError:
            return []
        failed = [r for r in res.get("results") or [] if not r.get("ok")]
        if self.audit_failing is None:
            self.audit_failing = [r["name"] for r in failed]
        out = [f'requirement check "{r["name"]}" fails: {r.get("detail", "")}' for r in failed]
        if full and failed:  # the steps once, so the model replays a check in the browser instead of guessing
            helpers = self.audit.split("const CHECKS = {", 1)[0].split("const pause", 1)[-1]
            out[0] += ("\nThese checks were written by a model from the requirement and can themselves be wrong: replay "
                       "one in the browser and compare with the requirement before changing the app. Steps of the "
                       "failing checks:\n" + "\n".join(f'- {r["name"][:70]}: {r.get("steps", "")[:700]}' for r in failed[:6])
                       + "\nHelpers they call:\n" + re.sub(r"\n\s*\n", "\n", helpers)[:3500])
        return out

    def spec_problems(self) -> list[str]:
        """Run the scenario spec once, in parallel workers, against one server start (the scenarios use separate seed
        records), and report each failing test with its step, page text and page errors."""
        if self.spec is None:
            return []
        work = STATE / "run"
        (work / "tests").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.spec, work / "tests" / "common.spec.ts")
        report = work / "report.json"
        report.unlink(missing_ok=True)
        (work / "playwright.config.cjs").write_text(
            # an action that times out fails inside the test (the page is still open for the failure detail); a test
            # timeout would close the page first and the detail would be empty
            f"module.exports = {{ testDir: './tests', timeout: 30000, expect: {{ timeout: 5000 }}, workers: {SPEC_WORKERS}, fullyParallel: true,"
            f" reporter: [['json', {{ outputFile: 'report.json' }}]],"
            f" use: {{ baseURL: 'http://127.0.0.1:{QA_PORT}', actionTimeout: 8000, navigationTimeout: 10000 }} }};\n", encoding="utf-8")
        if (err := self.app.start()):
            return [err]
        try:
            subprocess.run([str(self.pw / "node_modules" / ".bin" / "playwright"), "test", "-c", "playwright.config.cjs"],
                           cwd=work, capture_output=True, text=True, timeout=600, env=dict(os.environ, NODE_PATH=str(self.pw / "node_modules")))
        except subprocess.TimeoutExpired:
            return ["common-path tests timed out"]
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ["common-path tests produced no report"]
        out = []

        def walk(suite):
            for spec in suite.get("specs", []):
                tests = spec.get("tests") or []
                results = tests[0].get("results") if tests else None
                if not results:
                    continue
                r = results[-1]
                if r.get("status") == "passed":
                    continue
                msg = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(e.get("message", "") for e in r.get("errors") or []))
                detail = [l.strip() for l in msg.splitlines()
                          if re.search(r"Error:|Expected|Received|waiting for|resolved to|FAILED STEP:|SHOWED:|PAGE ERRORS:", l)][:7]
                detail = [re.sub(r"\.or\(.*\)\)+", "", l) if "waiting for" in l else l for l in detail]  # role alternatives are noise
                out.append(f'common-path test "{spec.get("title", "")}" fails: ' + " / ".join(dict.fromkeys(detail))[:900])
            for child in suite.get("suites", []):
                walk(child)

        for suite in data.get("suites", []):
            walk(suite)
        return out

    def run(self, full: bool = True) -> list[str]:
        if (err := self.app.start()):
            return [err]
        try:
            res = json.loads(_node(self.pw, ROLE_CHECK_JS, [f"http://127.0.0.1:{QA_PORT}", json.dumps(self.pairs)], 180))
        except json.JSONDecodeError:
            res = {}
        problems = [f"the requirement names {k}, but the app renders it as {v}" for k, v in (res.get("mismatch") or {}).items()]
        problems += [f"{s}; the tests run on one slow CPU core and wait at most 5 s per step, so render only about 100 "
                     "rows (or only the visible part) and update only the changed elements after an edit, including "
                     "the seed data's sizes" for s in res.get("slow") or []]
        # after the pages were served, so apps that write their data on first use have written it
        invented = [] if SKIP_SEED_PASSWORDS else invented_seed_values(self.app.root, self.requirement)
        passwords = [v for v in invented if "password" in v]
        if passwords:
            problems.append("the seed uses passwords the requirement never gives: " + ", ".join(passwords[:8])
                            + "; tests can only sign in with values the requirement states, so every seeded account "
                            "uses the password the requirement gives")
        if len(invented) > len(passwords):
            log("seed accounts the requirement never names (not changed): "
                + ", ".join(v for v in invented if v not in passwords)[:300])
        # last: it creates records (the common-path tests below restart the app on the startup data)
        buttons = [name for roles, name in self.pairs if "button" in roles]
        items = [name for roles, name in self.pairs if "menuitem" in roles]
        try:
            clicks = json.loads(_node(self.pw, CLICK_PROBE_JS, [f"http://127.0.0.1:{QA_PORT}", json.dumps(buttons), json.dumps(items)], 420))
        except json.JSONDecodeError:
            clicks = {}
        problems += [f"{p}: this control is wired to the wrong call, value or state"
                     for p in clicks.get("problems") or []]
        problems += list(res.get("errors") or []) + self.audit_problems(full) + self.spec_problems()
        if self.app.proc and self.app.proc.poll() is not None:
            problems.append("the server process exited while the checks ran (every later page load was refused); its "
                            "output:\n" + (STATE / "server.log").read_text(errors="replace")[-1500:])
        return problems


# ---------------------------------------------------------------- tools

BROWSER_JS = r"""
const { chromium, expect } = require('@playwright/test');
const BASE = process.argv[2];
(async () => {
  const browser = await chromium.launch(); const page = await (await browser.newContext()).newPage();
  page.setDefaultTimeout(8000);
  const logs = []; page.on('pageerror', (e) => logs.push('pageerror: ' + e.message));
  page.on('response', (res) => { if (res.status() >= 400 && logs.length < 12) { const u = new URL(res.url());
    res.text().then((b) => logs.push(`server ${res.request().method()} ${u.pathname} -> ${res.status()}: ${b.replace(/\s+/g, ' ').slice(0, 200)}`)).catch(() => {}); } });
  page.on('console', (m) => { if (m.type() === 'error') logs.push('console.error: ' + m.text()); });
  const goto = (p) => page.goto(BASE + p);
  let error = null, result;
  try { result = await (async () => { /*CODE*/ })(); } catch (e) { error = e.message.split('\n').slice(0, 8).join('\n'); }
  let snapshot = ''; try { snapshot = await page.locator('body').ariaSnapshot(); } catch (e) {}
  let returned; try { returned = result === undefined ? undefined : JSON.parse(JSON.stringify(result)); } catch (e) { returned = String(result); }
  console.log(JSON.stringify({ ok: !error, error, returned, url: page.url(), logs: logs.slice(0, 8), snapshot: snapshot.slice(0, 3000) }));
  await browser.close();
})();
"""

TOOLS = [
    {"name": "run_checks", "description": "Rebuild, restart on the seed data and re-run all checks; returns the remaining problems.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "browser", "description": "Run a short Playwright snippet on the freshly restarted app (seed data restored). "
     "Variables: page, expect, goto(path). Returns ok/error, the value your code returns (use `return {...}` to see "
     "DOM facts), final URL, page errors and the accessibility snapshot.",
     "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "list_files", "description": "List the app's source files with sizes.", "parameters": {"type": "object", "properties": {}}},
    {"name": "read_file", "description": "Read lines of a source file (1-based, inclusive, at most 200).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start": {"type": "integer"},
                                                     "end": {"type": "integer"}}, "required": ["path"]}},
    {"name": "search_code", "description": "Find lines containing a text in the app's sources.",
     "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "edit_file", "description": "Replace one exact occurrence of old_text with new_text in a source file.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"},
                                                     "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "search_requirement", "description": "Sentences of the requirement containing a text.",
     "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "finish", "description": "Stop: everything reported is fixed or should not be changed.",
     "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}},
]

QA_SYSTEM = """You are the QA engineer of a generated web application. Automated Playwright tests will grade it
against the requirement, locating every control by its ARIA role and exact accessible name. The harness found
the problems below. Reproduce each with the browser tool when useful, fix the source with edit_file, confirm
with run_checks or browser, then call finish. Change only what a problem needs; keep every other feature.
The requirement is the authority, not the tests: search_requirement before changing behaviour. If a failure
comes from the test itself (an ambiguous locator, a value the requirement never states) while the app does
what the requirement says, do not change the app; say so in finish.
A "requirement check" was written by hand from the requirement text and passes on a correct app: treat it as
certain, find its REQ id with search_requirement, and fix the app. Several checks failing the same way usually
share one cause (an uncaught exception, focus left in a text box, a wrong call): fix that first.
Work from evidence: first replay one failing check with the browser tool (its steps are listed) and return the DOM
facts you need, then read only the code that the result points to. Do not read whole files up front.
You have at most {calls} tool calls; be efficient (read only the lines you need)."""


def _sources(root: Path) -> list[Path]:
    return [f for f in sorted(root.rglob("*")) if f.is_file() and "node_modules" not in f.parts and "dist" not in f.parts
            and f.suffix in (".js", ".mjs", ".cjs", ".html", ".css") and f.name != "package-lock.json"]


def _inside(root: Path, rel: str) -> Path | None:
    p = (root / str(rel)).resolve()
    return p if str(p).startswith(str(root.resolve())) and p.is_file() else None


def _tool(name: str, args: dict, checks: Checks, requirement: str) -> str:
    root = checks.app.root
    if name == "run_checks":
        probs = checks.run(full=False)
        return "all checks pass" if not probs else "\n".join(f"- {p}" for p in probs)
    if name == "browser":
        if (err := checks.app.start()):
            return err
        return _node(checks.pw, BROWSER_JS.replace("/*CODE*/", str(args.get("code", ""))), [f"http://127.0.0.1:{QA_PORT}"], 60)
    if name == "list_files":
        return "\n".join(f"{f.relative_to(root).as_posix()} ({f.stat().st_size} bytes)" for f in _sources(root))
    if name == "read_file":
        p = _inside(root, args.get("path", ""))
        if not p:
            return "no such file"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        s = max(1, int(args.get("start") or 1))
        e = min(len(lines), int(args.get("end") or len(lines)), s + 199)
        more = f"\n... ({len(lines)} lines total)" if e < len(lines) else ""
        return "\n".join(f"{i}: {lines[i - 1]}" for i in range(s, e + 1)) + more
    if name == "search_code":
        needle, out = str(args.get("text", "")), []
        for f in _sources(root):
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if needle and needle in line:
                    out.append(f"{f.relative_to(root).as_posix()}:{i}: {line.strip()[:200]}")
        return "\n".join(out[:40]) or "no match"
    if name == "edit_file":
        p = _inside(root, args.get("path", ""))
        if not p:
            return "no such file"
        text, old = p.read_text(encoding="utf-8"), str(args.get("old_text", ""))
        n = text.count(old) if old else 0
        if n != 1:
            return f"old_text found {n} times; it must match exactly once"
        p.write_text(text.replace(old, str(args.get("new_text", "")), 1), encoding="utf-8")
        return "edited"
    if name == "search_requirement":
        needle = str(args.get("text", "")).lower()
        hits = [s for s in re.split(r"(?<=[.!?])\s+", requirement) if needle and needle in s.lower()]
        return "\n".join(dict.fromkeys(hits))[:4000] or "no match"
    return "unknown tool"


# ---------------------------------------------------------------- entry point

_AUDIT: dict = {}


def start_audit(tree: dict | None, requirement: str) -> None:
    """Write the requirement checks in a background thread (they need only the requirement), so they are ready when
    QA starts without adding to the run time."""
    if not tree:
        return
    import threading  # noqa: PLC0415

    def work() -> None:
        try:
            import audit_gen  # noqa: PLC0415
            path = audit_gen.generate(tree, requirement, areas_from_tree(tree), _chat, log, STATE / "requirement_checks.cjs",
                                      requirement_name_patterns(requirement))
            _AUDIT["path"] = path
        except Exception as e:  # noqa: BLE001
            log(f"requirement checks not written ({type(e).__name__}: {str(e)[:200]})")
    _AUDIT["thread"] = threading.Thread(target=work, daemon=True)
    _AUDIT["thread"].start()


def audit_script(wait: float = 900) -> str | None:
    t = _AUDIT.get("thread")
    if t is None:
        return None
    t.join(wait)
    path = _AUDIT.get("path")
    return path.read_text(encoding="utf-8") if path and path.exists() else None


def run_qa(out: Path, tree: dict | None, requirement: str, pw: Path | None, deadline: float) -> None:
    """The whole QA phase; never raises (it is an improvement pass, not a requirement to finish)."""
    try:
        waited = time.time()
        audit_script()  # waiting for the requirement checks is not QA time (a fast build left QA 14 calls)
        deadline += time.time() - waited
        _run(out, tree, requirement, pw, deadline)
    except Exception as e:  # noqa: BLE001
        log(f"stopped by an error ({type(e).__name__}: {str(e)[:200]}); the app is kept as it is")


def _run(out: Path, tree: dict | None, requirement: str, pw: Path | None, deadline: float) -> None:
    if pw is None or not (pw / "node_modules" / ".bin" / "playwright").exists():
        log("skipped: no Playwright available")
        return
    started = time.time()
    spec = None
    if tree and scenarios_are_steps(tree):  # concrete scenarios: one checked test per scenario
        spec = scenario_spec(tree, requirement)
    # templated scenarios: the common-path tests below (description-written tests failed 9 of 10 on a 58-point
    # sheet app, mostly by never opening a workbook first, and QA changed nothing in 33 calls)
    if spec is None and tree:
        spec = common_spec(tree, requirement)
    app = App(out)
    checks = Checks(app, pw, requirement_pairs(requirement), spec, requirement, audit_script())
    backup = {f: f.read_bytes() for f in _sources(out)}
    before = checks.run()
    log(f"{len(checks.pairs)} named controls; {len(before)} problem(s) at start")
    for p in before:
        log(f"  - {p[:220]}")
    if not before:
        app.stop()
        return
    certain = [p for p in before if not p.startswith("common-path test")]
    possible = [p for p in before if p.startswith("common-path test")]
    messages = [{"role": "system", "content": QA_SYSTEM.format(calls=QA_CALLS)},
                {"role": "user", "content":
                 ("CERTAIN problems (checked by code against the requirement text) - fix these first, with as few reads "
                  "as possible:\n" + "\n".join(f"- {p}" for p in certain) + "\n\n" if certain else "")
                 + ("POSSIBLE problems (generated tests; a test may itself be wrong) - confirm with the browser tool and "
                    "search_requirement before changing anything, and ignore those where the app follows the requirement:\n"
                    + "\n".join(f"- {p}" for p in possible) + "\n\n" if possible else "")
                 + "Source files:\n" + _tool("list_files", {}, checks, requirement)}]
    calls, tokens = 0, [0, 0]
    limit = min(started + QA_SECONDS, deadline)
    while calls < QA_CALLS and time.time() < limit:
        r = _chat({"messages": messages, "max_tokens": 16384, "thinking": {"type": "disabled"},
                   "tools": [{"type": "function", "function": t} for t in TOOLS]}, 300, "qa turn")
        if not r:
            break
        u = r.get("usage") or {}
        tokens[0] += u.get("prompt_tokens", 0); tokens[1] += u.get("completion_tokens", 0)
        msg = r["choices"][0]["message"]
        messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
        if msg.get("content"):
            log(f"model: {msg['content'].strip()[:240]}")
        if not msg.get("tool_calls"):
            break
        finished = False
        for tc in msg["tool_calls"]:
            calls += 1
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = f"finished: {args.get('summary', '')}" if name == "finish" else _tool(name, args, checks, requirement)
            shown = {k: (v[:100] + "..." if isinstance(v, str) and len(v) > 100 else v) for k, v in args.items()}
            log(f"#{calls} {name}({json.dumps(shown, ensure_ascii=False)[:220]}) -> {result[:180].replace(chr(10), ' | ')}")
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result[:6000]})
            finished = finished or name == "finish"
        if finished:
            break
    after = checks.run()
    if len(after) > len(before):
        for f, data in backup.items():
            f.write_bytes(data)
        for f in _sources(out):
            if f not in backup:
                f.unlink()
        log(f"checks got worse ({len(before)} -> {len(after)}); every QA edit reverted")
    app.stop()
    app.restore_data()
    log(f"done: {calls} tool calls, {int(time.time() - started)}s, tokens in/out {tokens[0]}/{tokens[1]}; "
        f"problems {len(before)} -> {len(after)}")
