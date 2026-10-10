"""One change to the application in two small requests: first the model names the files and functions to touch
(it sees the source index, never the source), then it edits numbered excerpts of exactly those by line number
(edits.py). Each request is small enough for the reasoning phase. Generic: nothing here knows a task."""
import re
from pathlib import Path

import edits

LOCATE_PROMPT = """\
{goal}

Source index (functions:line, routes, interface strings per file)
{index}

{material}

Name what to change to do this, one line per file, nothing else:
path/from/app/root: function1, function2       (the functions or top-level declarations to edit in that file)
path/from/app/root: all                        (a short file to edit as a whole)
NEW path/from/app/root                         (a file to add)
Name every file the change touches (the page, the API client, the server route table, the data layer...); keep the
list to what is needed (at most 10 lines).
"""

EDIT_PROMPT = """\
{goal}

{material}

Source excerpts (line numbers are the ones to use in the edits)
{excerpts}

{format}
"""

_LOCATE_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:`)?(NEW\s+)?((?:frontend|backend)/[\w./-]+)(?:`)?\s*(?::\s*(.*))?$", re.I | re.M)
MAX_EXCERPT_CHARS = 48000


def parse_locate(reply: str) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """[(path, [function names] or [] for all)], [new file paths]"""
    found, new = [], []
    for m in _LOCATE_LINE.finditer(reply):
        is_new, rel, names = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
        if is_new:
            new.append(rel)
            continue
        if names.lower() in ("", "all", "whole file", "entire file"):
            found.append((rel, []))
        else:
            found.append((rel, [n.strip().strip("`()") for n in re.split(r"[,\s]+and\s+|,", names) if n.strip()]))
    return found, new


def excerpts(out: Path, located: list[tuple[str, list[str]]], extra: list[str] = (), whole: set[str] = frozenset()) -> tuple[str, dict[str, set[int]]]:
    """Numbered excerpts for the located functions (whole file when it is short or no function was named); `extra`
    paths are shown whole when they fit; `whole` paths (files earlier rounds edited) are shown whole when they are
    not long, so the model sees what its own earlier edits left there instead of adding another layer."""
    parts, shown, used = [], {}, 0
    order = list(located) + [(rel, []) for rel in extra if rel not in {r for r, _ in located}]
    for rel, names in order:
        f = out / rel
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        if rel in whole and len(text.splitlines()) <= 400:
            names = []
        spans = edits.function_spans(text, names) if names else None
        if names and not spans:  # the names are not there yet (to be added): their mentions, the head and the tail
            n_lines = len(text.splitlines())
            hits = [i + 1 for i, l in enumerate(text.splitlines()) if any(n in l for n in names)]
            spans = [(h, h) for h in hits[:8]] + [(1, 30), (max(1, n_lines - 30), n_lines)]
        if not names and len(text.splitlines()) > 400:
            spans = [(1, 60)]  # a long file named whole: its head only, the rest by name next time
        if spans:  # the import header is always shown: a wrong import is fixed there, not in the functions
            head = [i + 1 for i, l in enumerate(text.splitlines()[:120]) if re.match(r"\s*(?:import\s|.*require\()", l)]
            if head:
                spans = [(1, max(head))] + spans
        block, lines_shown = edits.numbered(f, rel, spans)
        if used + len(block) > MAX_EXCERPT_CHARS:
            continue  # this one does not fit; a smaller later file still may
        parts.append(block)
        shown[rel] = lines_shown
        used += len(block)
    return "\n\n".join(parts), shown


def ask_change(model, out: Path, goal: str, rendered: str, material: str, log, save, extra_files: list[str] = (),
               wholeapp=None, controls: list[str] = (), whole: set[str] = frozenset()) -> tuple[list[str], list[str]]:
    """Locate, then edit. Returns (changed paths, problems). `save(label, prompt, reply)` records the exchange.
    `controls` are the requirement's control names: the code paths behind them (graph.py) are given to both
    requests, and edits outside those paths are logged."""
    import graph  # noqa: PLC0415
    system = wholeapp.SYSTEM
    g = graph.Graph(out)
    api_paths = list(dict.fromkeys(re.findall(r"(?:HTTP \d{3}|for) (/api/[^\s|,;)]+)", material)))  # paths the problems name
    paths = g.chain(list(controls), api_paths=api_paths) if controls or api_paths else []
    if paths:
        material = material + "\n\nCODE PATHS BEHIND THE NAMED CONTROLS (functions that render or handle them, then what they call)\n" + g.describe(paths)
    before_text = {rel: list(lines) for rel, lines in g.text.items()}
    prompt = LOCATE_PROMPT.format(goal=goal, index=rendered, material=material)
    reply, _ = model.chat([{"role": "system", "content": system}, {"role": "user", "content": prompt}], "locate", True)
    save("locate", prompt, reply)
    located, new_files = parse_locate(reply)
    if not located and not new_files:
        log(f"locate: nothing named; reply starts {reply.strip()[:200]!r}")
        located = [(rel, []) for rel in extra_files]
    if paths:  # the functions on the code paths are shown too, named precisely
        by_file: dict = {}
        for rel, fn in paths:
            by_file.setdefault(rel, []).append(fn)
        named = {rel for rel, _ in located}
        located += [(rel, fns) for rel, fns in by_file.items() if rel not in named]
    log("locate: " + "; ".join(f"{rel}: {', '.join(n) or 'all'}" for rel, n in located)[:160] + (f"; new: {', '.join(new_files)}" if new_files else ""))  # the platform keeps ~26k chars of stdout
    text, shown = excerpts(out, located, extra_files, whole)
    if new_files:
        material = material + "\n\nFiles to add (whole, with FILE blocks): " + ", ".join(new_files)
    prompt = EDIT_PROMPT.format(goal=goal, material=material, excerpts=text, format=edits.FORMAT)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    reply, finish = model.chat(messages, "edit", True)
    if finish == "length" and reply.strip():  # the output budget ran out mid-edits: once, the rest (new files are often last)
        more, _ = model.chat(messages + [{"role": "assistant", "content": reply},
                                         {"role": "user", "content": "Your reply was cut off. Give only the remaining edits and files, "
                                          "in the same format, without repeating any block above."}], "edit (rest)", True)
        reply = reply + "\n" + more
    save("edit", prompt, reply)
    reply = reply.replace("&lt;&lt;&lt;", "<<<").replace("&gt;&gt;&gt;", ">>>")
    changed, problems = edits.apply_line_edits(out, reply, shown)
    files, _ = wholeapp.parse_blocks(reply)
    for rel, body in list(files.items()):
        safe = wholeapp.safe_rel(rel)
        if safe and (out / safe).is_file() and safe not in shown:
            problems.append(f"FILE block for {safe}: an existing file is changed by line edits, not rewritten")
            files.pop(rel)
    for rel in wholeapp.write(out, files):
        changed.append(rel)
    if not changed and not problems:
        log(f"edit: the reply held no edit; it starts {reply.strip()[:200]!r}")
    if changed and paths:
        outside = [f"{rel}:{fn}" for rel, fn in graph.changed_functions(before_text, out, changed) if (rel, fn) not in set(paths)]
        if outside:
            log(f"edit: changes outside the code paths behind the controls: {', '.join(outside[:10])}")
    return list(dict.fromkeys(changed)), problems
