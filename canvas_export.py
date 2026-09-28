#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
canvas_export.py - Export everything from a Canvas LMS course into a single zip.

Grabs files, pages, modules, assignments and quizzes from any Canvas instance
using the official REST API, rebuilds them as a browsable offline site, and
packs the result into a .zip you can keep forever.

Zero dependencies: Python 3.8+ standard library only.

Quick start
-----------
    # 1. Make a token:  Canvas -> Account -> Settings -> "+ New Access Token"
    # 2. Save your credentials once (optional):
    python canvas_export.py --url https://canvas.school.edu --token XXXX --save-config

    # 3. See what you are enrolled in:
    python canvas_export.py --list-courses

    # 4. Export:
    python canvas_export.py --course 12345
    python canvas_export.py --course https://canvas.school.edu/courses/12345
    python canvas_export.py --course all -o ~/Desktop

Author: built for Divyaansh.
License: MIT.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import html as html_mod
import json
import os
import posixpath
import re
import shutil
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

__version__ = "1.1.0"

if sys.version_info < (3, 8):  # pragma: no cover
    sys.exit("canvas_export requires Python 3.8 or newer.")

# --------------------------------------------------------------------------
# Configuration / constants
# --------------------------------------------------------------------------

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".canvas_export.json")

ALL_SECTIONS = [
    "files",
    "pages",
    "modules",
    "assignments",
    "quizzes",
    "syllabus",
    "announcements",
    "discussions",
]

# What gets exported when you don't say otherwise.
DEFAULT_SECTIONS = ["files", "pages", "modules", "assignments", "quizzes"]

USER_AGENT = f"canvas-export/{__version__} (+python-urllib)"

MAX_RETRIES = 5
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}

# Windows reserved device names - a file called "con.txt" is unopenable there.
_WIN_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

class Log:
    """Dead simple leveled logger writing to stderr."""

    def __init__(self, verbose: bool = False, quiet: bool = False):
        self.verbose = verbose
        self.quiet = quiet
        self._lock = threading.Lock()

    def _emit(self, prefix: str, msg: str) -> None:
        with self._lock:
            sys.stderr.write(f"{prefix}{msg}\n")
            sys.stderr.flush()

    def info(self, msg: str) -> None:
        if not self.quiet:
            self._emit("  ", msg)

    def step(self, msg: str) -> None:
        if not self.quiet:
            self._emit("\n== ", msg)

    def debug(self, msg: str) -> None:
        if self.verbose and not self.quiet:
            self._emit("  . ", msg)

    def warn(self, msg: str) -> None:
        self._emit("  ! ", msg)

    def error(self, msg: str) -> None:
        self._emit("ERROR: ", msg)


LOG = Log()


def esc(text) -> str:
    """HTML-escape a value of any type."""
    if text is None:
        return ""
    return html_mod.escape(str(text), quote=True)


def human_size(num_bytes) -> str:
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def pretty_date(value) -> str:
    """Canvas hands back ISO-8601 in UTC; render something a human can read."""
    if not value:
        return ""
    try:
        cleaned = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%a %d %b %Y, %I:%M %p").replace(" 0", " ")
    except Exception:
        return str(value)


def safe_name(name: str, fallback: str = "untitled", max_len: int = 110) -> str:
    """Turn arbitrary text into a filename that survives Windows, macOS and zip."""
    name = urllib.parse.unquote(str(name or "")).strip()
    name = _ILLEGAL_CHARS.sub("-", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = fallback
    stem, dot, ext = name.rpartition(".")
    if dot and len(ext) <= 8 and stem:
        if len(stem) > max_len:
            stem = stem[:max_len].rstrip(" .-")
        name = f"{stem}.{ext}"
    elif len(name) > max_len:
        name = name[:max_len].rstrip(" .-")
    if name.split(".")[0].lower() in _WIN_RESERVED:
        name = "_" + name
    return name or fallback


def slugify(text: str, fallback: str = "item", max_len: int = 60) -> str:
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    text = html_mod.unescape(text)
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text or fallback


def unique_path(directory: str, filename: str, taken: set) -> str:
    """Pick a name inside *directory* that is not already used."""
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    key = os.path.join(directory, filename).lower()
    if key not in taken:
        taken.add(key)
        return filename
    i = 2
    while True:
        candidate = f"{stem} ({i}).{ext}" if ext else f"{stem} ({i})"
        key = os.path.join(directory, candidate).lower()
        if key not in taken:
            taken.add(key)
            return candidate
        i += 1


def strip_tags(text: str, limit: int = 240) -> str:
    plain = re.sub(r"<[^>]+>", " ", str(text or ""))
    plain = html_mod.unescape(plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:limit] + ("..." if len(plain) > limit else "")


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

class CanvasError(Exception):
    def __init__(self, message: str, status: int = 0, url: str = ""):
        super().__init__(message)
        self.status = status
        self.url = url


class _StripAuthRedirect(urllib.request.HTTPRedirectHandler):
    """
    Canvas file URLs redirect to S3 / instfs. Amazon rejects requests that carry
    both a signed query string and an Authorization header ("Only one auth
    mechanism allowed"), so drop the header whenever the host changes.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None
        old_host = urllib.parse.urlsplit(req.full_url).netloc.lower()
        new_host = urllib.parse.urlsplit(newurl).netloc.lower()
        if old_host != new_host:
            for key in list(new_req.headers):
                if key.lower() == "authorization":
                    del new_req.headers[key]
            try:
                new_req.unredirected_hdrs.pop("Authorization", None)
            except Exception:
                pass
        return new_req


class CanvasClient:
    """Thin, retrying client for the Canvas REST API."""

    def __init__(self, base_url: str, token: str, timeout: int = 60,
                 verify_tls: bool = True):
        self.base_url = base_url.rstrip("/")
        self.host = urllib.parse.urlsplit(self.base_url).netloc.lower()
        self.token = token
        self.timeout = timeout
        ctx = ssl.create_default_context()
        if not verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            _StripAuthRedirect(),
        )
        self.api_calls = 0
        self._lock = threading.Lock()

    # -- low level ---------------------------------------------------------

    def _request(self, url: str, want_bytes: bool = False):
        """Perform a GET with retry/backoff. Returns (payload, headers)."""
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            req = urllib.request.Request(url, method="GET")
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("User-Agent", USER_AGENT)
            if not want_bytes:
                req.add_header("Accept", "application/json+canvas-string-ids, application/json")
            try:
                with self._lock:
                    self.api_calls += 1
                resp = self._opener.open(req, timeout=self.timeout)
                return resp, dict(resp.headers)
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read(4096).decode("utf-8", "replace")
                except Exception:
                    pass
                # Canvas signals throttling with 403 + a specific body.
                throttled = exc.code == 403 and "Rate Limit Exceeded" in body
                if exc.code in RETRY_STATUSES or throttled:
                    if attempt < MAX_RETRIES:
                        wait = min(60, 2 ** attempt) + (0.3 * attempt)
                        LOG.debug(f"HTTP {exc.code} on {url} - retry in {wait:.0f}s")
                        time.sleep(wait)
                        last_error = exc
                        continue
                raise CanvasError(
                    self._explain(exc.code, body, url), status=exc.code, url=url
                ) from exc
            except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
                last_error = exc
                if attempt < MAX_RETRIES:
                    wait = min(30, 2 ** attempt)
                    LOG.debug(f"Network error on {url} ({exc}) - retry in {wait}s")
                    time.sleep(wait)
                    continue
                raise CanvasError(f"Network error for {url}: {exc}", url=url) from exc
        raise CanvasError(f"Gave up on {url}: {last_error}", url=url)

    @staticmethod
    def _explain(code: int, body: str, url: str) -> str:
        snippet = strip_tags(body, 200)
        if code == 401:
            return ("401 Unauthorized - the access token is missing, expired or "
                    f"revoked. ({url})")
        if code == 403:
            return (f"403 Forbidden - your account cannot see this. Often the "
                    f"course has that tab disabled, or the endpoint is "
                    f"teacher-only. ({url}) {snippet}")
        if code == 404:
            return f"404 Not Found - {url}. Check the course ID and the Canvas URL."
        return f"HTTP {code} for {url}: {snippet}"

    # -- JSON --------------------------------------------------------------

    def get_json(self, path: str, **params):
        url = self._build_url(path, params)
        resp, _ = self._request(url)
        with resp:
            raw = resp.read()
        text = raw.decode("utf-8", "replace")
        # Canvas prefixes some responses with a JS defence prelude.
        if text.startswith("while(1);"):
            text = text[len("while(1);"):]
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise CanvasError(f"Bad JSON from {url}: {exc}", url=url) from exc

    def paginate(self, path: str, **params):
        """Yield every item across every page, following RFC-5988 Link headers."""
        params.setdefault("per_page", 100)
        url = self._build_url(path, params)
        seen_urls = set()
        while url and url not in seen_urls:
            seen_urls.add(url)
            resp, headers = self._request(url)
            with resp:
                raw = resp.read()
            text = raw.decode("utf-8", "replace")
            if text.startswith("while(1);"):
                text = text[len("while(1);"):]
            try:
                data = json.loads(text) if text.strip() else []
            except json.JSONDecodeError as exc:
                raise CanvasError(f"Bad JSON from {url}: {exc}", url=url) from exc
            if isinstance(data, dict):
                # A few endpoints wrap the list, e.g. {"discussion_topics": [...]}
                for value in data.values():
                    if isinstance(value, list):
                        data = value
                        break
                else:
                    data = [data]
            for item in data:
                yield item
            url = self._next_link(headers.get("Link") or headers.get("link"))

    @staticmethod
    def _next_link(link_header):
        if not link_header:
            return None
        for part in link_header.split(","):
            chunk = part.strip()
            if not chunk.startswith("<"):
                continue
            try:
                target, _, rest = chunk[1:].partition(">")
            except ValueError:
                continue
            if 'rel="next"' in rest or "rel=next" in rest:
                return target
        return None

    def _build_url(self, path: str, params: dict) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = f"{self.base_url}/api/v1/{path.lstrip('/')}"
        pairs = []
        for key, value in (params or {}).items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                for item in value:
                    pairs.append((key if key.endswith("[]") else key + "[]", item))
            else:
                pairs.append((key, value))
        if pairs:
            joiner = "&" if "?" in url else "?"
            url = url + joiner + urllib.parse.urlencode(pairs)
        return url

    # -- binary ------------------------------------------------------------

    def download(self, url: str, dest: str, expected_size=None) -> int:
        """Stream a URL to disk. Returns bytes written. Skips if already there."""
        if expected_size and os.path.exists(dest):
            if os.path.getsize(dest) == int(expected_size):
                LOG.debug(f"already have {os.path.basename(dest)}")
                return 0
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        part = dest + ".part"
        resp, _ = self._request(url, want_bytes=True)
        total = 0
        try:
            with resp, open(part, "wb") as fh:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    fh.write(chunk)
                    total += len(chunk)
            os.replace(part, dest)
        finally:
            if os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass
        return total


# --------------------------------------------------------------------------
# Presentation: the little offline site the zip contains
# --------------------------------------------------------------------------

STYLESHEET = """\
:root {
  --bg: #fbfaf8; --surface: #ffffff; --ink: #1f1d1b; --muted: #6b6560;
  --line: #e4e0da; --accent: #9a5b2c; --accent-soft: #f4ece4;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #171614; --surface: #201e1c; --ink: #ece8e3; --muted: #a49d95;
    --line: #34312d; --accent: #d79a63; --accent-soft: #2b2622;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
  -webkit-text-size-adjust: 100%;
}
main { max-width: 860px; margin: 0 auto; padding: 8px 20px 72px; }
.ce-header {
  border-bottom: 1px solid var(--line); background: var(--surface);
  padding: 12px 20px; position: sticky; top: 0; z-index: 5;
}
.ce-home { color: var(--accent); text-decoration: none; font-size: 14px; font-weight: 600; }
.ce-home:hover { text-decoration: underline; }
h1 { font-size: 1.9rem; line-height: 1.25; margin: 28px 0 8px; letter-spacing: -0.02em; }
h2 { font-size: 1.25rem; margin: 34px 0 10px; letter-spacing: -0.01em; }
h3 { font-size: 1.05rem; margin: 24px 0 8px; }
a { color: var(--accent); }
p, li { overflow-wrap: anywhere; }
img, video, iframe, table { max-width: 100%; }
img { height: auto; }
pre { overflow-x: auto; background: var(--accent-soft); padding: 12px; border-radius: 8px; }
code { background: var(--accent-soft); padding: 1px 5px; border-radius: 4px; font-size: 0.9em; }
blockquote { margin: 16px 0; padding: 4px 16px; border-left: 3px solid var(--line); color: var(--muted); }
table { border-collapse: collapse; width: 100%; display: block; overflow-x: auto; }
th, td { border: 1px solid var(--line); padding: 7px 10px; text-align: left; vertical-align: top; }
.meta { color: var(--muted); font-size: 0.87rem; margin: 0 0 18px; }
.meta span + span::before { content: " · "; }
.card {
  background: var(--surface); border: 1px solid var(--line); border-radius: 12px;
  padding: 16px 18px; margin: 12px 0;
}
.grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); margin: 18px 0 8px; }
.grid a.card { text-decoration: none; color: inherit; display: block; }
.grid a.card:hover { border-color: var(--accent); }
.grid .n { display: block; font-size: 1.7rem; font-weight: 600; letter-spacing: -0.02em; }
.grid .l { display: block; color: var(--muted); font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.06em; }
ul.list { list-style: none; padding: 0; margin: 8px 0; }
ul.list li { border-bottom: 1px solid var(--line); padding: 9px 2px; }
ul.list li:last-child { border-bottom: 0; }
ul.list .sub { color: var(--muted); font-size: 0.85rem; }
.indent-1 { padding-left: 22px !important; }
.indent-2 { padding-left: 44px !important; }
.indent-3 { padding-left: 66px !important; }
.tag {
  display: inline-block; font-size: 0.72rem; text-transform: uppercase;
  letter-spacing: 0.06em; background: var(--accent-soft); color: var(--muted);
  border-radius: 999px; padding: 2px 9px; margin-right: 7px; vertical-align: 1px;
}
.note { background: var(--accent-soft); border-radius: 8px; padding: 10px 14px; font-size: 0.9rem; color: var(--muted); }
.q { border-left: 3px solid var(--accent); padding-left: 14px; margin: 20px 0; }
.q .answers { list-style: none; padding: 0; }
.q .answers li { padding: 4px 0; }
.q .correct { font-weight: 600; color: var(--accent); }
hr { border: 0; border-top: 1px solid var(--line); margin: 28px 0; }
footer { color: var(--muted); font-size: 0.8rem; border-top: 1px solid var(--line); margin-top: 40px; padding-top: 14px; }
"""


def html_doc(title: str, body: str, depth: int, subtitle: str = "") -> str:
    up = "../" * depth
    meta = f'<p class="meta">{subtitle}</p>\n' if subtitle else ""
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{esc(title)}</title>\n"
        f"<link rel=\"stylesheet\" href=\"{up}_assets/style.css\">\n"
        "</head>\n<body>\n"
        f"<header class=\"ce-header\"><a class=\"ce-home\" href=\"{up}index.html\">"
        "&#8592; Course index</a></header>\n"
        f"<main>\n<h1>{esc(title)}</h1>\n{meta}{body}\n"
        "<footer>Exported from Canvas with canvas_export.py</footer>\n"
        "</main>\n</body>\n</html>\n"
    )


# --------------------------------------------------------------------------
# The exporter
# --------------------------------------------------------------------------

class CourseExporter:
    """Pulls one course down into *root_dir* as a browsable folder."""

    def __init__(self, client: CanvasClient, course: dict, root_dir: str,
                 sections, jobs: int = 5, max_file_bytes=None):
        self.api = client
        self.course = course
        self.course_id = str(course.get("id"))
        self.root = root_dir
        self.sections = set(sections)
        self.jobs = max(1, int(jobs))
        self.max_file_bytes = max_file_bytes

        # id -> path relative to self.root, used for offline link rewriting
        self.file_paths = {}
        self.page_paths = {}
        self.assignment_paths = {}
        self.quiz_paths = {}
        self.topic_paths = {}

        # Reserve the section index pages so a page titled "Index" cannot
        # silently overwrite pages/index.html and friends.
        self.taken_names = {
            os.path.join(d, "index.html").lower()
            for d in ("pages", "assignments", "quizzes", "announcements",
                      "discussions", "modules", "files")
        }
        self.written_html = []          # course-root-relative html paths
        self._modules = []
        self._page_index = []           # (title, rel)
        self._assignment_index = []     # (title, rel, due_at)
        self._quiz_index = []           # (title, rel, question_count)
        self._topic_index = {}          # folder -> [(title, rel, posted_at)]
        self.stats = {}
        self.warnings = []
        self.bytes_downloaded = 0
        self._counter_lock = threading.Lock()

    # -- utilities ---------------------------------------------------------

    def path(self, *parts) -> str:
        return os.path.join(self.root, *parts)

    def write_html(self, rel_path: str, title: str, body: str, subtitle: str = "") -> None:
        abs_path = self.path(rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        depth = rel_path.replace("\\", "/").count("/")
        with open(abs_path, "w", encoding="utf-8") as fh:
            fh.write(html_doc(title, body, depth, subtitle))
        self.written_html.append(rel_path.replace("\\", "/"))

    def write_json(self, rel_path: str, data) -> None:
        abs_path = self.path(rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)

    def note_warning(self, msg: str) -> None:
        self.warnings.append(msg)
        LOG.warn(msg)

    def collect(self, endpoint: str, label: str, **params):
        """paginate() but turns a permission error into a warning, not a crash."""
        try:
            return list(self.api.paginate(endpoint, **params))
        except CanvasError as exc:
            if exc.status in (401, 403, 404):
                self.note_warning(f"Skipped {label}: {exc}")
                return []
            raise

    # -- 1. course shell ---------------------------------------------------

    def export(self) -> dict:
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(self.path("_assets"), exist_ok=True)
        with open(self.path("_assets", "style.css"), "w", encoding="utf-8") as fh:
            fh.write(STYLESHEET)
        self.write_json("course.json", self.course)

        if "files" in self.sections:
            self.export_files()
        if "pages" in self.sections:
            self.export_pages()
        if "assignments" in self.sections:
            self.export_assignments()
        if "quizzes" in self.sections:
            self.export_quizzes()
        if "announcements" in self.sections:
            self.export_topics(announcements=True)
        if "discussions" in self.sections:
            self.export_topics(announcements=False)
        if "syllabus" in self.sections:
            self.export_syllabus()
        if "modules" in self.sections:
            self.export_modules()

        # Listings can be closed off even when the items themselves are not;
        # the module tree is the reliable way back to them.
        self.backfill_from_modules()

        if "files" in self.sections:
            self.fetch_linked_files()
        self.write_section_indexes()
        self.rewrite_links()
        self.build_index()
        return self.stats

    # -- 2. files ----------------------------------------------------------

    def export_files(self) -> None:
        LOG.step("Files")
        folders = self.collect(f"courses/{self.course_id}/folders", "course folders")
        folder_map = {}
        for folder in folders:
            full = str(folder.get("full_name") or "")
            # Canvas roots everything at "course files"
            parts = [p for p in full.split("/")[1:] if p]
            folder_map[str(folder.get("id"))] = "/".join(safe_name(p) for p in parts)

        files = self.collect(f"courses/{self.course_id}/files", "course files")
        if not files and not folders:
            LOG.info("No files visible through the Files tab "
                     "(it may be hidden - linked files are still picked up later).")

        jobs = []
        skipped_locked = 0
        skipped_big = 0
        for meta in files:
            if meta.get("locked_for_user") and not meta.get("url"):
                skipped_locked += 1
                continue
            url = meta.get("url")
            if not url:
                skipped_locked += 1
                continue
            size = meta.get("size") or 0
            if self.max_file_bytes and size and size > self.max_file_bytes:
                skipped_big += 1
                self.note_warning(
                    f"Skipped '{meta.get('display_name')}' ({human_size(size)}) - over --max-file-size"
                )
                continue
            sub = folder_map.get(str(meta.get("folder_id")), "_unsorted")
            display = meta.get("display_name") or meta.get("filename") or f"file-{meta.get('id')}"
            directory = posixpath.join("files", sub) if sub else "files"
            name = unique_path(directory, safe_name(display), self.taken_names)
            rel = posixpath.join(directory, name)
            self.file_paths[str(meta.get("id"))] = rel
            jobs.append((url, rel, size, display))

        self._download_many(jobs)
        self.stats["files"] = len(jobs)
        if skipped_locked:
            LOG.info(f"{skipped_locked} file(s) locked or unavailable to your account.")
        if skipped_big:
            LOG.info(f"{skipped_big} file(s) skipped for size.")

    def _download_many(self, jobs) -> None:
        if not jobs:
            return
        total = len(jobs)
        done = 0

        def work(job):
            url, rel, size, label = job
            try:
                written = self.api.download(url, self.path(rel), expected_size=size)
                return written, label, None
            except CanvasError as exc:
                return 0, label, str(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.jobs) as pool:
            for written, label, err in pool.map(work, jobs):
                done += 1
                if err:
                    self.note_warning(f"Download failed: {label} - {err}")
                else:
                    with self._counter_lock:
                        self.bytes_downloaded += written
                    LOG.info(f"[{done}/{total}] {label} ({human_size(written) if written else 'cached'})")

    def fetch_linked_files(self) -> None:
        """
        Pages and assignments often embed files that the Files tab never lists
        (hidden files, or a course with Files switched off). Scan everything we
        wrote for /files/<id> references and fetch whatever is still missing.
        """
        pattern = re.compile(r"/files/(\d+)")
        wanted = set()
        for rel in self.written_html:
            try:
                with open(self.path(rel), "r", encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            for match in pattern.finditer(text):
                wanted.add(match.group(1))
        missing = [fid for fid in wanted if fid not in self.file_paths]
        if not missing:
            return
        LOG.step(f"Embedded files ({len(missing)} referenced but not in the Files tab)")
        jobs = []
        for fid in sorted(missing):
            try:
                meta = self.api.get_json(f"courses/{self.course_id}/files/{fid}")
            except CanvasError:
                try:
                    meta = self.api.get_json(f"files/{fid}")
                except CanvasError as exc:
                    LOG.debug(f"file {fid} unreachable: {exc}")
                    continue
            if not meta or not meta.get("url"):
                continue
            size = meta.get("size") or 0
            if self.max_file_bytes and size and size > self.max_file_bytes:
                continue
            display = meta.get("display_name") or meta.get("filename") or f"file-{fid}"
            name = unique_path("files/_embedded", safe_name(display), self.taken_names)
            rel = posixpath.join("files", "_embedded", name)
            self.file_paths[str(fid)] = rel
            jobs.append((meta["url"], rel, size, display))
        self._download_many(jobs)
        self.stats["files"] = self.stats.get("files", 0) + len(jobs)

    # -- 3. pages ----------------------------------------------------------

    def export_pages(self) -> None:
        LOG.step("Pages")
        listing = self.collect(f"courses/{self.course_id}/pages", "pages", sort="title")
        if not listing:
            LOG.info("The Pages listing returned nothing. If the course hides "
                     "its Pages tab, pages linked from modules are still "
                     "recovered later.")
        for n, item in enumerate(listing, start=1):
            slug = item.get("url") or slugify(item.get("title"))
            if self.add_page(slug, fallback=item):
                LOG.info(f"[{n}/{len(listing)}] {item.get('title') or slug}")

    def add_page(self, slug, fallback=None):
        """Fetch and write one wiki page. Returns its path, or None."""
        key = str(slug or "").lower()
        if not key:
            return None
        if key in self.page_paths:
            return self.page_paths[key]
        try:
            page = self.api.get_json(
                f"courses/{self.course_id}/pages/"
                f"{urllib.parse.quote(str(slug), safe='')}")
        except CanvasError as exc:
            if fallback is None:
                LOG.debug(f"page '{slug}' unavailable: {exc}")
                return None
            self.note_warning(
                f"Page '{fallback.get('title') or slug}' unavailable: {exc}")
            page = dict(fallback)
            page["body"] = "<p><em>Body could not be retrieved.</em></p>"
        if not page:
            return None
        title = page.get("title") or slug
        name = unique_path("pages", safe_name(slugify(title) + ".html"), self.taken_names)
        rel = posixpath.join("pages", name)
        self.page_paths[key] = rel
        canonical = str(page.get("url") or "").lower()
        if canonical:
            self.page_paths.setdefault(canonical, rel)
        bits = []
        if page.get("updated_at"):
            bits.append(f"Updated {esc(pretty_date(page['updated_at']))}")
        if page.get("front_page"):
            bits.append("Front page")
        body = page.get("body") or "<p><em>This page is empty.</em></p>"
        self.write_html(rel, title, body,
                        "".join(f"<span>{b}</span>" for b in bits))
        self._page_index.append((title, rel))
        self.stats["pages"] = len(self._page_index)
        return rel

    # -- 4. assignments ----------------------------------------------------

    def export_assignments(self) -> None:
        LOG.step("Assignments")
        items = self.collect(
            f"courses/{self.course_id}/assignments", "assignments",
            order_by="position", include=["submission"],
        )
        for n, item in enumerate(items, start=1):
            if self.add_assignment(item):
                LOG.info(f"[{n}/{len(items)}] {item.get('name')}")

    def add_assignment(self, item):
        """Write one assignment. Returns its path, or None."""
        aid = str(item.get("id") or "")
        if not aid:
            return None
        if aid in self.assignment_paths:
            return self.assignment_paths[aid]
        title = item.get("name") or f"Assignment {aid}"
        folder = f"{len(self._assignment_index) + 1:03d}-{slugify(title)}"
        rel = posixpath.join("assignments", folder, "index.html")
        self.assignment_paths[aid] = rel

        bits = []
        if item.get("due_at"):
            bits.append(f"Due {esc(pretty_date(item['due_at']))}")
        if item.get("points_possible") is not None:
            bits.append(f"{esc(item['points_possible'])} points")
        types = item.get("submission_types") or []
        if types:
            bits.append("Submit via " + esc(", ".join(t.replace("_", " ") for t in types)))
        if item.get("unlock_at"):
            bits.append(f"Available {esc(pretty_date(item['unlock_at']))}")
        subtitle = "".join(f"<span>{b}</span>" for b in bits)

        parts = [item.get("description") or "<p><em>No description.</em></p>"]

        if item.get("is_quiz_lti_assignment"):
            parts.append(
                '<p class="note">This is a <strong>New Quizzes</strong> item. '
                "Canvas does not expose New Quizzes questions over the API, so "
                "only the description could be exported.</p>"
            )

        rubric = item.get("rubric")
        if rubric:
            rows = "".join(
                "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                    esc(c.get("description")),
                    esc(strip_tags(c.get("long_description") or "", 400)),
                    esc(c.get("points")),
                )
                for c in rubric
            )
            parts.append(
                "<h2>Rubric</h2><table><thead><tr><th>Criterion</th>"
                f"<th>Details</th><th>Points</th></tr></thead><tbody>{rows}</tbody></table>"
            )

        sub = item.get("submission") or {}
        if sub and (sub.get("score") is not None or sub.get("submitted_at")):
            facts = []
            if sub.get("submitted_at"):
                facts.append(f"<li>Submitted {esc(pretty_date(sub['submitted_at']))}</li>")
            if sub.get("score") is not None:
                facts.append(f"<li>Score: {esc(sub['score'])} / {esc(item.get('points_possible'))}</li>")
            if sub.get("grade"):
                facts.append(f"<li>Grade: {esc(sub['grade'])}</li>")
            parts.append("<h2>Your submission</h2><ul>" + "".join(facts) + "</ul>")

        if item.get("html_url"):
            parts.append(
                f'<hr><p class="meta">Live version on Canvas: '
                f'<a {self.KEEP_MARKER} href="{esc(item["html_url"])}">'
                f'{esc(item["html_url"])}</a></p>')

        self.write_html(rel, title, "\n".join(parts), subtitle)
        self.write_json(posixpath.join("assignments", folder, "assignment.json"), item)
        self._assignment_index.append((title, rel, item.get("due_at")))
        self.stats["assignments"] = len(self._assignment_index)
        return rel

    # -- 5. quizzes --------------------------------------------------------

    def export_quizzes(self) -> None:
        LOG.step("Quizzes")
        items = self.collect(f"courses/{self.course_id}/quizzes", "quizzes")
        if not items:
            LOG.info("The Quizzes listing returned nothing. If the course hides "
                     "its Quizzes tab, quizzes linked from modules are still "
                     "recovered later.")
        for n, item in enumerate(items, start=1):
            if self.add_quiz(item):
                LOG.info(f"[{n}/{len(items)}] {item.get('title')}")

    def add_quiz(self, item):
        """Write one quiz, with its questions if Canvas will part with them."""
        qid = str(item.get("id") or "")
        if not qid:
            return None
        if qid in self.quiz_paths:
            return self.quiz_paths[qid]
        title = item.get("title") or f"Quiz {qid}"
        folder = f"{len(self._quiz_index) + 1:03d}-{slugify(title)}"
        rel = posixpath.join("quizzes", folder, "index.html")
        self.quiz_paths[qid] = rel

        bits = []
        if item.get("quiz_type"):
            bits.append(esc(str(item["quiz_type"]).replace("_", " ").title()))
        if item.get("points_possible") is not None:
            bits.append(f"{esc(item['points_possible'])} points")
        if item.get("question_count") is not None:
            bits.append(f"{esc(item['question_count'])} questions")
        if item.get("time_limit"):
            bits.append(f"{esc(item['time_limit'])} minute limit")
        if item.get("due_at"):
            bits.append(f"Due {esc(pretty_date(item['due_at']))}")
        subtitle = "".join(f"<span>{b}</span>" for b in bits)

        parts = [item.get("description") or "<p><em>No description.</em></p>"]

        questions = []
        try:
            questions = list(self.api.paginate(
                f"courses/{self.course_id}/quizzes/{qid}/questions"))
        except CanvasError as exc:
            if exc.status in (401, 403, 404):
                parts.append(
                    '<p class="note">Quiz questions were not returned by Canvas. '
                    "The questions endpoint is normally restricted to teachers, or "
                    "only opens while an attempt is in progress.</p>"
                )
            else:
                raise

        if questions:
            parts.append(f"<h2>Questions ({len(questions)})</h2>")
            for qi, q in enumerate(questions, start=1):
                parts.append(self._render_question(qi, q))
            self.write_json(posixpath.join("quizzes", folder, "questions.json"), questions)

        if item.get("html_url"):
            parts.append(
                f'<hr><p class="meta">Live version on Canvas: '
                f'<a {self.KEEP_MARKER} href="{esc(item["html_url"])}">'
                f'{esc(item["html_url"])}</a></p>')

        self.write_html(rel, title, "\n".join(parts), subtitle)
        self.write_json(posixpath.join("quizzes", folder, "quiz.json"), item)
        self._quiz_index.append((title, rel, len(questions)))
        self.stats["quizzes"] = len(self._quiz_index)
        return rel

    @staticmethod
    def _render_question(number: int, q: dict) -> str:
        kind = str(q.get("question_type") or "").replace("_", " ")
        head = (f'<div class="q"><h3>{number}. {esc(q.get("question_name") or "Question")}'
                f'</h3><p class="meta"><span class="tag">{esc(kind)}</span>'
                f'<span>{esc(q.get("points_possible"))} points</span></p>')
        body = q.get("question_text") or ""
        answers = q.get("answers") or []
        if answers:
            items = []
            for ans in answers:
                text = ans.get("text") or ans.get("html") or ""
                correct = ans.get("weight", 0) and float(ans.get("weight") or 0) > 0
                cls = ' class="correct"' if correct else ""
                mark = " &#10003;" if correct else ""
                items.append(f"<li{cls}>{esc(text)}{mark}</li>")
            body += '<ul class="answers">' + "".join(items) + "</ul>"
        if q.get("correct_comments"):
            body += f'<p class="note">{esc(q["correct_comments"])}</p>'
        return head + body + "</div>"

    # -- 6. announcements & discussions ------------------------------------

    def export_topics(self, announcements: bool) -> None:
        label = "Announcements" if announcements else "Discussions"
        LOG.step(label)
        params = {"only_announcements": "true"} if announcements else {}
        topics = self.collect(f"courses/{self.course_id}/discussion_topics",
                              label.lower(), **params)
        if not announcements:
            topics = [t for t in topics if not t.get("is_announcement")]
        for n, topic in enumerate(topics, start=1):
            if self.add_topic(topic, announcements):
                LOG.info(f"[{n}/{len(topics)}] {topic.get('title')}")

    def add_topic(self, topic, announcements=None):
        """Write one announcement or discussion thread."""
        tid = str(topic.get("id") or "")
        if not tid:
            return None
        if tid in self.topic_paths:
            return self.topic_paths[tid]
        if announcements is None:
            announcements = bool(topic.get("is_announcement"))
        folder = "announcements" if announcements else "discussions"
        if folder not in self.sections:
            return None
        index = self._topic_index.setdefault(folder, [])
        title = topic.get("title") or f"Topic {tid}"
        name = unique_path(folder,
                           safe_name(f"{len(index) + 1:03d}-{slugify(title)}.html"),
                           self.taken_names)
        rel = posixpath.join(folder, name)
        self.topic_paths[tid] = rel

        bits = []
        author = (topic.get("author") or {}).get("display_name")
        if author:
            bits.append(esc(author))
        if topic.get("posted_at"):
            bits.append(esc(pretty_date(topic["posted_at"])))
        subtitle = "".join(f"<span>{b}</span>" for b in bits)
        parts = [topic.get("message") or "<p><em>No content.</em></p>"]

        if not announcements:
            try:
                view = self.api.get_json(
                    f"courses/{self.course_id}/discussion_topics/{tid}/view")
            except CanvasError:
                view = None
            entries = (view or {}).get("view") or []
            if entries:
                parts.append(f"<h2>Replies ({len(entries)})</h2>")
                parts.append(self._render_replies(
                    entries, (view or {}).get("participants") or []))

        self.write_html(rel, title, "\n".join(parts), subtitle)
        index.append((title, rel, topic.get("posted_at")))
        self.stats[folder] = len(index)
        return rel

    def _render_replies(self, entries, participants, depth: int = 0) -> str:
        names = {str(p.get("id")): p.get("display_name") for p in participants}
        out = []
        for entry in entries:
            if entry.get("deleted"):
                continue
            who = names.get(str(entry.get("user_id")), "Unknown")
            when = pretty_date(entry.get("created_at"))
            out.append(
                f'<div class="card" style="margin-left:{depth * 18}px">'
                f'<p class="meta"><span>{esc(who)}</span><span>{esc(when)}</span></p>'
                f'{entry.get("message") or ""}'
            )
            children = entry.get("replies") or []
            if children:
                out.append(self._render_replies(children, participants, depth + 1))
            out.append("</div>")
        return "".join(out)

    # -- 7. syllabus -------------------------------------------------------

    def export_syllabus(self) -> None:
        body = self.course.get("syllabus_body")
        if not body:
            LOG.debug("No syllabus body on this course.")
            self.stats["syllabus"] = 0
            return
        LOG.step("Syllabus")
        self.write_html("syllabus.html", "Syllabus", body)
        self.stats["syllabus"] = 1

    # -- 8. modules --------------------------------------------------------

    def export_modules(self) -> None:
        LOG.step("Modules")
        modules = self.collect(f"courses/{self.course_id}/modules", "modules",
                               include=["items"])
        blocks = []
        item_count = 0
        for module in modules:
            items = module.get("items")
            if items is None:
                items = self.collect(
                    f"courses/{self.course_id}/modules/{module.get('id')}/items",
                    f"items of module {module.get('id')}")
                module["items"] = items
            rows = []
            for item in items or []:
                item_count += 1
                rows.append(self._module_row(item))
            state = module.get("state") or ""
            head = f"<h2>{esc(module.get('name') or 'Module')}</h2>"
            if module.get("unlock_at"):
                head += f'<p class="meta"><span>Unlocks {esc(pretty_date(module["unlock_at"]))}</span></p>'
            body = f'<ul class="list">{"".join(rows)}</ul>' if rows else '<p class="meta">Empty module.</p>'
            blocks.append(head + body)
            LOG.info(f"{module.get('name')} - {len(items or [])} items")

        self._modules = modules
        self.stats["modules"] = len(modules)
        self.stats["module_items"] = item_count
        if blocks:
            self.write_html("modules/index.html", "Modules", "\n".join(blocks),
                            f"<span>{len(modules)} modules</span><span>{item_count} items</span>")
        self.write_json("modules/modules.json", modules)

    def _module_row(self, item: dict) -> str:
        kind = str(item.get("type") or "")
        title = item.get("title") or "(untitled)"
        indent = min(int(item.get("indent") or 0), 3)
        cls = f' class="indent-{indent}"' if indent else ""

        if kind == "SubHeader":
            return f'<li{cls}><strong>{esc(title)}</strong></li>'

        # Build the canonical Canvas URL for this item; rewrite_links() then
        # swaps it for the local copy. html_url alone points at
        # /modules/items/<id>, which we cannot map to anything on disk.
        base = f"{self.api.base_url}/courses/{self.course_id}"
        content_id = item.get("content_id")
        href = ""
        if kind == "File" and content_id:
            href = f"{base}/files/{content_id}"
        elif kind == "Page" and item.get("page_url"):
            href = f"{base}/pages/{item['page_url']}"
        elif kind == "Assignment" and content_id:
            href = f"{base}/assignments/{content_id}"
        elif kind == "Quiz" and content_id:
            href = f"{base}/quizzes/{content_id}"
        elif kind == "Discussion" and content_id:
            href = f"{base}/discussion_topics/{content_id}"
        elif kind in ("ExternalUrl", "ExternalTool"):
            href = item.get("external_url") or item.get("html_url") or ""
        if not href:
            href = item.get("html_url") or ""

        pretty_kind = re.sub(r"(?<!^)(?=[A-Z])", " ", kind)
        tag = f'<span class="tag">{esc(pretty_kind)}</span>'
        if href:
            return f'<li{cls}>{tag}<a href="{esc(href)}">{esc(title)}</a></li>'
        return f"<li{cls}>{tag}{esc(title)}</li>"

    # -- 8b. recovering what the listings hid ------------------------------

    def backfill_from_modules(self) -> None:
        """
        A course can hide its Pages or Quizzes tab from students. Canvas then
        answers 404 on /pages and /quizzes while every individual item stays
        perfectly readable - so a listing-only exporter silently drops most of
        the course. Walk the module tree and fetch anything still missing.
        """
        if not self._modules:
            return

        todo = {"Page": {}, "Quiz": {}, "Assignment": {}, "Discussion": {}}
        for module in self._modules:
            for item in module.get("items") or []:
                kind = item.get("type")
                if kind not in todo:
                    continue
                if kind == "Page":
                    key = str(item.get("page_url") or "")
                    if not key or key.lower() in self.page_paths:
                        continue
                else:
                    key = str(item.get("content_id") or "")
                    known = {"Quiz": self.quiz_paths,
                             "Assignment": self.assignment_paths,
                             "Discussion": self.topic_paths}[kind]
                    if not key or key in known:
                        continue
                todo[kind].setdefault(key, item.get("title") or key)

        # Only chase sections the user actually asked for.
        wanted = {"Page": "pages", "Quiz": "quizzes",
                  "Assignment": "assignments", "Discussion": "discussions"}
        for kind, section in wanted.items():
            if section not in self.sections:
                todo[kind] = {}

        total = sum(len(v) for v in todo.values())
        if not total:
            return
        LOG.step(f"Recovering {total} item(s) the course listings did not return")

        recovered, failed = 0, 0
        for slug, title in todo["Page"].items():
            if self.add_page(slug):
                recovered += 1
                LOG.info(f"page: {title}")
            else:
                failed += 1

        for cid, title in todo["Quiz"].items():
            try:
                quiz = self.api.get_json(f"courses/{self.course_id}/quizzes/{cid}")
            except CanvasError as exc:
                LOG.debug(f"quiz {cid} unreachable: {exc}")
                quiz = None
            if quiz and self.add_quiz(quiz):
                recovered += 1
                LOG.info(f"quiz: {title}")
            else:
                failed += 1

        for cid, title in todo["Assignment"].items():
            try:
                item = self.api.get_json(
                    f"courses/{self.course_id}/assignments/{cid}",
                    include=["submission"])
            except CanvasError as exc:
                LOG.debug(f"assignment {cid} unreachable: {exc}")
                item = None
            if item and self.add_assignment(item):
                recovered += 1
                LOG.info(f"assignment: {title}")
            else:
                failed += 1

        for cid, title in todo["Discussion"].items():
            try:
                topic = self.api.get_json(
                    f"courses/{self.course_id}/discussion_topics/{cid}")
            except CanvasError as exc:
                LOG.debug(f"discussion {cid} unreachable: {exc}")
                topic = None
            if topic and self.add_topic(topic):
                recovered += 1
                LOG.info(f"discussion: {title}")
            else:
                failed += 1

        self.stats["recovered_from_modules"] = recovered
        LOG.info(f"Recovered {recovered} of {total}.")
        if failed:
            self.note_warning(
                f"{failed} module item(s) could not be fetched individually "
                "either - your account most likely cannot see them.")

    def write_section_indexes(self) -> None:
        """Written last, so backfilled items appear in the section listings."""
        if self._page_index:
            rows = "".join(
                f'<li><a href="{esc(posixpath.relpath(rel, "pages"))}">{esc(title)}</a></li>'
                for title, rel in sorted(self._page_index, key=lambda r: r[0].lower())
            )
            self.write_html("pages/index.html", "Pages",
                            f'<ul class="list">{rows}</ul>',
                            f"<span>{len(self._page_index)} pages</span>")

        if self._assignment_index:
            rows = "".join(
                '<li><a href="{}">{}</a>{}</li>'.format(
                    esc(posixpath.relpath(rel, "assignments")), esc(title),
                    f'<div class="sub">Due {esc(pretty_date(due))}</div>' if due else "",
                )
                for title, rel, due in self._assignment_index
            )
            self.write_html("assignments/index.html", "Assignments",
                            f'<ul class="list">{rows}</ul>',
                            f"<span>{len(self._assignment_index)} assignments</span>")

        if self._quiz_index:
            rows = "".join(
                '<li><a href="{}">{}</a><div class="sub">{}</div></li>'.format(
                    esc(posixpath.relpath(rel, "quizzes")), esc(title),
                    f"{count} questions exported" if count else "description only",
                )
                for title, rel, count in self._quiz_index
            )
            self.write_html("quizzes/index.html", "Quizzes",
                            f'<ul class="list">{rows}</ul>',
                            f"<span>{len(self._quiz_index)} quizzes</span>")

        for folder, entries in self._topic_index.items():
            if not entries:
                continue
            label = "Announcements" if folder == "announcements" else "Discussions"
            rows = "".join(
                '<li><a href="{}">{}</a>{}</li>'.format(
                    esc(posixpath.relpath(rel, folder)), esc(title),
                    f'<div class="sub">{esc(pretty_date(when))}</div>' if when else "",
                )
                for title, rel, when in entries
            )
            self.write_html(f"{folder}/index.html", label,
                            f'<ul class="list">{rows}</ul>',
                            f"<span>{len(entries)} items</span>")

    # -- 9. offline link rewriting ----------------------------------------
    # The lookbehind keeps this from matching the tail of data-href / xlink:href.
    ATTR_RE = re.compile(
        r'(?<![\w:-])(?P<attr>href|src)=(?P<q>["\'])(?P<url>[^"\']*)(?P=q)',
        re.IGNORECASE)
    # Anchors carrying this class are left alone by rewrite_links().
    KEEP_MARKER = 'class="ce-original"'

    def _target_for(self, raw_url: str):
        """Map a Canvas URL to a path relative to the course root, or None."""
        if not raw_url or raw_url.startswith(("#", "mailto:", "data:", "javascript:")):
            return None
        split = urllib.parse.urlsplit(raw_url)
        if split.netloc and split.netloc.lower() != self.api.host:
            return None                      # genuinely external, leave alone
        path = split.path or ""
        frag = f"#{split.fragment}" if split.fragment else ""

        m = re.search(r"/files/(\d+)", path)
        if m and m.group(1) in self.file_paths:
            return self.file_paths[m.group(1)] + frag

        m = re.search(r"/courses/\d+/pages/([^/?#]+)", path)
        if m:
            slug = urllib.parse.unquote(m.group(1)).lower()
            if slug in self.page_paths:
                return self.page_paths[slug] + frag
            alt = slugify(slug)
            if alt in self.page_paths:
                return self.page_paths[alt] + frag

        m = re.search(r"/courses/\d+/assignments/(\d+)", path)
        if m and m.group(1) in self.assignment_paths:
            return self.assignment_paths[m.group(1)] + frag

        m = re.search(r"/courses/\d+/quizzes/(\d+)", path)
        if m and m.group(1) in self.quiz_paths:
            return self.quiz_paths[m.group(1)] + frag

        m = re.search(r"/courses/\d+/discussion_topics/(\d+)", path)
        if m and m.group(1) in self.topic_paths:
            return self.topic_paths[m.group(1)] + frag

        if re.search(r"/courses/\d+/assignments/syllabus", path):
            if os.path.exists(self.path("syllabus.html")):
                return "syllabus.html" + frag
        if re.search(r"/courses/\d+/modules/?$", path):
            if os.path.exists(self.path("modules", "index.html")):
                return "modules/index.html" + frag
        if re.search(r"/courses/\d+/assignments/?$", path):
            if os.path.exists(self.path("assignments", "index.html")):
                return "assignments/index.html" + frag
        if re.search(r"/courses/\d+/quizzes/?$", path):
            if os.path.exists(self.path("quizzes", "index.html")):
                return "quizzes/index.html" + frag
        if re.search(r"/courses/\d+/?$", path):
            return "index.html" + frag
        return None

    def rewrite_links(self) -> None:
        LOG.step("Rewriting links for offline use")
        rewritten = 0
        for rel in self.written_html:
            abs_path = self.path(rel)
            try:
                with open(abs_path, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            doc_dir = posixpath.dirname(rel)
            local_count = [0]

            def replace(match):
                target = self._target_for(match.group("url"))
                if not target:
                    return match.group(0)
                body, _, frag = target.partition("#")
                new = posixpath.relpath(body, doc_dir or ".")
                # Spaces, '#' and '%' are legal in filenames but not in a raw
                # href, so percent-encode the path we just built.
                new = urllib.parse.quote(new, safe="/-_.()~!*'")
                if frag:
                    new += "#" + frag
                local_count[0] += 1
                quote = match.group("q")
                return f'{match.group("attr")}={quote}{new}{quote}'

            # "View the original on Canvas" links must keep pointing at Canvas.
            text = text.replace(self.KEEP_MARKER + ' href="',
                                self.KEEP_MARKER + ' data-ce-href="')
            new_text = self.ATTR_RE.sub(replace, text)
            new_text = new_text.replace(self.KEEP_MARKER + ' data-ce-href="',
                                        self.KEEP_MARKER + ' href="')
            if local_count[0] or new_text != text:
                with open(abs_path, "w", encoding="utf-8") as fh:
                    fh.write(new_text)
                rewritten += local_count[0]
        LOG.info(f"{rewritten} link(s) now point at local copies.")
        self.stats["links_rewritten"] = rewritten

    # -- 10. index & manifest ---------------------------------------------

    def build_index(self) -> None:
        course = self.course
        name = course.get("name") or f"Course {self.course_id}"
        tiles = []

        def tile(href: str, number, label: str):
            if not os.path.exists(self.path(href)):
                return
            tiles.append(f'<a class="card" href="{esc(href)}">'
                         f'<span class="n">{esc(number)}</span>'
                         f'<span class="l">{esc(label)}</span></a>')

        tile("modules/index.html", self.stats.get("modules", 0), "Modules")
        tile("pages/index.html", self.stats.get("pages", 0), "Pages")
        tile("assignments/index.html", self.stats.get("assignments", 0), "Assignments")
        tile("quizzes/index.html", self.stats.get("quizzes", 0), "Quizzes")
        tile("announcements/index.html", self.stats.get("announcements", 0), "Announcements")
        tile("discussions/index.html", self.stats.get("discussions", 0), "Discussions")

        body = []
        bits = []
        if course.get("course_code"):
            bits.append(f"<span>{esc(course['course_code'])}</span>")
        term = (course.get("term") or {}).get("name")
        if term:
            bits.append(f"<span>{esc(term)}</span>")
        bits.append(f"<span>Exported {esc(pretty_date(datetime.now(timezone.utc).isoformat()))}</span>")

        if tiles:
            body.append(f'<div class="grid">{"".join(tiles)}</div>')

        if os.path.exists(self.path("syllabus.html")):
            body.append('<p><a href="syllabus.html">Read the syllabus &rarr;</a></p>')

        if self.file_paths:
            body.append(f"<h2>Files ({len(self.file_paths)})</h2>")
            body.append('<p class="meta">Everything downloadable sits in the '
                        "<code>files/</code> folder, in the same structure Canvas used.</p>")
            tree = {}
            for rel in sorted(self.file_paths.values()):
                folder = posixpath.dirname(rel)
                tree.setdefault(folder, []).append(rel)
            for folder in sorted(tree):
                label = folder[len("files/"):] if folder.startswith("files/") else ""
                if label == "_embedded":
                    label = "Embedded in pages and assignments"
                elif label == "_unsorted":
                    label = "Uncategorised"
                body.append(f"<h3>{esc(label or 'Course files')}</h3>")
                entries = "".join(
                    f'<li><a href="{esc(rel)}">{esc(posixpath.basename(rel))}</a>'
                    f'<div class="sub">{esc(human_size(self._size_of(rel)))}</div></li>'
                    for rel in sorted(tree[folder])
                )
                body.append(f'<ul class="list">{entries}</ul>')

        if self.warnings:
            body.append("<h2>Notes from the export</h2>")
            body.append('<ul class="list">' + "".join(
                f"<li><div class='sub'>{esc(w)}</div></li>" for w in self.warnings[:40]
            ) + "</ul>")

        body.append('<hr><p class="meta">Open <code>index.html</code> in any browser. '
                    "Links between pages, assignments and files work without an internet "
                    "connection; anything still pointing at Canvas is content the API "
                    "could not hand over.</p>")

        html = html_doc(name, "\n".join(body), 0, "".join(bits))
        # The index is at the root; drop the "back to index" chrome.
        html = html.replace(
            '<header class="ce-header"><a class="ce-home" href="index.html">'
            "&#8592; Course index</a></header>\n", "")
        with open(self.path("index.html"), "w", encoding="utf-8") as fh:
            fh.write(html)

        manifest = {
            "exporter": f"canvas_export.py {__version__}",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "canvas_url": self.api.base_url,
            "course": {
                "id": self.course_id,
                "name": course.get("name"),
                "code": course.get("course_code"),
                "term": term,
            },
            "sections": sorted(self.sections),
            "counts": self.stats,
            "bytes_downloaded": self.bytes_downloaded,
            "api_calls": self.api.api_calls,
            "warnings": self.warnings,
        }
        self.write_json("manifest.json", manifest)

    def _size_of(self, rel: str):
        try:
            return os.path.getsize(self.path(rel))
        except OSError:
            return 0


# --------------------------------------------------------------------------
# Zip packaging
# --------------------------------------------------------------------------

def make_zip(source_dir: str, zip_path: str) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(zip_path)) or ".", exist_ok=True)
    base = os.path.basename(source_dir.rstrip(os.sep))
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(source_dir):
            dirs.sort()
            for filename in sorted(files):
                if filename.endswith(".part"):
                    continue
                full = os.path.join(root, filename)
                rel = os.path.relpath(full, source_dir)
                zf.write(full, os.path.join(base, rel))
                count += 1
    return count


# --------------------------------------------------------------------------
# Credentials & CLI
# --------------------------------------------------------------------------

def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(url: str, token: str) -> None:
    data = {"url": url, "token": token}
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    LOG.info(f"Saved credentials to {CONFIG_PATH} (readable only by you).")


def normalise_url(raw: str) -> str:
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    split = urllib.parse.urlsplit(raw)
    return f"{split.scheme}://{split.netloc}"


def parse_course_ref(raw: str):
    """Accept '12345', a course URL, or 'all'."""
    raw = (raw or "").strip()
    if raw.lower() == "all":
        return "all", None
    match = re.search(r"/courses/(\d+)", raw)
    if match:
        return match.group(1), normalise_url(raw)
    if raw.isdigit():
        return raw, None
    raise SystemExit(f"Could not read a course ID out of '{raw}'. "
                     "Pass a number, a course URL, or 'all'.")


def resolve_sections(include, exclude):
    if include:
        raw = [s.strip().lower() for s in ",".join(include).split(",") if s.strip()]
        if "all" in raw:
            chosen = list(ALL_SECTIONS)
        else:
            chosen = []
            for name in raw:
                if name not in ALL_SECTIONS:
                    raise SystemExit(
                        f"Unknown section '{name}'. Choose from: {', '.join(ALL_SECTIONS)}, all")
                chosen.append(name)
    else:
        chosen = list(DEFAULT_SECTIONS)
    if exclude:
        drop = {s.strip().lower() for s in ",".join(exclude).split(",") if s.strip()}
        chosen = [s for s in chosen if s not in drop]
    if not chosen:
        raise SystemExit("Nothing left to export after --exclude.")
    return chosen


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="canvas_export.py",
        description="Export a Canvas LMS course (files, pages, modules, "
                    "assignments, quizzes) into a single browsable zip.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  canvas_export.py --url https://canvas.school.edu --token XXXX --save-config
  canvas_export.py --list-courses
  canvas_export.py --course 12345
  canvas_export.py --course https://canvas.school.edu/courses/12345 -o ~/Desktop
  canvas_export.py --course all --include all --jobs 8
  canvas_export.py --course 12345 --exclude files        # metadata only, fast

getting a token:
  Canvas -> Account -> Settings -> Approved Integrations -> "+ New Access Token".
  If your school hides that button, ask your IT help desk; without a token the
  API is closed. Treat the token like a password - anyone holding it is you.
""",
    )
    p.add_argument("--url", help="Canvas base URL, e.g. https://canvas.school.edu")
    p.add_argument("--token", help="Canvas API access token (or set CANVAS_TOKEN)")
    p.add_argument("--course", help="Course ID, course URL, or 'all'")
    p.add_argument("-o", "--out", default=".",
                   help="Output .zip path, or a directory to drop it in (default: here)")
    p.add_argument("--include", action="append", metavar="LIST",
                   help=f"Comma list of sections to export. Options: "
                        f"{', '.join(ALL_SECTIONS)}, all. "
                        f"Default: {', '.join(DEFAULT_SECTIONS)}")
    p.add_argument("--exclude", action="append", metavar="LIST",
                   help="Comma list of sections to leave out")
    p.add_argument("--list-courses", action="store_true",
                   help="Print your courses with their IDs and exit")
    p.add_argument("--jobs", type=int, default=5,
                   help="Parallel file downloads (default: 5)")
    p.add_argument("--max-file-size", type=float, metavar="MB",
                   help="Skip any single file larger than this many MB")
    p.add_argument("--work-dir", metavar="DIR",
                   help="Keep the unzipped export here and reuse it on the next "
                        "run (resumes interrupted downloads)")
    p.add_argument("--no-zip", action="store_true",
                   help="Leave the folder on disk instead of zipping it "
                        "(implies --work-dir if you also pass one)")
    p.add_argument("--timeout", type=int, default=60, help="HTTP timeout in seconds")
    p.add_argument("--save-config", action="store_true",
                   help=f"Remember --url and --token in {CONFIG_PATH}")
    p.add_argument("--insecure", action="store_true",
                   help="Skip TLS verification (only for broken campus proxies)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--version", action="version", version=f"canvas_export {__version__}")
    return p


def pick_credentials(args):
    config = load_config()
    url = normalise_url(args.url or os.environ.get("CANVAS_URL") or config.get("url") or "")
    token = (args.token or os.environ.get("CANVAS_TOKEN")
             or config.get("token") or "").strip()

    if not url:
        entered = input("Canvas URL (e.g. https://canvas.school.edu): ").strip()
        url = normalise_url(entered)
    if not url:
        raise SystemExit("A Canvas URL is required.")
    if not token:
        sys.stderr.write(
            "\nNeed an access token. In Canvas: Account -> Settings -> "
            "'+ New Access Token'.\n")
        token = getpass.getpass("Paste your Canvas access token (hidden): ").strip()
    if not token:
        raise SystemExit("An access token is required.")
    return url, token


def list_courses(client: CanvasClient) -> None:
    courses = list(client.paginate(
        "courses", enrollment_state="active", state=["available", "completed"],
        include=["term"]))
    if not courses:
        print("No active courses found for this token.")
        return
    width = max(len(str(c.get("id"))) for c in courses)
    print(f"\n{'ID'.ljust(width)}  {'CODE'.ljust(16)}  TERM / NAME")
    print("-" * 78)
    for course in sorted(courses, key=lambda c: str(c.get("name") or "")):
        term = (course.get("term") or {}).get("name") or ""
        code = str(course.get("course_code") or "")[:16]
        print(f"{str(course.get('id')).ljust(width)}  {code.ljust(16)}  "
              f"{course.get('name')}" + (f"  [{term}]" if term else ""))
    print(f"\n{len(courses)} course(s). Export one with:  "
          f"--course <ID>\n")


def resolve_out_path(out_arg: str, course: dict) -> str:
    code = safe_name(course.get("course_code") or course.get("name")
                     or f"course-{course.get('id')}", max_len=60)
    stamp = datetime.now().strftime("%Y-%m-%d")
    default_name = f"{code} - canvas export {stamp}.zip"
    out_arg = os.path.expanduser(out_arg or ".")
    if out_arg.lower().endswith(".zip"):
        return os.path.abspath(out_arg)
    return os.path.abspath(os.path.join(out_arg, default_name))


def export_one(client: CanvasClient, course_id: str, args, sections) -> dict:
    course = client.get_json(
        f"courses/{course_id}",
        include=["syllabus_body", "term", "course_image", "public_description"])
    if not course or not course.get("id"):
        raise CanvasError(f"Course {course_id} returned nothing usable.")

    name = course.get("name") or f"Course {course_id}"
    LOG.step(f"{name}  (id {course_id})")

    folder_name = safe_name(f"{course.get('course_code') or name}", max_len=80)
    if args.work_dir:
        base_dir = os.path.abspath(os.path.expanduser(args.work_dir))
        os.makedirs(base_dir, exist_ok=True)
        temp_dir = None
    else:
        temp_dir = tempfile.mkdtemp(prefix="canvas-export-")
        base_dir = temp_dir
    root = os.path.join(base_dir, folder_name)

    try:
        exporter = CourseExporter(
            client, course, root, sections,
            jobs=args.jobs,
            max_file_bytes=int(args.max_file_size * 1024 * 1024) if args.max_file_size else None,
        )
        stats = exporter.export()

        result = {"course": name, "id": course_id, "stats": stats,
                  "bytes": exporter.bytes_downloaded,
                  "warnings": len(exporter.warnings)}

        if args.no_zip:
            if temp_dir:
                final = os.path.abspath(os.path.join(
                    os.path.expanduser(args.out or "."), folder_name))
                if os.path.exists(final):
                    shutil.rmtree(final)
                shutil.move(root, final)
                root = final
                temp_dir = None
            LOG.step(f"Done - folder: {root}")
            result["output"] = root
        else:
            zip_path = resolve_out_path(args.out, course)
            LOG.step("Zipping")
            entries = make_zip(root, zip_path)
            LOG.info(f"{entries} entries, {human_size(os.path.getsize(zip_path))}")
            LOG.step(f"Done - {zip_path}")
            result["output"] = zip_path
        return result
    finally:
        if temp_dir and os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    global LOG
    LOG = Log(verbose=args.verbose, quiet=args.quiet)

    # A course URL can supply the Canvas host too.
    course_id = None
    if args.course:
        course_id, url_from_course = parse_course_ref(args.course)
        if url_from_course and not args.url:
            args.url = url_from_course

    url, token = pick_credentials(args)
    if args.save_config:
        save_config(url, token)

    client = CanvasClient(url, token, timeout=args.timeout,
                          verify_tls=not args.insecure)

    # Verify the token before doing anything slow.
    try:
        me = client.get_json("users/self")
        LOG.info(f"Signed in as {me.get('name') or me.get('id')} on {url}")
    except CanvasError as exc:
        LOG.error(str(exc))
        return 2

    if args.list_courses:
        list_courses(client)
        return 0

    if not course_id:
        LOG.error("No course given. Use --course <ID|URL|all>, "
                  "or --list-courses to see what you have.")
        return 2

    sections = resolve_sections(args.include, args.exclude)
    LOG.info("Exporting: " + ", ".join(sections))

    if course_id == "all":
        courses = list(client.paginate("courses", enrollment_state="active",
                                       state=["available"]))
        ids = [str(c.get("id")) for c in courses if c.get("id")]
        LOG.info(f"{len(ids)} active course(s) queued.")
    else:
        ids = [course_id]

    results, failures = [], []
    for cid in ids:
        try:
            results.append(export_one(client, cid, args, sections))
        except CanvasError as exc:
            LOG.error(f"Course {cid}: {exc}")
            failures.append(cid)
        except KeyboardInterrupt:
            LOG.error("Interrupted.")
            return 130

    if not args.quiet:
        print("\n" + "=" * 62)
        for res in results:
            counts = ", ".join(f"{v} {k}" for k, v in sorted(res["stats"].items())
                               if isinstance(v, int) and v and k != "links_rewritten")
            print(f"{res['course']}\n  {counts or 'nothing found'}"
                  f"\n  {human_size(res['bytes'])} downloaded"
                  + (f", {res['warnings']} note(s)" if res["warnings"] else "")
                  + f"\n  -> {res['output']}")
        if failures:
            print(f"\nFailed: {', '.join(failures)}")
        print("=" * 62)
    return 1 if failures and not results else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        sys.exit(130)
    except SystemExit:
        raise
    except CanvasError as _exc:
        sys.stderr.write(f"ERROR: {_exc}\n")
        sys.exit(2)
