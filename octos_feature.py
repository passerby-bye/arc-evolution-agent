"""A feature implemented by the Octos kernel instead of the two-step edit: Octos runs its own tool loop in the
application directory (reads files, edits them, runs the build) for one turn per request. Used for new features
(EVO_OCTOS=1), where a whole new module and its wiring are needed; the harness still runs the scenario tests and
feeds their failures back as the next turn. octos_stdio.py is the arc adapter's driver, copied unchanged."""
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from octos_stdio import OctosStdioSession

TURN_SECONDS = int(os.environ.get("EVO_OCTOS_TURN_SECONDS", "1200"))  # pass rate first: long turns are not cut early


# the same release the arc adapter (octos-arc/arc/main.py) downloads; mirrors first, as there
OCTOS_RELEASE_URL = ("https://github.com/octos-org/octos-arc/releases/download/v2.0.3-rc.11-arc.13/"
                     "octos-bundle-x86_64-unknown-linux-gnu.tar.gz")


def octos_bin() -> str | None:
    """OCTOS_BIN, PATH, the local install, else the release downloaded into OCTOS_CACHE_DIR (arc's _download_octos)."""
    for cand in (os.environ.get("OCTOS_BIN"), shutil.which("octos"), str(Path.home() / "octos" / "octos")):
        if cand and Path(cand).is_file():
            return cand
    try:
        return _download(Path(os.environ.get("OCTOS_CACHE_DIR", "/tmp/octos-bin")))
    except Exception as e:  # noqa: BLE001
        print(f"[octos] no binary: {e}", flush=True)
        return None


def _download(dest: Path) -> str:
    import subprocess
    import tarfile
    import urllib.request
    url = os.environ.get("OCTOS_RELEASE_URL", OCTOS_RELEASE_URL)
    binary = dest / "octos"
    marker = dest / "source-url.txt"
    if binary.is_file() and marker.is_file() and marker.read_text() == url:
        return str(binary)
    dest.mkdir(parents=True, exist_ok=True)
    tarball = dest / "octos-bundle.tar.gz"
    tarball.unlink(missing_ok=True)

    def ok() -> bool:
        try:
            with tarfile.open(tarball) as tf:
                return tf.getmember("octos") is not None
        except Exception:  # noqa: BLE001
            return False

    mirrors = [f"{prefix}/{url}" for prefix in ("https://ghfast.top", "https://gh-proxy.com")] + [url]
    for attempt in range(1, 13):
        if ok():
            break
        mirror = mirrors[(attempt - 1) % len(mirrors)]
        print(f"[octos] download attempt {attempt} ({mirror[:60]})", flush=True)
        if shutil.which("curl"):
            try:
                subprocess.run(["curl", "-fsSL", "--http1.1", "-C", "-", "--connect-timeout", "30", "--speed-limit", "10240",
                                "--speed-time", "60", "--retry", "2", "-o", str(tarball), mirror], check=False, timeout=600)
            except subprocess.TimeoutExpired:
                pass
        else:
            try:
                urllib.request.urlretrieve(mirror, tarball)
            except Exception as exc:  # noqa: BLE001
                print(f"[octos] download error: {exc}", flush=True)
    if not ok():
        raise RuntimeError("octos binary could not be downloaded")
    with tarfile.open(tarball) as tf:
        for member in ("octos", "octos-sandbox"):
            try:
                tf.extract(member, dest, filter="data")
            except KeyError:
                pass
    binary.chmod(0o755)
    if (dest / "octos-sandbox").exists():
        (dest / "octos-sandbox").chmod(0o755)
    marker.write_text(url)
    return str(binary)


class Implementer:
    """One Octos session per feature: the first turn implements, later turns fix (the session keeps its context)."""

    def __init__(self, out: Path, log):
        self.out, self.log = out, log
        self.home = Path(tempfile.mkdtemp(prefix="evo-octos-"))
        base_url = os.environ.get("OPENAI_BASE_URL", "")
        model = os.environ.get("MODEL", "")
        config = {"provider": "openai", "model": model, "base_url": base_url,
                  "sandbox": {"allow_network": True}, "memory": {"refresh": {"enabled": False}},
                  # arc sets low reasoning for deepseek: at higher effort the platform runs ended in output_truncated
                  "gateway": {"max_output_tokens": 65536, "reasoning_effort": os.environ.get("EVO_OCTOS_EFFORT", "low")}}
        (self.home / "config").mkdir()
        (self.home / "config" / "config.json").write_text(json.dumps(config), encoding="utf-8")
        # the servers Octos starts for its own checks get a port of their own, never the harness test port
        env = dict(os.environ, PORT=os.environ.get("EVO_OCTOS_APP_PORT", "38123"), OCTOS_CONFIG_DIR=str(self.home / "config"), OCTOS_DISABLE_STREAMING="1",
                   OCTOS_DANGER_FULL_ACCESS="1")
        self.events = self.home / "events.jsonl"
        self.tools = 0

        def on_event(method: str, params: dict) -> None:
            if method == "tool/started":
                self.tools += 1
            with self.events.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"method": method, "params": params}, ensure_ascii=False)[:2000] + "\n")

        binary = octos_bin()
        if not binary:
            raise RuntimeError("no octos binary")
        self._start = lambda: self._open_session(binary, out, env, model, base_url, on_event)
        self._start()

    def _open_session(self, binary, out, env, model, base_url, on_event):
        self.session = OctosStdioSession(binary, out, env, self.home / "data", on_event=on_event)
        self.session.bootstrap_profile(provider="openai", model=model, base_url=base_url or None, api_key_env="OPENAI_API_KEY")
        self.session.open()

    def _restart(self):
        """A timed-out turn may still be running in the kernel: a new session continues from the files on disk."""
        try:
            self.session.close()
        except Exception:  # noqa: BLE001
            pass
        self._start()

    def turn(self, text: str, label: str) -> bool:
        """One request; a turn cut off by the output budget (or a dropped request) is continued from where it
        stopped, up to twice, instead of counting as a failure (platform runs: three features lost this way)."""
        prompt = text
        for attempt in range(3):
            before = self.tools
            try:
                ok, reply = self.session.run_turn(prompt, timeout=TURN_SECONDS)
            except Exception as e:  # noqa: BLE001  # a protocol error fails this request, never the whole feature
                ok, reply = False, f"runtime_error: {type(e).__name__}: {e}"
            self.log(f"octos {label}{' (continued)' if attempt else ''}: {'ok' if ok else 'failed'}, "
                     f"{self.tools - before} tool calls; {reply.strip()[:160]!r}")
            if ok:
                return True
            if not re.search(r"output_truncated|max_tokens|failed to send request|timed out|runtime_error", reply):
                return False
            if re.search(r"timed out|already running|runtime_error", reply):
                try:
                    self._restart()
                except Exception as e:  # noqa: BLE001
                    self.log(f"octos {label}: could not restart the session ({e})")
                    return False
            prompt = CONTINUE_PROMPT
        return False

    def close(self) -> None:
        self.session.close()


PROMPT = """\
You are working in an existing web application (frontend/ and backend/ in the current directory). Implement the
feature below completely, so every scenario works end to end in the browser: the control that opens it, every
named field, button and message (exact names and text as quoted), the effect on the data and the page,
persistence through the application's existing save path, and the same state after a page reload.

Rules:
- Read the relevant files first (the page's main script, the module that renders the grid or page, the server
  routes and the data store) and follow the application's existing structure and style.
- Keep every existing control, route and behaviour working; do not rename or remove anything other features use.
- Records the scenarios name already exist in the seed data; do not change the seed data.
- After editing, run the frontend build (`cd frontend && npm run build`) and `node --check` on changed backend
  files, and fix any error before you finish.
- Do not write tests. If you start the server to check your work, stop it again before you finish.
  Finish with one line saying what you changed.

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

FIX_PROMPT = """\
The application was checked after your change. These problems remain; fix them (same rules as before: keep
existing behaviour, rebuild the frontend and fix build errors before you finish):

{problems}
"""

CONTINUE_PROMPT = """\
Your previous reply was cut off before you finished. Look at the files as they are now, finish the remaining
changes for the same feature in small steps (one file or function per edit), rebuild the frontend, and end with
one line saying what you changed.
"""
