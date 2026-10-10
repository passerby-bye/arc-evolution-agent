"""Requirement checks written by the model the way a person writes them by hand: first a small library of helpers
(one per interaction the requirement describes: open a dialog, fill its fields, confirm), then one check per stated
behaviour that builds its own data with the helpers and asserts what the sentence states. Free JavaScript, so file
uploads, downloads and context menus can be expressed; code keeps it grounded:
- every helper and check carries the requirement sentence it implements, and every UI string it uses must come from
  that sentence (a helper) or from its own feature's text (a check); a name that exists elsewhere in the requirement
  is not enough (per-area tests used the rename dialog's "Workbook name" box while creating a workbook);
- each piece must parse (node --check) on its own.
Generic: nothing here knows a particular task. Output: a .cjs file run like `node file <base url>` that prints
{"results":[{name, ok, detail}]}."""
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HEADER = r"""// Requirement checks written by the model from the public requirement (audit_gen.py).
const { chromium, expect } = require('@playwright/test');
const fs = require('fs');
const [BASE, onlyJson] = process.argv.slice(2);
const only = onlyJson ? JSON.parse(onlyJson) : null;
const T = { timeout: 5000 };
const R = (p, role, name) => p.getByRole(role, { name, exact: true });
const uid = (s) => `${s}-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`;
const pause = (p, ms = 300) => p.waitForTimeout(ms);
async function choose(scope, label, option) {  // a native <select> or a custom combobox with options
  const box = scope.getByLabel(label, { exact: true }).first();
  if ((await box.evaluate((e) => e.tagName)) === 'SELECT') return box.selectOption({ label: option });
  await box.click(); const page = typeof scope.page === 'function' ? scope.page() : scope;
  await page.getByRole('option', { name: option, exact: true }).first().click();
}
"""

RUNNER = r"""
(async () => {
  const browser = await chromium.launch(); const results = [];
  for (const [name, fn] of Object.entries(CHECKS)) {
    if (only && !only.includes(name)) continue;
    const ctx = await browser.newContext({ acceptDownloads: true, permissions: ['clipboard-read', 'clipboard-write'] });
    const p = await ctx.newPage(); p.setDefaultTimeout(6000);
    const errors = []; p.on('pageerror', (e) => errors.push(e.message));
    let r;
    try { r = await fn(p); } catch (e) { r = e.message.split('\n').map((l) => l.trim()).filter(Boolean).slice(0, 3).join(' / ').replace(/\x1b\[[0-9;]*m/g, ''); }
    const ok = r === true || r === undefined;
    results.push({ name, ok, detail: ok ? '' : String(r).slice(0, 300) + (errors.length ? ` (uncaught: ${errors[0].slice(0, 120)})` : ''),
      steps: ok ? '' : fn.toString().replace(/\s+/g, ' ').slice(0, 900) });
    await ctx.close();
  }
  console.log(JSON.stringify({ results }));
  await browser.close();
})().catch((e) => console.log(JSON.stringify({ results: [], error: String(e).slice(0, 300) })));
"""

EXAMPLE = r"""Example of the format, for an unrelated notes application whose requirement says: 'The home page has a "New note"
button. Clicking it opens a dialog named "New note" with a text box labeled "Title" and a "Save" button. A title that
is empty after trimming is rejected with "Title is required". Saved notes are listed on the home page as links named
by their title, and remain after a refresh.'

// quote: Clicking it opens a dialog named "New note" with a text box labeled "Title" and a "Save" button.
async function createNote(p, title) {
  await p.goto(BASE + '/'); await R(p, 'button', 'New note').click();
  const d = R(p, 'dialog', 'New note'); await d.getByLabel('Title', { exact: true }).fill(title);
  await d.getByRole('button', { name: 'Save', exact: true }).click(); await pause(p);
}

// quote: Saved notes are listed on the home page as links named by their title, and remain after a refresh.
'NOTE-1 a saved note is listed by its title and survives a refresh': async (p) => {
  const t = uid('note'); await createNote(p, t);
  await p.goto(BASE + '/'); await expect(R(p, 'link', t)).toBeVisible(T);
  await p.reload(); await expect(R(p, 'link', t)).toBeVisible(T);
},
// quote: A title that is empty after trimming is rejected with "Title is required".
'NOTE-1 a blank title shows "Title is required"': async (p) => {
  await createNote(p, '   '); await expect(p.getByText('Title is required', { exact: true })).toBeVisible(T);
},
"""

HELPERS_PROMPT = """\
Automated browser tests will grade a web application built from the requirement below. Write the helper
library for requirement checks that reproduce those tests: one async helper per interaction the requirement
describes in detail (reaching a starting point from the home page, opening a dialog or menu and confirming it,
entering a value through the editor the requirement names, uploading a file through a file control, ...), 8 to
20 helpers. Available: R(p, role, name) (exact role lookup), uid(prefix), pause(p, ms), T (expect options),
choose(scope, label, option) (picks an option of a labeled select or combobox, native or custom), BASE, expect, fs. Use Playwright as usual (setInputFiles with a buffer for file controls, waitForEvent('download'),
click({{ button: 'right' }}) for context menus).

Rules (code checks every helper and drops those that break one):
- Directly above each helper, one line `// quote: <sentence>` with the requirement sentence it performs, copied
  verbatim. Every string literal in the helper (names, labels, texts) must appear in that sentence; values come
  in as parameters.
- Locate elements by role with the exact accessible name, by exact label, or by exact text. Never type into a
  grid or table cell directly; use the editor the requirement names.
- Do not rely on seed records except ones every scenario names; a helper that reaches a starting point either
  opens such a record or creates a fresh one.

{example}

REQUIREMENT
{requirement}

Reply with only the helpers (and any small constants they need), in one ```js block.
"""

CHECKS_PROMPT = """\
Write requirement checks for one area of a web application, {area}. Automated browser tests will grade it;
these checks reproduce them so the app can be repaired first. Each check is an entry of an object literal:
'<feature id> <what it checks>': async (p) => {{ ... }}, returning nothing when it passes (throw or return a
short string describing the difference when it fails). Use the helpers below (and R, uid, pause, T, expect, BASE,
fs, choose(scope, label, option) for any labeled select or combobox). Call only functions defined here.

HELPERS (already defined)
{helpers}

AREA REQUIREMENT
{area_text}

Rules (code checks every check and drops those that break one):
- One to three checks per feature of the area, each about one behaviour a sentence of that feature states.
  Directly above each check, one line `// quote: <sentence>` copied verbatim from that feature. The name
  starts with that feature's id.
- Every string literal in a check (names, labels, messages, expected texts) must appear in that feature's own
  text, or be data the check itself enters; build your own data (uid) and assert the result the sentence states,
  including what persists after a reload when the sentence says so.
- Locate elements by role with the exact accessible name, by exact label, or by exact text. Cells of a grid
  have role gridcell (role cell is for tables).

{example}

Reply with only the check entries, in one ```js block.
"""


def _norm(s: str) -> str:
    s = (s or "").translate(str.maketrans("‘’“”", "''\"\""))
    return re.sub(r"\s+", " ", s).strip().lower()


def quoted(quote: str, text: str) -> bool:
    """The quote is copied from the text: each of its sentences (or pieces around "...") appears verbatim, at least
    one of them substantial (models join two sentences or skip a clause; a paraphrase still fails)."""
    pieces = [x.strip(" .;:\"'") for x in re.split(r"\s*(?:\.\.\.|…)\s*|(?<=[.;:])\s+", _norm(quote))]
    pieces = [x for x in pieces if len(x) >= 8]
    return bool(pieces) and max(map(len, pieces)) >= 15 and all(x in text for x in pieces)


_COORD = re.compile(r"^(?:[A-Z]{1,3}\d{1,5}|[A-Z]{1,3}|-?[\d.,]+%?)$")  # coordinates and numbers
_LIT = r"""('(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"|`(?:[^`\\]|\\.)*`)"""
_LOCATOR = re.compile(r"\bR\(\s*\w+\s*,\s*['\"]\w+['\"]\s*,\s*" + _LIT + r"|getByRole\([^,()]+,\s*\{\s*name:\s*" + _LIT
                      + r"|getBy(?:Label|Text|Placeholder|Title)\(\s*" + _LIT)


def locator_names(code: str) -> list[str]:
    """The names a piece of code finds elements by (role names, labels, texts); typed data is not among them.
    A template literal's ${...} parts become <x>, to be matched against the requirement's placeholder names."""
    out = []
    for m in _LOCATOR.finditer(code):
        lit = next(g for g in m.groups() if g is not None)
        body = lit[1:-1].replace("\\'", "'").replace('\\"', '"')
        if lit[0] == "`":
            body = re.sub(r"\$\{[^}]*\}", "<x>", body)
        if body.strip() and body.strip() != "<x>" and not _COORD.match(body.strip()):
            out.append(body)
    return out


def _allowed(name: str, text: str, patterns) -> bool:
    if "<x>" in name:  # a name built from a template: its fixed parts must be in the text, or match a placeholder name
        fixed = [f.strip() for f in name.split("<x>") if f.strip()]
        return all(_norm(f) in text for f in fixed) or any(p.match(name.replace("<x>", "X")) for p in patterns)
    return _norm(name) in text or any(p.match(name) for p in patterns)


_GLOBALS = {"R", "uid", "pause", "choose", "expect", "require", "String", "Number", "Object", "Array", "JSON", "Math",
            "Promise", "Buffer", "Date", "parseFloat", "parseInt", "isNaN", "RegExp", "Error", "Boolean", "Set", "Map",
            "setTimeout", "encodeURIComponent", "decodeURIComponent", "if", "for", "while", "switch", "catch", "return",
            "function", "async", "await", "typeof", "new",
            # browser globals used inside page.evaluate callbacks
            "getComputedStyle", "document", "window", "alert", "fetch", "console", "Symbol", "Intl", "structuredClone"}


_CELL_ROLE = re.compile(r"""(?:R\(\s*\w+\s*,|getByRole\()\s*['"]cell['"]""")


def undefined_calls(code: str, defined: set) -> list[str]:
    """Functions the code calls (not methods) that are neither defined in it, nor helpers, nor globals."""
    local = set(re.findall(r"(?:function\s+|(?:const|let|var)\s+)(\w+)", code))
    called = set(re.findall(r"(?<![\w.$])([A-Za-z_]\w*)\s*\(", code))
    return sorted(called - local - defined - _GLOBALS)


def _parses(code: str) -> str | None:
    """None when the code parses as a script, else node's first error line."""
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False, encoding="utf-8") as f:
        f.write(code)
    try:
        r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True, timeout=30)
    finally:
        Path(f.name).unlink(missing_ok=True)
    return None if r.returncode == 0 else (r.stderr.strip().splitlines() or ["syntax error"])[-1][:160]


def _units(block: str) -> list[tuple[str, str]]:
    """(quote, code) per piece of a reply: code from one `// quote:` line to the next."""
    pieces = re.split(r"^[ \t]*// quote:[ \t]*", block, flags=re.M)
    out = []
    for piece in pieces[1:]:
        quote, _, code = piece.partition("\n")
        out.append((quote.strip().strip('"“”'), code.rstrip()))
    return out


def _code_block(reply: str) -> str:
    m = re.search(r"```(?:js|javascript)?\n(.*?)```", reply, re.S)
    return m.group(1) if m else reply


def _ask(chat, prompt: str, label: str) -> str:
    r = chat({"messages": [{"role": "user", "content": prompt}], "max_tokens": 32768,
              "thinking": {"type": "enabled"}, "reasoning_effort": "low"}, 900, label)
    return ((r or {}).get("choices") or [{}])[0].get("message", {}).get("content") or ""


def nodes_by_id(tree: dict) -> dict:
    """{id: (node text, ancestors' text)} for every node."""
    out = {}

    def walk(n: dict, above: str) -> None:
        text = f"{n.get('name', '')}. {n.get('description', '')}"
        for sc in n.get("scenarios") or []:
            text += " " + " ".join(str(st.get("content", "")) for st in sc.get("steps") or [] if isinstance(st, dict))
        out[str(n.get("id"))] = (text, above)
        for c in n.get("children") or []:
            if isinstance(c, dict):
                walk(c, above + " " + str(n.get("description") or ""))
    walk(tree, "")
    return out


def _helpers(chat, log, requirement: str, conventions: str, patterns) -> tuple[list, list, dict]:
    """One attempt at the helper library: (kept helpers, their names, {name: quote})."""
    req = _norm(requirement)
    helpers, names, quotes = [], [], {}
    for quote, code in _units(_code_block(_ask(chat, HELPERS_PROMPT.format(example=EXAMPLE, requirement=requirement[:60000]),
                                               "audit helpers"))):
        m = re.search(r"(?:async\s+)?function\s+(\w+)|const\s+(\w+)\s*=", code)
        name = (m.group(1) or m.group(2)) if m else None
        bad = []
        if not quoted(quote, req):
            bad.append("its quote is not a sentence of the requirement")
        allowed = _norm(quote + " " + conventions)
        bad += [f'"{s}" is not in its quote' for s in locator_names(code) if not _allowed(s, allowed, patterns)]
        if not bad and (err := _parses(HEADER + code)):
            bad.append(f"does not parse: {err}")
        if name:
            bad += [f"calls {f}, which is not defined" for f in undefined_calls(code, set(names) | {name})]
        if bad or not name:
            log(f"dropped helper {name}: {'; '.join(dict.fromkeys(bad))[:90]}")
            continue
        helpers.append(f"// quote: {quote}\n{code}")
        names.append(name)
        quotes[name] = quote
    return helpers, names, quotes


def generate(tree: dict, requirement: str, areas: list, chat, log, out: Path, patterns=()) -> Path | None:
    """Helpers, then checks per area (in parallel); writes the runnable file to `out`."""
    conventions = str(tree.get("description") or "")
    req = _norm(requirement)
    usable = lambda h: len(h[0]) >= 6 and any("goto(" in x for x in h[0])  # noqa: E731
    best = ([], [], {})
    for _ in range(2):  # one run kept 6 helpers, none reaching a starting point, and every check failed
        found = _helpers(chat, log, requirement, conventions, patterns)
        if (usable(found), len(found[0])) > (usable(best), len(best[0])):
            best = found
        if usable(best):
            break
        log("no helper reaches a starting point from the home page, or fewer than 6 were kept; asking again")
    helpers, names, quotes = best
    log(f"helpers kept: {', '.join(names) or 'none'}")
    if not helpers:
        return None
    lib = "\n\n".join(helpers)
    signatures = "\n".join(f"{re.search(r'^.*function.*$|^const .*$', h.split(chr(10), 1)[1], re.M).group(0)[:150]}"
                           f"  // {h.split(chr(10), 1)[0][10:150]}" for h in helpers)
    by_id = nodes_by_id(tree)

    def one(area: tuple) -> list[str]:
        # a second request when none of an area's checks survives (in one run of three, a whole area kept none)
        for attempt in range(2):
            kept = one_try(area, attempt)
            if kept:
                break
        return kept

    def one_try(area: tuple, attempt: int) -> list[str]:
        label, text = area[0], area[1]
        reply = _ask(chat, CHECKS_PROMPT.format(area=label, helpers=signatures, area_text=text[:60000], example=EXAMPLE),
                     f"audit checks {label[:30]}" + (" (again)" if attempt else ""))
        kept = []
        for quote, code in _units(_code_block(reply)):
            m = re.match(r"""\s*['"`]((REQ[\w-]*?)(?=\s)[^'"`]*)['"`]\s*:""", code)
            if not m:
                continue
            title, fid = m.group(1), m.group(2)
            node = by_id.get(fid)
            bad = []
            if node is None:
                bad.append(f"unknown feature {fid}")
                node = ("", "")
            body = code[m.end():]
            called = " ".join(q for n, q in quotes.items() if re.search(rf"\b{n}\(", body))
            own = _norm(" ".join((node[0], node[1], conventions, called)))
            if not quoted(quote, _norm(node[0] + " " + node[1])):
                bad.append("its quote is not a sentence of its feature")
            # the names it finds elements by come from its own feature (and above) or the helpers it calls;
            # the data it types and the results it expects are its own business
            bad += [f'"{s}" is not in its feature' for s in locator_names(body) if not _allowed(s, own, patterns)]
            bad += [f"calls {f}, which is not defined" for f in undefined_calls(body, set(names))]
            if "gridcell" in req and _CELL_ROLE.search(body):
                bad.append("looks up role cell, but the requirement's grid cells are gridcells")
            entry = code.rstrip().rstrip(",") + ","
            if not bad and (err := _parses(HEADER + lib + "\nconst CHECKS = {\n" + entry + "\n};\n")):
                bad.append(f"does not parse: {err}")
            if bad:
                log(f"dropped check {title[:40]}: {'; '.join(dict.fromkeys(bad))[:90]}")
            else:
                kept.append(f"  // quote: {quote}\n  {entry}")
        log(f"checks {label[:40]}: {len(kept)} kept")
        return kept

    with ThreadPoolExecutor(max_workers=min(6, len(areas) or 1)) as pool:
        checks = [c for chunk in pool.map(one, areas) for c in chunk]
    if not checks:
        return None
    out.write_text(HEADER + "\n" + lib + "\n\nconst CHECKS = {\n" + "\n".join(checks) + "\n};\n" + RUNNER, encoding="utf-8")
    log(f"requirement checks: {len(names)} helpers, {len(checks)} checks -> {out}")
    return out
