"""The controls an application shows, page by page, so a change can be checked for what it lost: the inherited
application's home page and the pages it links to are opened in a browser before any change, and again after one;
controls that disappeared are regressions the build check cannot see (a rewritten router whose home route now
renders "Not found"). Code only; generic."""
import json
import re
from pathlib import Path

_ID_SEGMENT = re.compile(r"/[^/]*(?:[0-9a-f]{8,}|\d{4,})[^/]*(?:/|$)")  # /workbooks/wb__d213b28ffce17596, /issues/1234

SNAPSHOT_JS = r"""
const { chromium } = require('@playwright/test');
const BASE = process.argv[2];
const KNOWN = JSON.parse(process.argv[3] || '[]');  // pages the inherited app had: opened even when nothing links to them now
const MAX_PAGES = 12 + KNOWN.length;
(async () => {
  const browser = await chromium.launch(); const page = await (await browser.newContext()).newPage();
  page.setDefaultTimeout(5000);
  let errors = [];
  page.on('pageerror', (e) => errors.push(String(e.message || e).slice(0, 200)));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)); });
  page.on('response', (r) => {
    if (!r.url().startsWith(BASE)) return;
    if (r.status() >= 400) errors.push(`HTTP ${r.status()} ${r.url().slice(BASE.length)}`);
    const ct = r.headers()['content-type'] || '';
    if (r.request().resourceType() === 'script' && r.status() < 400 && !/javascript|ecmascript/.test(ct))
      errors.push(`script ${r.url().slice(BASE.length)} was served as ${ct.split(';')[0]}: the file does not exist or an import path is wrong`);
  });
  const collect = async () => page.evaluate(() => {
    const names = new Set(); const all = [];
    const label = (el) => (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('placeholder') || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    for (const el of document.querySelectorAll('a, button, h1, h2, h3, input, select, textarea, [role=tab], [role=menuitem], [role=link], [role=button]')) {
      const role = el.getAttribute('role') || el.tagName.toLowerCase();
      const name = el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA'
        ? (el.getAttribute('aria-label') || (el.id && document.querySelector(`label[for="${el.id}"]`)?.innerText) || el.getAttribute('placeholder') || '').trim()
        : label(el);
      if (name) { names.add(`${role}:${name}`); if (['a', 'button', 'input', 'select', 'textarea', 'link', 'tab', 'menuitem'].includes(role)) all.push(`${role}:${name}`); }
    }
    const links = [...document.querySelectorAll('a[href]')].map((a) => a.getAttribute('href')).filter((h) => h && h.startsWith('/') && !h.startsWith('//'));
    const counts = {}; for (const k of all) counts[k] = (counts[k] || 0) + 1;
    return { names: [...names], links: [...new Set(links)], dups: Object.entries(counts).filter(([, n]) => n > 1).map(([k, n]) => `${k} x${n}`) };
  });
  const out = {}; const queue = ['/', ...KNOWN.filter((k) => k !== '/')]; const seen = new Set();
  while (queue.length && seen.size < MAX_PAGES) {
    const path = queue.shift(); if (seen.has(path)) continue; seen.add(path);
    errors = [];
    try {
      await page.goto(BASE + path, { waitUntil: 'load' }); await page.waitForTimeout(400);
      const r = await collect(); out[path] = r.names; out[path + ' #dups'] = r.dups;
      out[path + ' #mainLinks'] = await page.locator('main a, [role=main] a, main [role=link], [role=main] [role=link]').evaluateAll((els) => els.map((e) => (e.getAttribute('aria-label') || e.innerText || '').trim()));
      out[path + ' #bannerSearch'] = await page.locator('header input[type=search], [role=banner] input[type=search], header [role=searchbox], [role=banner] [role=searchbox]').evaluateAll((els) => els.some((e) => ((e.getAttribute('aria-label') || '') + ' ' + (e.labels && e.labels[0] ? e.labels[0].innerText : '')).trim().split(/\s+/)[0] === 'Search' || e.getAttribute('aria-label') === 'Search'));
      for (const l of r.links) if (!seen.has(l)) queue.push(l);  // every page's links, up to MAX_PAGES: a crash two clicks deep is found
    } catch (e) { out[path] = []; errors.push(String(e.message || e).slice(0, 200)); }
    if (errors.length) out[path + ' #errors'] = [...new Set(errors)].slice(0, 3);
  }
  console.log(JSON.stringify(out)); await browser.close();
})().catch((e) => { console.log(JSON.stringify({})); });
"""


def take(node_runner, pw: Path, base: str, known: list[str] = ()) -> dict:
    """{path: [role:name]} for the home page, the pages it links to and the `known` pages (node_runner runs a
    Playwright script)."""
    try:
        return json.loads(node_runner(pw, SNAPSHOT_JS, [base, json.dumps(list(known))], 180))
    except (json.JSONDecodeError, TypeError):
        return {}


def duplicates(after: dict) -> list[str]:
    """Interactive controls that share an exact name on a page (tests look controls up by exact name and fail on
    two matches: a page with two headers shows "Sign in" twice)."""
    out = []
    for key, dups in after.items():
        if key.endswith(" #dups") and dups and isinstance(dups, list):
            page = key[:-len(" #dups")]
            out.append(f"page {page} shows the same control name more than once: " + ", ".join(dups[:8])
                       + " (tests find controls by their exact name; a duplicate fails them)")
    return out


def lost(before: dict, after: dict) -> list[str]:
    """Human-readable lines for controls a page had before and lacks now (pages that no longer open count whole)."""
    out = []
    for path, names in before.items():
        if not names or " #" in path:
            continue
        now = set(after.get(path, []))
        gone = [n for n in names if n not in now]
        errors = after.get(path + " #errors") or []
        if _ID_SEGMENT.search(path) and (not now or len(gone) * 2 >= len(names) or any("HTTP 404" in e for e in errors)):
            continue  # a page addressed by a generated id: re-seeding gives new ids, the record is elsewhere, not lost
        if gone:
            out.append(f"page {path} lost {len(gone)} of its {len(names)} controls that the inherited application showed: "
                       + ", ".join(gone[:10]) + (f"; the page logged: {' | '.join(errors)}" if errors else ""))
    return out


def fatal(before: dict, after: dict) -> bool:
    """The home page lost at least half of its controls."""
    home = before.get("/") or []
    if not home:
        return False
    now = set(after.get("/", []))
    return sum(1 for n in home if n not in now) * 2 >= len(home)
