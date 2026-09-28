# canvas_export

Pull everything out of a Canvas LMS course and get a single `.zip` you can keep
forever — files, pages, modules, assignments and quizzes, rebuilt as a little
offline website that still works when your enrolment ends.

One file, no dependencies, Python 3.8+.

---

## Setup (2 minutes)

**1. Get an access token.** In Canvas: **Account → Settings → Approved
Integrations → "+ New Access Token"**. Give it a purpose, leave the expiry
blank, click Generate, and copy the token — Canvas shows it exactly once.

> If your school has hidden that button, personal tokens are disabled and this
> tool can't get in. Ask your IT help desk for API access.

**2. Save your credentials** (optional, but it saves retyping):

```bash
python3 canvas_export.py --url https://canvas.yourschool.edu --token PASTE_TOKEN_HERE --save-config
```

That writes `~/.canvas_export.json` with permissions `600`. You can skip this
and use `--url` / `--token` every time, or set `CANVAS_URL` and `CANVAS_TOKEN`
in your environment instead.

---

## Use it

```bash
# What am I enrolled in?
python3 canvas_export.py --list-courses

# Export one course to a zip in the current folder
python3 canvas_export.py --course 12345

# Paste the course URL straight from your browser instead of the ID
python3 canvas_export.py --course https://canvas.yourschool.edu/courses/12345

# Everything you're enrolled in, into ~/Desktop, 8 downloads at a time
python3 canvas_export.py --course all --include all --jobs 8 -o ~/Desktop
```

The zip is named like `COMP3300 - canvas export 2026-09-19.zip`.

---

## What's in the zip

```
COMP3300/
├── index.html                 ← open this
├── modules/index.html         the module list, in course order, with links
├── pages/                     every wiki page as standalone HTML
├── assignments/001-…/         description, due date, rubric, your grade, JSON
├── quizzes/001-…/             description + questions and answers where allowed
├── files/
│   ├── Week 1/…               the real Canvas folder structure, preserved
│   └── _embedded/             files linked from pages but hidden from Files
├── course.json                raw course metadata
└── manifest.json              what was exported, when, and anything skipped
```

Open `index.html` in any browser. **Links between pages, assignments, quizzes
and files are rewritten to point at the local copies**, so the whole thing
browses offline. Anything still pointing at Canvas is content the API wouldn't
hand over — those are listed in `manifest.json`.

---

## Options

| Flag | What it does |
|---|---|
| `--course ID\|URL\|all` | What to export |
| `-o PATH` | Output `.zip` path, or a folder to drop it in |
| `--include LIST` | `files,pages,modules,assignments,quizzes,syllabus,announcements,discussions` or `all`. Default is everything except syllabus/announcements/discussions |
| `--exclude LIST` | Drop sections, e.g. `--exclude files` for a fast metadata-only run |
| `--jobs N` | Parallel downloads (default 5) |
| `--max-file-size MB` | Skip anything bigger — useful for courses full of lecture videos |
| `--work-dir DIR` | Keep the unzipped export and **resume** next time instead of re-downloading |
| `--no-zip` | Leave the folder on disk |
| `--list-courses` | Print your courses with IDs |
| `--save-config` | Remember URL + token |
| `-v` / `-q` | Verbose / quiet |

Run `python3 canvas_export.py --help` for the full list.

---

## Things it handles that will otherwise bite you

- **Pagination.** Follows Canvas `Link` headers, so you get all 400 files, not the first 100.
- **Rate limiting.** Canvas answers a flood with `403 Rate Limit Exceeded`; this backs off and retries instead of dying.
- **The S3 redirect trap.** Canvas file URLs redirect to a CDN that rejects requests carrying both a signed URL and an `Authorization` header. The header is dropped on cross-host redirects.
- **Hidden tabs.** A course can hide its Pages or Quizzes tab from students. Canvas then answers `404` on `/pages` and `/quizzes` while every individual item stays readable, so a listing-only exporter silently drops most of the course while reporting success. After the listings run, the module tree is walked and anything missing is fetched item by item.
- **Hidden files.** Files embedded in a page but absent from the Files tab (or a course with Files switched off entirely) get found by scanning the exported HTML and fetched separately into `files/_embedded/`.
- **Teacher-only endpoints.** Quiz questions are usually invisible to students. You get the description plus a note, not a crash.
- **New Quizzes.** Canvas exposes no API for New Quizzes questions. Those appear as assignments with a clear note saying so — nothing can be done about it from outside Canvas.
- **Filenames.** Colons, slashes, emoji, 300-character titles, Windows reserved names like `CON.txt`, and duplicate names in one folder are all normalised so the zip opens cleanly on Windows, macOS and Linux.
- **Interrupted runs.** With `--work-dir`, finished downloads are skipped on the next run.

---

## Limits, honestly

- **Only what your account can see.** The API enforces the same permissions as the web UI. Unpublished content, locked files and other people's submissions stay invisible.
- **Panopto / Echo360 / Kaltura lecture recordings** are embedded from other systems, not stored in Canvas. They can't come through this. The embed links are preserved.
- **New Quizzes** questions are not available over any API.
- Big media-heavy courses take a while — try `--max-file-size 100` first.

Check your institution's acceptable-use policy before bulk-downloading course
material, and keep the export to yourself: course content is usually the
instructor's copyright, and your token is as good as your password.

MIT licensed.
