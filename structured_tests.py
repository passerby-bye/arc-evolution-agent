"""Acceptance tests the model fills in as a structured table (a JSON schema enforced through function calling),
checked by code against the requirement and compiled to Playwright here. Free-form test code let the model invent
seed contents, counts and loose locators (v5: 12/18 tests failed on a 58-point app, 11/18 on a 44-point one); a
table with a fixed action vocabulary, a verbatim requirement quote per test and a data-flow check leaves it no room
for that. Generic: nothing in here knows a particular task."""
import json
import re

ROLES = ["button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio", "tab", "menuitem", "option",
         "dialog", "region", "heading", "grid", "gridcell", "rowheader", "columnheader", "row", "listitem", "list",
         "alert", "status", "navigation", "table", "cell", "img", "menu", "tablist", "toolbar", "form", "banner", "main"]
ACTIONS = ["click", "dblclick", "right_click", "drag_to", "fill", "select", "check", "uncheck", "press", "reload",
           "expect_text", "expect_value", "expect_visible", "expect_hidden", "expect_attribute", "expect_count",
           "expect_checked"]
DATA_ROLES = {"gridcell", "cell", "row", "listitem", "option"}  # their text is data the test must have entered
INPUT_ROLES = {"textbox", "searchbox", "combobox"}
# a click on a named control: the requirement fixes the name, the model guessed the role ("Data validation" is a
# menu item in the Data menu, written as a button), so clicks accept any of these roles for the name
CLICK_ROLES = ["button", "menuitem", "link", "tab", "option", "checkbox", "radio", "menuitemcheckbox", "treeitem"]

TARGET = {"type": "object", "description": "Exactly one way to find the element.", "properties": {
    "role": {"type": "string", "enum": ROLES}, "name": {"type": "string", "description": "exact accessible name"},
    "label": {"type": "string", "description": "exact label of an input"},
    "text": {"type": "string", "description": "exact visible text (only for a message the requirement quotes)"}}}
SCHEMA = {"type": "object", "required": ["tests"], "properties": {"tests": {"type": "array", "items": {
    "type": "object", "required": ["requirement_id", "quote", "title", "steps"], "properties": {
        "requirement_id": {"type": "string"},
        "quote": {"type": "string", "description": "one sentence copied verbatim from the requirement that this test checks"},
        "title": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "object", "required": ["action"], "properties": {
            "action": {"type": "string", "enum": ACTIONS},
            "target": TARGET,
            "within": {"type": "object", "description": "optional container: role and name of a dialog, region, grid, list",
                       "properties": {"role": {"type": "string", "enum": ROLES}, "name": {"type": "string"}}},
            "to": TARGET,
            "value": {"type": "string", "description": "text to fill / option to select / key to press / expected text, "
                      "value or count / attribute value; {uid} becomes a unique suffix"},
            "attribute": {"type": "string"}}}}}}}}}

TOOL = {"type": "function", "function": {"name": "submit_tests", "description": "Submit the acceptance tests.",
                                         "parameters": SCHEMA}}

PROMPT = """\
Write acceptance tests for one area of a web application, {area}, as a table (call submit_tests). Automated
Playwright tests will grade the app; these tests let it be checked and repaired first.

AREA REQUIREMENT (with the conventions every area shares)
{area_text}

Rules (code checks every test and drops those that break one):
- 4 to 8 tests, each about one requirement of this area that many scenarios depend on. "quote" is one sentence
  copied verbatim from the requirement; every name, label, message and count in the test must come from the
  requirement, and the assertions check what that sentence states.
- Each test starts on the home page; reach everything through clicks on named controls, as a user does.
- Every target, "within" and "to" names its element: a role with its exact accessible name, a label, or a text.
  A role alone matches every element of that role and is rejected; grid cells are named by their coordinate.
- Build your own data: create records with names ending in {{uid}} and type the values you later assert. Never
  assert the content of a grid cell, row or list item that this test did not enter itself (results of formulas the
  test entered are fine); do not rely on seed data except records every scenario names (e.g. to open them).
- fill only text boxes and fields (role textbox, searchbox, combobox, or a label); to change a grid value use the
  editor control the requirement names. Use expect_count only with a number stated in the quote.
- After a rejected submission, fill every field again before the next attempt.
- Assert messages verbatim with expect_text on the element that shows them or with target text.
"""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


_COORD = re.compile(r"^(?:[A-Z]{1,3}\d{1,5}|[A-Z]{1,3}|\d{1,5})$")  # names a grid derives from its data


CONTAINER_ROLES = {"dialog", "region", "list", "listitem", "menu", "grid", "table", "tablist", "toolbar", "form", "main"}


def normalise(test: dict) -> dict:
    steps = []
    for st in test.get("steps") or []:
        # some models write the element under "to" (the drag destination) for every action
        if not st.get("target") and st.get("to") and st.get("action") != "drag_to":
            st["target"] = st.pop("to")
        if st.get("action") == "drag_to" and not st.get("target") and st.get("from"):
            st["target"] = st.pop("from")
        for key in ("target", "within", "to"):
            loc = st.get(key)
            # a grid cell is named by its coordinate; "Cell H4" is the requirement's prose
            if isinstance(loc, dict) and loc.get("role") in DATA_ROLES and re.match(r"(?i)^cell\s+[A-Z]{1,3}\d{1,5}$", str(loc.get("name") or "")):
                loc["name"] = loc["name"].split()[-1]
        t = st.get("target") or {}
        # "the dialog is visible" on an unnamed container asserts nothing: the step goes, not the test
        if st.get("action") == "expect_visible" and t.get("role") in CONTAINER_ROLES and not (t.get("name") or t.get("text") or t.get("label")):
            continue
        steps.append(st)
    test["steps"] = steps
    for st in steps:
        w = st.get("within")
        if isinstance(w, dict) and not (w.get("name") or w.get("text") or w.get("label")):
            st.pop("within")
        t = st.get("target") or {}
        # a click on a cell coordinate ("H4") written with a button/link role means the grid cell
        if st.get("action") in ("click", "dblclick", "right_click") and t.get("role") in ("button", "link", "cell")                 and re.match(r"^[A-Z]{1,3}\d{1,5}$", str(t.get("name") or "")):
            t["role"] = "gridcell"
        # "text", "paragraph", "generic" are not ARIA roles: the model means the text itself (github run 9: every
        # message assertion looked for role 'text' and never matched a page that showed the message)
        if t.get("role") in ("text", "statictext", "paragraph", "generic", "span", "div", "label") and (t.get("name") or t.get("text")):
            st["target"] = t = {"text": t.get("name") or t.get("text")}
        # a message: the requirement states its text is visible, not which live-region role shows it
        if t.get("role") in ("alert", "status") and st.get("action") in ("expect_text", "expect_visible") \
                and (st.get("value") or t.get("name") or t.get("text")):
            st["target"] = {"text": st.get("value") or t.get("name") or t.get("text")}
            st["action"] = "expect_visible"
        # expect_text with the message as the locator and no value compiled to toHaveText("") and failed on every
        # app (local stage 1: most scenario tests); the message being visible is what the scenario asserts
        elif st.get("action") == "expect_text" and not st.get("value") and t.get("role") in DATA_ROLES                 and re.match(r"(?i)^[A-Z]{1,3}\d{1,5}$", str(t.get("name") or "")):
            st["value"] = ""  # a cell named by its coordinate with no value: it displays empty text (a cleared cell)
            st["expect_empty"] = True
        elif st.get("action") == "expect_text" and not st.get("value") and (t.get("text") or t.get("name") or t.get("label")):
            st["action"] = "expect_visible"  # a cell named by its content ("Atlas/Queued/17"), a message: it is shown
        # an input has no text: "the Username field shows 'Username format is invalid'" means the message is
        # visible next to it (github run 7: the page showed it and the test still failed); a single token is the
        # field's value
        elif st.get("action") == "expect_text" and st.get("value") and (t.get("role") in INPUT_ROLES or (t.get("label") and not t.get("role"))):
            if re.search(r"\s", st["value"].strip()):
                st["target"] = {"text": st["value"]}
                st["action"] = "expect_visible"
            else:
                st["action"] = "expect_value"
    return test


def validate(test: dict, requirement: str, name_patterns: list, procedure: bool = False, given: tuple = ()) -> list[str]:
    """Reasons to drop the test (empty when it may be kept). A procedure has no quote and no assertion of its own;
    `given` holds the values its scenario's GIVEN states as pre-provisioned, which count as entered data."""
    req = _norm(requirement)
    quoted = {_norm(q) for q in re.findall(r'[“"`]([^”"`\n]{1,80})[”"`]', requirement)}
    series = [re.compile(re.escape(m) + r"\d+$") for m in re.findall(r'\b([A-Z][a-z]+)N\b', requirement)]
    caps = set(re.findall(r'\b[A-Z]{3,}\b', requirement))
    bad = []
    if not procedure and (_norm(test.get("quote")) not in req or len(_norm(test.get("quote"))) < 20):
        bad.append("its quote is not a sentence of the requirement")
    typed: list[str] = [str(g) for g in given]

    def known(s: str) -> bool:
        s = (s or "").replace("{uid}", "")
        return (not s.strip() or _norm(s) in quoted or _COORD.match(s.strip()) is not None
                or s.strip() in caps or any(p.match(s.strip()) for p in series)
                or any(t and (s in t or t in s) for t in typed) or any(p.match(s) for p in name_patterns))

    steps = test.get("steps") or []
    if not procedure and not any(str(st.get("action", "")).startswith("expect") for st in steps):
        bad.append("no assertion")
    for st in steps:
        a, t = st.get("action"), st.get("target") or {}
        if a not in ACTIONS:
            bad.append(f"unknown action {a}"); continue
        for which, loc in (("target", t), ("within", st.get("within") or {}), ("to", st.get("to") or {})):
            # a role without a name matches every element of that role (2 text boxes, 2600 grid cells)
            if loc and not any(loc.get(k) for k in ("name", "label", "text")):
                bad.append(f'{which} {loc.get("role", "")} has no name, label or text')
            for k in ("name", "label", "text"):
                if loc.get(k) and not known(loc[k]):
                    bad.append(f'{k} "{loc[k]}" is not in the requirement')
        if a in ("reload",):
            continue
        if not t and a != "press":
            bad.append(f"{a} without a target"); continue
        v = st.get("value") or ""
        # a cell found by its coordinate is always there: without an expected text the step checks nothing
        # (an explicit empty value is an expectation: a cleared cell displays empty text)
        if a in ("expect_text", "expect_visible") and t.get("role") in DATA_ROLES and not v \
                and (a == "expect_visible" or st.get("value") is None) \
                and _COORD.match(str(t.get("name") or t.get("text") or "").strip()):
            bad.append(f'{a} on {t["role"]} {t.get("name") or t.get("text")} without the expected text')
        if a == "expect_value" and t.get("role") and t["role"] not in INPUT_ROLES:
            bad.append(f'expect_value on a {t["role"]} (not an input)')
        if a == "fill":
            if t.get("role") and t["role"] not in INPUT_ROLES:
                bad.append(f'fill on a {t["role"]} (not an input)')
            typed.append(v.replace("{uid}", ""))
        in_data = t.get("role") in DATA_ROLES or (st.get("within") or {}).get("role") in ("grid", "table", "list")
        if a in ("expect_text", "expect_value") and in_data:
            if v and not any(v.replace("{uid}", "") == x or v.replace("{uid}", "") in x for x in typed) \
                    and not (re.fullmatch(r"-?[\d.,]+|#[A-Z/0!?]+!?", v) and any(re.search(r"[=\d]", x) for x in typed)):
                bad.append(f'asserts data "{v}" the test never entered')
        elif a in ("expect_text", "expect_value") and v and not (known(v) or _norm(v) in req):
            bad.append(f'expects "{v}", which is not in the requirement')
        if a == "expect_count" and (not v.strip().isdigit() or v.strip() not in (test.get("quote") or "")):
            bad.append(f'count "{v}" is not stated in its quote')
    return list(dict.fromkeys(bad))


def parse_tests(raw: str) -> list | None:
    """The tests of a submit_tests argument string; a truncated table (the stream ended early) yields the complete
    objects it holds. None when nothing parses."""
    try:
        return [t for t in json.loads(raw[raw.find("{"):raw.rfind("}") + 1]).get("tests") or [] if isinstance(t, dict)]
    except (ValueError, AttributeError):
        pass
    start = raw.find('"tests"')
    start = raw.find("[", start) if start >= 0 else -1
    if start < 0:
        return None
    dec, pos, out = json.JSONDecoder(), start + 1, []
    while True:
        while pos < len(raw) and raw[pos] in " \n\r\t,":
            pos += 1
        if pos >= len(raw) or raw[pos] != "{":
            break
        try:
            obj, end = dec.raw_decode(raw, pos)
        except ValueError:
            break
        if isinstance(obj, dict):
            out.append(obj)
        pos = end
    return out or None


def _js(s: str) -> str:
    return json.dumps(s, ensure_ascii=False).replace("{uid}", '" + U + "') if "{uid}" in (s or "") else json.dumps(s or "", ensure_ascii=False)


def _loc(t: dict, scope: str = "page", click: bool = False) -> str:
    if t.get("role"):
        name = t.get("name") or t.get("text") or t.get("label")
        if not name:
            return f"{scope}.getByRole({json.dumps(t['role'])})"
        if click and t["role"] in CLICK_ROLES:
            # the named role first; another interactive role only when nothing has that role and name (an .or()
            # chain with .first() took the header link "Sign in" over the form's button "Sign in": github run 7)
            roles = [t["role"]] + [r for r in CLICK_ROLES if r != t["role"]]
            cands = ", ".join(f"{scope}.getByRole({json.dumps(r)}, {{ name: {_js(name)}, exact: true }})" for r in roles)
            # count() does not wait: a list that loads asynchronously counts 0 for every role, so the chain first waits
            # for any candidate to appear, then picks by priority
            return (f"(await (async () => {{ const c = [{cands}]; await c.reduce((a, b) => a.or(b)).first().waitFor({{ timeout: 8000 }}).catch(() => {{}}); "
                    f"for (const l of c) if (await l.count()) return l; return c[0]; }})())")
        if t["role"] in INPUT_ROLES:  # textbox / searchbox / combobox are interchangeable to the model (github run 9:
            roles = [t["role"]] + [r for r in INPUT_ROLES if r != t["role"]]  # a textbox "Search" asked for as searchbox)
            return ".or(".join(f"{scope}.getByRole({json.dumps(r)}, {{ name: {_js(name)}, exact: true }})" for r in roles) + ")" * (len(roles) - 1) + ".first()"
        return f"{scope}.getByRole({json.dumps(t['role'])}, {{ name: {_js(name)}, exact: true }})"
    if t.get("label"):
        return f"{scope}.getByLabel({_js(t['label'])}, {{ exact: true }})"
    return f"{scope}.getByText({_js(t.get('text', ''))}, {{ exact: true }})"


_CELL_COORD = re.compile(r"^[A-Z]{1,3}\d{1,5}$", re.I)


def _fill_code(t: dict, scope: str, L: str, v: str) -> str:
    """fill: a text box or select; a grid cell (data role, or a name like K8 whatever role the model wrote) is
    edited the way a user does it: double-click, type, Enter (sheet run 10: "fill K8" looked for a textbox K8)."""
    name = str(t.get("name") or t.get("label") or "")
    if t.get("role") in ("gridcell", "cell") or (name and _CELL_COORD.match(name) and not t.get("text")):
        cell = f"{scope}.getByRole('gridcell', {{ name: {_js(name)}, exact: true }}).or({scope}.getByRole('cell', {{ name: {_js(name)}, exact: true }}))"
        return (f"await {cell}.first().dblclick(); await page.keyboard.press('Control+A'); "
                f"await page.keyboard.type({_js(v)}); await page.keyboard.press('Enter');")
    return f"if ((await {L}.evaluate((e) => e.tagName)) === 'SELECT') await {L}.selectOption({{ label: {_js(v)} }}); else await {L}.fill({_js(v)});"


def compile_test(test: dict) -> str:
    title = f"{test.get('requirement_id', '')} {test.get('title', '')}".strip().replace("`", "'")
    # a failing step reports what the page showed instead and the errors it logged: "element not found" alone
    # sends the fix rounds guessing
    lines = [f"test({json.dumps(title, ensure_ascii=False)}, async ({{ page }}) => {{",
             "  const U = '-' + Date.now().toString(36);",
             "  const errs = []; page.on('pageerror', (e) => errs.push('uncaught: ' + String(e.message).slice(0, 120)));",
             "  page.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 120)); });",
             "  page.on('response', (r) => { if (r.status() >= 400) errs.push('HTTP ' + r.status() + ' ' + new URL(r.url()).pathname); });",
             "  let step = 'open /'; try {", "  await page.goto('/');"]
    for st in test.get("steps") or []:
        a, t, v = st["action"], st.get("target") or {}, st.get("value") or ""
        w = st.get("within") or {}
        scope = f"page.getByRole({json.dumps(w['role'])}, {{ name: {_js(w.get('name', ''))}, exact: true }})" if w.get("role") and w.get("name") else "page"
        L = _loc(t, scope, click=a in ("click", "dblclick", "right_click")) if t else ""
        code = {
            "click": f"await {L}.first().click();", "dblclick": f"await {L}.first().dblclick();",
            "right_click": f"await {L}.first().click({{ button: 'right' }});",
            "drag_to": f"await {L}.first().dragTo({_loc(st.get('to') or {}, scope)}.first());",
            "fill": _fill_code(t, scope, L, v), "select": f"await {L}.selectOption({{ label: {_js(v)} }});",
            "check": f"await {L}.check();", "uncheck": f"await {L}.uncheck();",
            "press": f"await {(L + '.press') if L else 'page.keyboard.press'}({json.dumps(v or 'Enter')});",
            "reload": "await page.reload();",
            "expect_text": f"await expect({L}.first()).toHaveText({_js(v)});",
            # the field may have been in a dialog that closed on submit: the value shown as text then counts
            "expect_value": f"if (await {L}.count()) await expect({L}).toHaveValue({_js(v)}); else await expect(page.getByText({_js(v)}, {{ exact: true }}).first()).toBeVisible();",
            "expect_visible": f"await expect({L}.first()).toBeVisible();", "expect_hidden": f"await expect({L}).toBeHidden();",
            "expect_attribute": f"await expect({L}.first()).toHaveAttribute({json.dumps(st.get('attribute') or '')}, {_js(v)});",
            "expect_count": f"await expect({L}).toHaveCount({int(v) if v.strip().isdigit() else 0});",
            "expect_checked": f"await expect({L}).toBeChecked();",
        }[a]
        what = " ".join(x for x in (a, t.get("name") or t.get("text") or t.get("label") or "", f"= {v}" if v else "") if x)
        lines.append(f"  step = {json.dumps(what[:120], ensure_ascii=False)};")
        lines.append("  " + code)
    lines.append("  } catch (e) {")
    lines.append("    const shown = await page.locator('body').innerText().catch(() => '').then((t) => t.replace(/\\s+/g, ' ').slice(0, 300));")
    lines.append("    e.message += `\\nFAILED STEP: ${step}\\nPAGE ${new URL(page.url()).pathname} SHOWED: ${shown}\\nPAGE ERRORS: ${[...new Set(errs)].slice(0, 4).join(' | ') || 'none'}`;")
    lines.append("    throw e;")
    lines.append("  }")
    lines.append("});")
    return "\n".join(lines)


def spec_from_tables(tables: list[dict], requirement: str, name_patterns: list, log) -> str | None:
    kept = []
    for test in tables:
        reasons = validate(normalise(test), requirement, name_patterns, given=tuple(test.pop("_given", ()) or ()))
        if reasons:
            log(f"dropped {test.get('requirement_id', '')} {str(test.get('title', ''))[:40]}: {'; '.join(reasons)[:90]}")
        else:
            kept.append(compile_test(test))
    if not kept:
        return None
    return "import { test, expect } from '@playwright/test';\n\n" + "\n\n".join(kept) + "\n"


def generate(areas: list, requirement: str, chat, log, name_patterns: list) -> str | None:
    """One table per top-level area (in parallel), validated and compiled into one spec file."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    def one(area: tuple) -> list:
        label, text = area[0], area[1]
        body = {"messages": [{"role": "user", "content": PROMPT.format(area=label, area_text=text[:60000])}],
                "max_tokens": 16384, "thinking": {"type": "disabled"}, "tools": [TOOL],
                "tool_choice": {"type": "function", "function": {"name": "submit_tests"}}}
        r = chat(body, 600, f"structured tests {label[:30]}")
        if r is None:  # an endpoint that rejects a forced tool choice
            body.pop("tool_choice"); r = chat(body, 600, f"structured tests {label[:30]} (retry)")
        msg = ((r or {}).get("choices") or [{}])[0].get("message") or {}
        raw = next((c["function"].get("arguments") for c in msg.get("tool_calls") or [] if c.get("function")), None)             or (msg.get("content") or "")
        try:
            tests = json.loads(raw[raw.find("{"):raw.rfind("}") + 1]).get("tests") or []
        except (ValueError, AttributeError):
            log(f"structured tests {label[:30]}: no table in the reply"); return []
        log(f"structured tests {label[:40]}: {len(tests)} written")
        return [t for t in tests if isinstance(t, dict)]

    with ThreadPoolExecutor(max_workers=min(6, len(areas) or 1)) as pool:
        tables = [t for chunk in pool.map(one, areas) for t in chunk]
    return spec_from_tables(tables, requirement, name_patterns, log)


SCENARIO_PROMPT = """\
Translate each scenario below into one acceptance test (call submit_tests). Automated Playwright tests derived
from these scenarios will grade the app; these tests let it be checked and repaired first.

SCENARIOS of {area}
{scenarios}

Conventions every scenario shares
{conventions}

Rules (code checks every test and drops those that break one):
- One test per scenario, in the same order; requirement_id is the scenario's requirement id. "quote" is one
  sentence copied verbatim from that scenario's WHEN or THEN.
- Follow the scenario literally: the GIVEN says which records and accounts already exist (use them by the exact
  names and passwords it gives), the WHEN lists the clicks and inputs in order, the THEN lists what to assert.
  Add only the steps the scenario implies to get there (for example signing in through the "Sign in" link with
  the account it names before a signed-in action). Do not add assertions the scenario does not state.
- Every target names its element: a role with its exact accessible name, a label, or a text. A role alone is
  rejected. A name like `pw-branch-<unique suffix>` becomes pw-branch-{{uid}}.
- "opens `X`" means clicking the control named exactly X (the record's name) where it is listed; there is no
  "Open" or "Workbook" control unless the requirement quotes one.
- fill only text boxes and fields; use expect_count only with a number the scenario states.
"""


def generate_from_scenarios(tree: dict, requirement: str, chat, log, name_patterns: list) -> str | None:
    """One test per scenario, translated literally by the model, validated and compiled (for requirements whose
    scenarios are concrete steps)."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
    conventions = " ".join(str(tree.get("description") or "").split())[:4000]

    def scenarios_of(node: dict, out: list) -> list:
        for i, sc in enumerate(node.get("scenarios") or []):
            steps = "\n".join(f"    {st.get('keyword', '')}: {' '.join(str(st.get('content', '')).split())}"
                              for st in sc.get("steps") or [] if isinstance(st, dict))
            out.append(f"- {node.get('id')} {node.get('name', '')} (scenario {i + 1})\n{steps}")
        for c in node.get("children") or []:
            if isinstance(c, dict):
                scenarios_of(c, out)
        return out

    areas = [c for c in tree.get("children") or [] if isinstance(c, dict)] or [tree]

    def one(area: dict, extra: str = "") -> list:
        label = f"{area.get('id')} {area.get('name', '')}"
        items = scenarios_of(area, [])
        tests = []
        for k in range(0, len(items), 12):  # at most 12 scenarios per request keeps each reply complete
            body = {"messages": [{"role": "user", "content": SCENARIO_PROMPT.format(
                        area=label, scenarios="\n".join(items[k:k + 12]), conventions=conventions) + extra}],
                    "max_tokens": 16384, "thinking": {"type": "disabled"}, "tools": [TOOL],
                    "tool_choice": {"type": "function", "function": {"name": "submit_tests"}}}
            batch = None
            for attempt in range(2):  # a reply without a parseable table is asked once more
                r = chat(body, 600, f"scenario tests {label[:30]}" + (" (retry)" if attempt else ""))
                if r is None:
                    body.pop("tool_choice", None)
                    continue
                msg = ((r or {}).get("choices") or [{}])[0].get("message") or {}
                raw = next((c["function"].get("arguments") for c in msg.get("tool_calls") or [] if c.get("function")), None) \
                    or (msg.get("content") or "")
                batch = parse_tests(raw)
                if batch is not None:
                    break
                log(f"scenario tests {label[:30]}: no table in the reply ({len(raw)} chars, starts {raw[:160]!r})")
            if batch is None:
                continue
            # values the GIVEN clauses state as pre-provisioned may be asserted (cell F3 holds `unreviewed`)
            given = re.findall(r"`([^`\n]+)`", " ".join(l for it in items[k:k + 12] for l in it.splitlines() if "GIVEN" in l))
            for t in batch:
                t["_given"] = given
            tests += batch
        log(f"scenario tests {label[:40]}: {len(tests)} written for {len(items)} scenarios")
        return tests

    reasons: list[str] = []

    def capture(msg: str) -> None:
        reasons.append(msg)
        log(msg)

    with ThreadPoolExecutor(max_workers=min(6, len(areas))) as pool:
        tables = [t for chunk in pool.map(one, areas) for t in chunk]
        spec = spec_from_tables(tables, requirement, name_patterns, capture)
        if spec is None and tables:
            # every test was dropped (sheet run 10: sign-in steps invented for an app without accounts): once more,
            # with the reasons, so the feature is not accepted unverified
            hint = ("\n\nAn earlier attempt was rejected for these reasons; write the tests again using only names, labels "
                    "and messages that the scenarios or conventions state, with no step the scenarios do not describe:\n"
                    + "\n".join(r[:300] for r in reasons[:6]))
            tables = [t for chunk in pool.map(lambda a: one(a, hint), areas) for t in chunk]
            spec = spec_from_tables(tables, requirement, name_patterns, log)
    return spec


# ---------------------------------------------------------------- shared procedures (v5e)
# Tests written per area failed mostly in their setup: 9 of 10 never opened the record the feature works on, and each
# test wrote its own copy of the same steps, so one wrong copy cost one test. The model now writes the shared steps
# once as procedures (same table vocabulary, {param} placeholders), every test starts with a call to one, and calls
# are expanded before the usual checks, so a procedure obeys every rule a test does.

STEP = SCHEMA["properties"]["tests"]["items"]["properties"]["steps"]["items"]
PROC_SCHEMA = {"type": "object", "required": ["procedures"], "properties": {"procedures": {"type": "array", "items": {
    "type": "object", "required": ["name", "purpose", "entry", "params", "steps"], "properties": {
        "name": {"type": "string", "description": "snake_case"},
        "purpose": {"type": "string"},
        "entry": {"type": "boolean", "description": "true if it starts on the home page and reaches a starting point "
                  "for tests (a test may begin with it)"},
        "params": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": STEP}}}}}}
PROC_TOOL = {"type": "function", "function": {"name": "submit_procedures", "description": "Submit the shared procedures.",
                                              "parameters": PROC_SCHEMA}}
CALL_STEP = json.loads(json.dumps(STEP))
CALL_STEP["properties"]["action"]["enum"] = ACTIONS + ["call"]
CALL_STEP["properties"]["procedure"] = {"type": "string", "description": "for action call: the procedure name"}
CALL_STEP["properties"]["args"] = {"type": "array", "items": {"type": "string"},
                                   "description": "for action call: the values of the procedure's parameters, in order"}
CALL_SCHEMA = json.loads(json.dumps(SCHEMA))
CALL_SCHEMA["properties"]["tests"]["items"]["properties"]["steps"]["items"] = CALL_STEP
CALL_TOOL = {"type": "function", "function": {"name": "submit_tests", "description": "Submit the acceptance tests.",
                                              "parameters": CALL_SCHEMA}}

PROC_PROMPT = """\
Automated Playwright tests will grade a web application built from the requirement below; each test starts
on the home page and reaches its feature through the visible controls. Write the steps those tests share as
3 to 6 procedures (call submit_procedures): first the ways to reach a starting point from the home page (for
example creating a fresh record of the kind most features work on, or signing in with an account the
requirement gives) with entry true, then repeated actions such as entering a value through the editor the
requirement names. Steps use the table vocabulary; a step value or name may be a parameter written {{param}}
(list it in params), and {{uid}} becomes a unique suffix.

Rules (code checks every procedure):
- Every target names its element: a role with its exact accessible name, a label, or a text, all as the
  requirement quotes them (or a parameter). A role alone is rejected.
- Create what you need with names ending in {{uid}}; do not rely on seed records unless every scenario names them.
- fill only text boxes and fields; never type into a grid or table cell directly.
- An entry procedure takes no parameters (it opens a record the requirement names, or creates a fresh one)
  and ends with expect_visible on an element that shows the starting point was reached.

REQUIREMENT
{requirement}
"""

PROC_TESTS_PROMPT = """\
Write acceptance tests for one area of a web application, {area}, as a table (call submit_tests). Automated
Playwright tests will grade the app; these tests let it be checked and repaired first.

SHARED PROCEDURES (a step {{"action": "call", "procedure": "<name>", "args": [<one value per parameter, in
order>]}} runs the procedure's steps with those values)
{procedures}

AREA REQUIREMENT (with the conventions every area shares)
{area_text}

Rules (code checks every test and drops those that break one):
- One test for each feature of this area (at most 12), each checking what one sentence of that feature states.
  "quote" is that sentence copied verbatim; every name, label, message and count must come from the requirement.
- The first step of every test is a call to an entry procedure; use calls for the shared steps.
- Every target, "within" and "to" names its element: a role with its exact accessible name, a label, or a text.
  A role alone is rejected; grid cells are named by their coordinate.
- Build your own data: type the values you later assert. Never assert the content of a grid cell, row or list item
  this test did not enter itself (results of formulas it entered are fine).
- fill only text boxes and fields; use expect_count only with a number stated in the quote.
- Assert messages verbatim with expect_text on the element that shows them or with target text; every
  expect_text and expect_value gives the exact expected text in "value".
"""


def _subst(x, args: dict):
    if isinstance(x, str):
        return re.sub(r"\{(\w+)\}", lambda m: args.get(m.group(1), m.group(0)), x)
    if isinstance(x, dict):
        return {k: _subst(v, args) for k, v in x.items()}
    if isinstance(x, list):
        return [_subst(v, args) for v in x]
    return x


def expand(test: dict, procs: dict) -> tuple[dict, list[str]]:
    """The test with every call replaced by the procedure's steps (arguments substituted); reasons it cannot be."""
    steps, bad = [], []
    for i, st in enumerate(test.get("steps") or []):
        if st.get("action") != "call":
            steps.append(st)
            continue
        p = procs.get(st.get("procedure"))
        if p is None:
            bad.append(f'calls unknown procedure {st.get("procedure")}')
            continue
        if i == 0 and not p.get("entry"):
            bad.append(f'starts with {p["name"]}, which is not an entry procedure')
        raw = st.get("args")
        params = p.get("params") or []
        if not raw and params:  # the model often writes a call's values into the step's target and value instead
            t = st.get("target") or {}
            named = t.get("name") or t.get("text") or t.get("label")
            raw = [x for x in ([named, st.get("value")] if len(params) == 2 else [st.get("value") or named]) if x]
        # positional (the schema); a {param: value} object is accepted too
        args = ({k: str(v) for k, v in raw.items()} if isinstance(raw, dict)
                else dict(zip(params, [str(v) for v in raw or []])))
        args.pop("uid", None)
        missing = [a for a in p.get("params") or [] if a not in args]
        if missing:
            bad.append(f'call of {p["name"]} lacks {", ".join(missing)}')
        steps += _subst(p.get("steps") or [], args)
    if ((test.get("steps") or [{}])[0]).get("action") != "call":
        bad.append("does not start with an entry procedure")
    return dict(test, steps=steps), bad


def _call(chat, body: dict, label: str, log) -> dict:
    r = chat(body, 600, label)
    if r is None:  # an endpoint that rejects a forced tool choice
        body.pop("tool_choice", None)
        r = chat(body, 600, label + " (retry)")
    msg = ((r or {}).get("choices") or [{}])[0].get("message") or {}
    raw = next((c["function"].get("arguments") for c in msg.get("tool_calls") or [] if c.get("function")), None) \
        or (msg.get("content") or "")
    try:
        return json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    except (ValueError, AttributeError):
        log(f"{label}: no table in the reply")
        return {}


def procedures(requirement: str, chat, log, name_patterns: list) -> dict:
    """{name: procedure} for the procedures that pass every check (parameters count as named)."""
    body = {"messages": [{"role": "user", "content": PROC_PROMPT.format(requirement=requirement[:60000])}],
            "max_tokens": 8192, "thinking": {"type": "disabled"}, "tools": [PROC_TOOL],
            "tool_choice": {"type": "function", "function": {"name": "submit_procedures"}}}
    kept = {}
    for p in _call(chat, body, "procedures", log).get("procedures") or []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        # the parameters are the placeholders the steps use (the model often leaves params out)
        p["params"] = sorted(set(re.findall(r"\{(\w+)\}", json.dumps(p.get("steps") or []))) - {"uid"})
        probe = _subst({"steps": p.get("steps") or []}, {a: "{uid}" for a in p.get("params") or []})
        reasons = validate(normalise(probe), requirement, name_patterns, procedure=True)
        if p.get("entry") and p["params"]:  # tests then pass invented record names ("Sample Workbook")
            reasons.append(f'an entry procedure with parameters ({", ".join(p["params"])})')
        if reasons:
            log(f"dropped procedure {p['name']}: {'; '.join(reasons)[:90]}")
        else:
            kept[p["name"]] = p
    log("procedures kept: " + (", ".join(n + (" (entry)" if p.get("entry") else "") for n, p in kept.items()) or "none"))
    return kept


def generate_with_procedures(areas: list, requirement: str, chat, log, name_patterns: list) -> str | None:
    """Shared procedures first, then one table of tests per area that calls them; calls are expanded, then every test
    is validated and compiled as usual."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
    procs = procedures(requirement, chat, log, name_patterns)
    if not any(p.get("entry") for p in procs.values()):
        log("no usable entry procedure")
        return None
    listing = "\n".join(f'- {n}({", ".join(p["params"])}){" [entry: may be the first step]" if p.get("entry") else ""}: '
                        f'{p.get("purpose", "")}' for n, p in procs.items())

    def one(area: tuple) -> list:
        label, text = area[0], area[1]
        body = {"messages": [{"role": "user", "content": PROC_TESTS_PROMPT.format(
                    area=label, procedures=listing, area_text=text[:60000])}],
                "max_tokens": 16384, "thinking": {"type": "disabled"}, "tools": [CALL_TOOL],
                "tool_choice": {"type": "function", "function": {"name": "submit_tests"}}}
        tests = [t for t in _call(chat, body, f"tests {label[:30]}", log).get("tests") or [] if isinstance(t, dict)]
        log(f"tests {label[:40]}: {len(tests)} written")
        return tests

    with ThreadPoolExecutor(max_workers=min(6, len(areas) or 1)) as pool:
        tables = [t for chunk in pool.map(one, areas) for t in chunk]
    kept = []
    for test in tables:
        flat, reasons = expand(test, procs)
        reasons += validate(normalise(flat), requirement, name_patterns)
        if reasons:
            log(f"dropped {test.get('requirement_id', '')} {str(test.get('title', ''))[:40]}: "
                f"{'; '.join(dict.fromkeys(reasons))[:90]}")
        else:
            kept.append(compile_test(flat))
    log(f"procedure tests: {len(kept)} kept of {len(tables)}")
    if not kept:
        return None
    return "import { test, expect } from '@playwright/test';\n\n" + "\n\n".join(kept) + "\n"
