#!/usr/bin/env python3
"""
Cookbook — a small, dependency-free (stdlib only) HTTP server for browsing
and cooking from recipes stored as plain Markdown files in a folder on
disk. Same philosophy as Vehicle Maintenance Record: one file, plain
Python, nothing to pip install, nothing to break on an update.

Recipes are read straight off disk on every request — there's no database
and no build step, so a file dropped into the Cooking folder from any
device (upload, a sync client, scp, whatever) just shows up the next time
the page loads.

The folder defaults to ~/Documents/Cooking; override it with the
COOKBOOK_RECIPES_DIR environment variable (set in the systemd unit, not
committed here) if your actual data lives somewhere else. Recipe photos
live in a "Cooking Photos" subfolder nested inside that same folder
(default ~/Documents/Cooking/Cooking Photos, override with
COOKBOOK_PHOTOS_DIR) rather than mixed in alongside the .md files themselves.

Run:  python3 server.py
Serves on 0.0.0.0:8092, protected by HTTP Basic Auth.
"""
import base64
import email
import html
import json
import os
import re
import struct
import unicodedata
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, quote, unquote

PORT = 8092
RECIPES_DIR = os.environ.get(
    "COOKBOOK_RECIPES_DIR",
    os.path.join(os.path.expanduser("~"), "Documents", "Cooking"),
)

# Real credentials live in local_secrets.py, gitignored — see
# local_secrets.py.example. Falls back to an obviously-placeholder password
# so a fresh checkout without that file still runs, just isn't the real
# server (and makes it obvious the file needs to be created).
try:
    from local_secrets import AUTH_USER, AUTH_PASS
except ImportError:
    AUTH_USER = "admin"
    AUTH_PASS = "changeme"


# --- Recipe parsing ---------------------------------------------------------
# Recipe files come from two places: pasted/scraped from recipe sites (messy,
# inconsistent heading names — "Instructions", "How to Make It", "Preparation
# Instructions", sometimes ingredients split across several headed groups
# like "Sauce"/"Beef"/"Noodles") and ones typed through this app's own "New
# Recipe" form (always the same clean shape). The parser below is heuristic
# on purpose so it copes with both instead of demanding one exact format.

STEP_HEADING_RE = re.compile(r"^step\s*\d+\b[:.]?\s*", re.IGNORECASE)
INGREDIENT_WORD_RE = re.compile(r"ingredient", re.IGNORECASE)
INSTRUCTION_WORD_RE = re.compile(r"instruction|direction|method|preparation|how to make", re.IGNORECASE)
NOTES_WORD_RE = re.compile(r"notes?\b|tips?\b|nutrition|faq", re.IGNORECASE)
BULLET_RE = re.compile(r"^[-*]\s+(.*)")
NUMBERED_RE = re.compile(r"^\d+[.)]\s+(.*)")


def render_inline(text):
    """Escape HTML, then turn **bold** and *italic* into real tags. Good
    enough for the plain-prose recipe text this app ever produces or copies
    from a recipe site — not a general Markdown engine."""
    text = html.escape(text.strip())
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", text)
    return text


def parse_md_table(lines):
    """A recipe pasted from some sites lists ingredients as a GFM table
    (| Amount | Ingredient |) instead of a bullet list. Returns a list of
    data rows (cell lists), header and the --- separator row dropped."""
    rows = []
    for line in lines:
        line = line.strip()
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue  # separator row
        rows.append(cells)
    return rows[1:] if rows else rows  # drop the header row


def render_block(lines):
    """Renders a chunk of markdown-ish lines (bullets, numbered items, plain
    paragraphs) to HTML. Used for intro text and any section that isn't
    treated as ingredients/steps (e.g. Notes, Nutrition, Tips)."""
    html_parts = []
    para = []
    list_items = []
    list_tag = None

    def flush_para():
        if para:
            html_parts.append(f"<p>{render_inline(' '.join(para))}</p>")
            para.clear()

    def flush_list():
        nonlocal list_tag
        if list_items:
            items = "".join(f"<li>{render_inline(i)}</li>" for i in list_items)
            html_parts.append(f"<{list_tag}>{items}</{list_tag}>")
            list_items.clear()
        list_tag = None

    for line in lines:
        line = line.rstrip()
        if not line.strip():
            flush_para()
            flush_list()
            continue
        bm = BULLET_RE.match(line)
        nm = NUMBERED_RE.match(line)
        if bm:
            flush_para()
            if list_tag != "ul":
                flush_list()
                list_tag = "ul"
            list_items.append(bm.group(1))
        elif nm:
            flush_para()
            if list_tag != "ol":
                flush_list()
                list_tag = "ol"
            list_items.append(nm.group(1))
        else:
            flush_list()
            para.append(line.strip())
    flush_para()
    flush_list()
    return "".join(html_parts)


def split_numbered_steps(lines):
    """Splits a block of lines into one chunk per leading '1.'/'2)' marker,
    each chunk running until the next marker."""
    steps, current = [], []
    for line in lines:
        if NUMBERED_RE.match(line):
            if current:
                steps.append(current)
            current = [NUMBERED_RE.match(line).group(1)]
        elif current:
            current.append(line)
    if current:
        steps.append(current)
    return steps


def split_sections(text):
    """Shared front end for parse_recipe, extract_editable_summary, and
    extract_raw_steps: pulls the title (H1) off, then splits everything
    else into (heading, body_lines) chunks on '## ' headings — everything
    before the first one is the intro (byline, description, meta line)."""
    lines = text.replace("\r\n", "\n").split("\n")

    title = "Untitled Recipe"
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        lines = lines[1:]

    sections = []
    intro_lines = []
    current_heading = None
    current_body = []
    for line in lines:
        if line.startswith("## "):
            if current_heading is None:
                intro_lines = current_body
            else:
                sections.append((current_heading, current_body))
            current_heading = line[3:].strip()
            current_body = []
        else:
            current_body.append(line)
    if current_heading is None:
        intro_lines = current_body
    else:
        sections.append((current_heading, current_body))

    return title, intro_lines, sections


def is_step_section(heading, body):
    """True for any section whose content becomes cooking-mode step cards
    — a '## Step N: ...' heading, an '## Instructions'-ish heading, or a
    section that's just mostly a numbered list even under some other name.
    Shared by the parser and the edit form, so both agree on what counts
    as a step (and therefore what gets excluded from the editable summary
    text so editing doesn't duplicate or eat the Instructions section)."""
    if STEP_HEADING_RE.match(heading):
        return True
    non_empty = [l for l in body if l.strip()]
    numbered_ratio = (sum(1 for l in non_empty if NUMBERED_RE.match(l)) / len(non_empty)) if non_empty else 0
    return bool(INSTRUCTION_WORD_RE.search(heading) or (numbered_ratio > 0.5 and not INGREDIENT_WORD_RE.search(heading)))


_META_LABEL_RE = re.compile(
    r"\*\*(Servings|Prep(?:\s*Time)?|Cook(?:\s*Time)?|Total(?:\s*Time)?|Difficulty):?\*\*\s*([^\n*|]+)",
    re.IGNORECASE,
)


def _parse_minutes(text):
    """Best-effort minutes out of a free-text duration like '35 min',
    'About 2 hours', or '1 hr 20 min'. Used only for the time-bucket
    filter, so an unparseable value (None) just drops out of every
    bucket rather than raising."""
    if not text:
        return None
    t = text.lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\s*(?:(\d+)\s*(?:minutes?|mins?))?", t)
    if m:
        hours = float(m.group(1))
        mins = int(m.group(2)) if m.group(2) else 0
        return round(hours * 60 + mins)
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", t)]
    if not nums:
        return None
    return round(sum(nums) / len(nums))


def extract_recipe_meta(intro_text):
    """Servings/Total Time/Difficulty pulled out of the intro's free-text
    '**Label:** value' fields — scraped recipes carry these in wildly
    different shapes (Total vs Total Time, Prep vs Prep Time, a byline
    like '30 minutes | 4 servings', or nothing at all), so every field
    here is optional. Total Time falls back to Prep + Cook when a recipe
    gives those two but not a total."""
    found = {}
    for label, value in _META_LABEL_RE.findall(intro_text):
        key = label.strip().lower()
        if key.startswith("prep"):
            key = "prep_time"
        elif key.startswith("cook"):
            key = "cook_time"
        elif key.startswith("total"):
            key = "total_time"
        elif key.startswith("serving"):
            key = "servings"
        elif key.startswith("difficulty"):
            key = "difficulty"
        found.setdefault(key, value.strip().rstrip(".").strip())

    if "total_time" not in found and "prep_time" in found and "cook_time" in found:
        prep_min = _parse_minutes(found["prep_time"])
        cook_min = _parse_minutes(found["cook_time"])
        if prep_min is not None and cook_min is not None:
            found["total_time"] = f"{prep_min + cook_min} min"

    total_time = found.get("total_time")
    return {
        "servings": found.get("servings"),
        "prep_time": found.get("prep_time"),
        "cook_time": found.get("cook_time"),
        "total_time": total_time,
        "total_time_minutes": _parse_minutes(total_time),
        "difficulty": found.get("difficulty"),
    }


# Prep/Cook/Total Time and Difficulty get their own Edit-form fields, so a
# saved recipe's inline "**Prep Time:** ..." fragments need to disappear
# from the Summary text on edit (else they'd show up twice — once in the
# dedicated fields, once still sitting in the free-text Summary below).
# Servings isn't included: there's no dedicated field for it, so it's left
# alone wherever it appears in the free text.
_EDITABLE_META_LABELS = ["Prep(?:\\s*Time)?", "Cook(?:\\s*Time)?", "Total(?:\\s*Time)?", "Difficulty"]
_STRIP_META_RE = re.compile(
    r"\*\*(?:" + "|".join(_EDITABLE_META_LABELS) + r"):?\*\*\s*[^\n*|]+",
    re.IGNORECASE,
)


def _strip_meta_fields(text):
    text = _STRIP_META_RE.sub("", text)
    cleaned_lines = []
    for line in text.split("\n"):
        parts = [p.strip() for p in line.split("|")]
        parts = [p for p in parts if p]
        cleaned_lines.append(" | ".join(parts))
    result = "\n".join(cleaned_lines)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def _format_minutes(total_minutes):
    if total_minutes < 60:
        return f"{total_minutes} min"
    hours, mins = divmod(total_minutes, 60)
    return f"{hours} hr {mins} min" if mins else f"{hours} hr"


def _build_meta_line(prep_time, cook_time, difficulty):
    # No Total Time input — it's always derived from Prep + Cook, never
    # typed separately, so the two numbers can't drift out of sync.
    bits = []
    if prep_time.strip():
        bits.append(f"**Prep Time:** {prep_time.strip()}")
    if cook_time.strip():
        bits.append(f"**Cook Time:** {cook_time.strip()}")
    prep_min = _parse_minutes(prep_time)
    cook_min = _parse_minutes(cook_time)
    if prep_min is not None and cook_min is not None:
        bits.append(f"**Total Time:** {_format_minutes(prep_min + cook_min)}")
    if difficulty.strip():
        bits.append(f"**Difficulty:** {difficulty.strip()}")
    return " | ".join(bits)


def parse_recipe(text):
    title, intro_lines, sections = split_sections(text)

    ingredient_groups = []  # [(group_label_or_None, [item, ...]), ...]
    steps = []              # [{"label": str_or_None, "html": str}, ...]
    notes_sections = []     # [(heading, html), ...]
    seen_steps_section = False

    for heading, body in sections:
        non_empty = [l for l in body if l.strip()]
        bullet_ratio = (sum(1 for l in non_empty if BULLET_RE.match(l)) / len(non_empty)) if non_empty else 0

        if STEP_HEADING_RE.match(heading):
            label = STEP_HEADING_RE.sub("", heading).strip() or None
            steps.append({"label": label, "html": render_block(body)})
            seen_steps_section = True
            continue

        if is_step_section(heading, body):
            for chunk in split_numbered_steps(body):
                steps.append({"label": None, "html": render_block(chunk)})
            seen_steps_section = True
            continue

        if INGREDIENT_WORD_RE.search(heading) or (
            bullet_ratio > 0.5 and not seen_steps_section and not NOTES_WORD_RE.search(heading)
        ):
            if any("|" in l for l in non_empty):
                items = [" ".join(c for c in row if c) for row in parse_md_table(body)]
                items = [i for i in items if i]
            else:
                items = [BULLET_RE.match(l).group(1) for l in non_empty if BULLET_RE.match(l)]
            label = None if INGREDIENT_WORD_RE.search(heading) else heading
            if items:
                ingredient_groups.append((label, items))
            continue

        notes_sections.append((heading, render_block(body)))

    return {
        "title": title,
        "intro_html": render_block(intro_lines),
        "ingredient_groups": ingredient_groups,
        "steps": steps,
        "notes_sections": notes_sections,
        "meta": extract_recipe_meta("\n".join(intro_lines)),
    }


def extract_editable_summary(text):
    """Everything from the file EXCEPT the title, the Instructions/Step
    section(s), and — when there's exactly one plain '## Ingredients'
    heading (no grouping) — that section, since the Edit form's structured
    Ingredients list-builder owns it instead. A recipe with ingredients
    split across several named sections (Sauce/Beef/Noodles, ...) has no
    single heading the structured box could cleanly own, so those stay
    here untouched, verbatim, exactly as before — the Edit form's Summary
    box was always the fallback for anything it doesn't have a dedicated
    field for."""
    _title, intro_lines, sections = split_sections(text)
    parts = []
    intro = "\n".join(intro_lines).strip()
    if intro:
        parts.append(intro)

    ingredient_heading_count = 0
    simple_ingredient_heading = None
    seen_steps_section = False
    for heading, body in sections:
        if is_step_section(heading, body):
            seen_steps_section = True
            continue
        non_empty = [l for l in body if l.strip()]
        bullet_ratio = (sum(1 for l in non_empty if BULLET_RE.match(l)) / len(non_empty)) if non_empty else 0
        is_ingredient = INGREDIENT_WORD_RE.search(heading) or (
            bullet_ratio > 0.5 and not seen_steps_section and not NOTES_WORD_RE.search(heading)
        )
        if is_ingredient:
            ingredient_heading_count += 1
            if INGREDIENT_WORD_RE.search(heading):
                simple_ingredient_heading = heading
    skip_heading = simple_ingredient_heading if ingredient_heading_count == 1 else None

    for heading, body in sections:
        if is_step_section(heading, body):
            continue
        if heading == skip_heading:
            continue
        body_text = "\n".join(body).strip()
        parts.append(f"## {heading}\n\n{body_text}" if body_text else f"## {heading}")
    return _strip_meta_fields("\n\n".join(parts))


def extract_raw_steps(text):
    """The recipe's steps as plain text lines, pre-rendering — one line per
    step, matching the New/Edit form's 'one step per line' textarea
    convention, so re-opening a recipe for editing shows its steps back the
    same way they'd be typed in fresh."""
    _title, _intro_lines, sections = split_sections(text)
    lines = []
    for heading, body in sections:
        if not is_step_section(heading, body):
            continue
        if STEP_HEADING_RE.match(heading):
            label = STEP_HEADING_RE.sub("", heading).strip()
            body_text = " ".join(l.strip() for l in body if l.strip())
            lines.append(f"{label}: {body_text}" if label else body_text)
        else:
            for chunk in split_numbered_steps(body):
                lines.append(" ".join(l.strip() for l in chunk if l.strip()))
    return lines


def slugify(title):
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_")
    return slug or "recipe"


TRASH_DIR = os.path.join(RECIPES_DIR, ".trash")


def _valid_slug(slug):
    return bool(slug) and slug == os.path.basename(slug) and "/" not in slug and not slug.startswith(".")


def _list_md_files(directory):
    if not os.path.isdir(directory):
        return []
    names = []
    for name in os.listdir(directory):
        if not name.lower().endswith(".md") or name.startswith("."):
            continue
        if os.path.isfile(os.path.join(directory, name)):
            names.append(name)
    return names


def load_recipes():
    """Every top-level .md file in RECIPES_DIR, parsed and sorted by title.
    Subfolders (an "Original sources" archive, or .trash — see below) are
    skipped on purpose; only files directly in Cooking/ are live recipes."""
    recipes = []
    for name in _list_md_files(RECIPES_DIR):
        path = os.path.join(RECIPES_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        parsed = parse_recipe(text)
        parsed["slug"] = name[:-3]
        recipes.append(parsed)
    recipes.sort(key=lambda r: r["title"].lower())
    return recipes


def load_recipe(slug):
    if not _valid_slug(slug):
        return None
    path = os.path.join(RECIPES_DIR, slug + ".md")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    parsed = parse_recipe(text)
    parsed["slug"] = slug
    return parsed


def load_recipe_raw(slug):
    """Raw file text, for the Edit form (which needs the untouched markdown
    to pre-fill Summary — see extract_editable_summary). Returns None if the
    slug doesn't resolve to a real file in Cooking/."""
    if not _valid_slug(slug):
        return None
    path = os.path.join(RECIPES_DIR, slug + ".md")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _build_markdown(title, summary, steps, ingredients="", meta_line=""):
    parts = [f"# {title}\n"]
    if meta_line.strip():
        parts.append(f"\n{meta_line.strip()}\n")
    if summary.strip():
        parts.append(f"\n{summary.strip()}\n")
    ing_lines = [l.strip() for l in ingredients.splitlines() if l.strip()]
    if ing_lines:
        parts.append("\n## Ingredients\n\n" + "\n".join(f"- {l}" for l in ing_lines) + "\n")
    step_lines = [l.strip() for l in steps.splitlines() if l.strip()]
    if step_lines:
        parts.append("\n## Instructions\n\n" + "\n".join(f"{i}. {l}" for i, l in enumerate(step_lines, 1)) + "\n")
    return "".join(parts)


def save_recipe(title, summary, steps, ingredients="", prep_time="", cook_time="", difficulty=""):
    """Creates a new recipe file, picking a free filename off the title."""
    slug = slugify(title)
    path = os.path.join(RECIPES_DIR, slug + ".md")
    suffix = 2
    while os.path.exists(path):
        path = os.path.join(RECIPES_DIR, f"{slug}_{suffix}.md")
        suffix += 1
    os.makedirs(RECIPES_DIR, exist_ok=True)
    meta_line = _build_meta_line(prep_time, cook_time, difficulty)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_build_markdown(title, summary, steps, ingredients, meta_line))
    return os.path.basename(path)[:-3]


def update_recipe(slug, title, summary, steps, ingredients="", prep_time="", cook_time="", difficulty=""):
    """Overwrites an existing recipe in place — the filename/slug (and so
    its URL) never changes on edit, even if the title does, so links and
    bookmarks keep working."""
    if not _valid_slug(slug):
        return False
    path = os.path.join(RECIPES_DIR, slug + ".md")
    if not os.path.isfile(path):
        return False
    meta_line = _build_meta_line(prep_time, cook_time, difficulty)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_build_markdown(title, summary, steps, ingredients, meta_line))
    return True


# --- Trash: soft delete ------------------------------------------------------
# There's no database row to flag deleted_at on, so "soft delete" here just
# means moving the file into Cooking/.trash — load_recipes() already skips
# anything not a direct child of Cooking/, so a trashed file simply stops
# being a recipe until it's moved back.

def _unique_path(directory, slug):
    path = os.path.join(directory, slug + ".md")
    suffix = 2
    while os.path.exists(path):
        path = os.path.join(directory, f"{slug}_{suffix}.md")
        suffix += 1
    return path


def load_trash():
    recipes = []
    for name in _list_md_files(TRASH_DIR):
        path = os.path.join(TRASH_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        parsed = parse_recipe(text)
        parsed["slug"] = name[:-3]
        recipes.append(parsed)
    recipes.sort(key=lambda r: r["title"].lower())
    return recipes


def trash_recipe(slug):
    if not _valid_slug(slug):
        return False
    src = os.path.join(RECIPES_DIR, slug + ".md")
    if not os.path.isfile(src):
        return False
    os.makedirs(TRASH_DIR, exist_ok=True)
    os.rename(src, _unique_path(TRASH_DIR, slug))
    return True


def restore_recipe(slug):
    if not _valid_slug(slug):
        return False
    src = os.path.join(TRASH_DIR, slug + ".md")
    if not os.path.isfile(src):
        return False
    os.rename(src, _unique_path(RECIPES_DIR, slug))
    return True


def delete_forever(slug):
    if not _valid_slug(slug):
        return False
    path = os.path.join(TRASH_DIR, slug + ".md")
    if not os.path.isfile(path):
        return False
    os.remove(path)
    delete_all_recipe_photos(slug)
    return True


def empty_trash():
    for name in _list_md_files(TRASH_DIR):
        os.remove(os.path.join(TRASH_DIR, name))


# --- Recipe photos -------------------------------------------------------
# A recipe can carry a whole gallery of photos — "what it looked like this
# time I cooked it" — not just one. They live in a "Cooking Photos"
# subfolder nested inside RECIPES_DIR (PHOTOS_DIR) — one subfolder per
# recipe under that, named by slug. load_recipes() only picks up top-level
# .md files (see _list_md_files), so this nested folder is automatically
# invisible to it — no risk of it being mistaken for a recipe.
#
# Each file is named after its own timestamp — the photo's EXIF
# DateTimeOriginal when there is one, otherwise the moment it was uploaded
# — so a folder listing sorts and reads sensibly on its own even outside
# this app. That timestamp is re-derived live from the file itself at
# listing time (EXIF if present, else mtime) rather than trusted from the
# filename, so a photo dropped in or renamed by hand from any device still
# labels correctly, the same "just read what's on disk" philosophy as
# recipes themselves.

PHOTO_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "gif")
PHOTO_CONTENT_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif",
}
PHOTOS_DIR = os.environ.get(
    "COOKBOOK_PHOTOS_DIR",
    os.path.join(RECIPES_DIR, "Cooking Photos"),
)


def _recipe_photos_dir(slug):
    return os.path.join(PHOTOS_DIR, slug)


def list_recipe_photos(slug):
    """[{"filename", "when" (datetime), "source" ("exif"/"upload")}, ...]
    oldest first — everything the recipe page and Edit form need to render
    the gallery."""
    if not _valid_slug(slug):
        return []
    d = _recipe_photos_dir(slug)
    if not os.path.isdir(d):
        return []
    photos = []
    for name in os.listdir(d):
        ext = os.path.splitext(name)[1].lstrip(".").lower()
        if ext not in PHOTO_EXTENSIONS:
            continue
        path = os.path.join(d, name)
        captured = None
        if ext in ("jpg", "jpeg"):
            try:
                with open(path, "rb") as f:
                    captured = extract_jpeg_datetime(f.read())
            except OSError:
                captured = None
        if captured is not None:
            when, source = captured, "exif"
        else:
            when, source = datetime.fromtimestamp(os.path.getmtime(path)), "upload"
        photos.append({"filename": name, "when": when, "source": source})
    photos.sort(key=lambda p: p["when"])
    return photos


def _unique_photo_name(directory, base, ext):
    name = f"{base}.{ext}"
    suffix = 2
    while os.path.exists(os.path.join(directory, name)):
        name = f"{base}_{suffix}.{ext}"
        suffix += 1
    return name


def add_recipe_photo(slug, filename, data):
    if not data or not _valid_slug(slug):
        return
    ext = os.path.splitext(filename)[1].lstrip(".").lower()
    if ext == "jpeg":
        ext = "jpg"
    if ext not in PHOTO_EXTENSIONS:
        return
    when = (extract_jpeg_datetime(data) if ext == "jpg" else None) or datetime.now()

    d = _recipe_photos_dir(slug)
    os.makedirs(d, exist_ok=True)
    out_name = _unique_photo_name(d, when.strftime("%Y-%m-%d_%H-%M-%S"), ext)
    with open(os.path.join(d, out_name), "wb") as f:
        f.write(data)


def delete_recipe_photo(slug, filename):
    if not _valid_slug(slug) or not filename or filename != os.path.basename(filename):
        return
    path = os.path.join(_recipe_photos_dir(slug), filename)
    if os.path.isfile(path):
        os.remove(path)


def delete_all_recipe_photos(slug):
    d = _recipe_photos_dir(slug)
    if not os.path.isdir(d):
        return
    for name in os.listdir(d):
        os.remove(os.path.join(d, name))
    os.rmdir(d)


def extract_jpeg_datetime(data):
    """Best-effort EXIF DateTimeOriginal (falling back to the plain
    DateTime tag) out of a JPEG's APP1 segment — hand-rolled since this
    server has no dependency on Pillow/exifread. Returns None for
    non-JPEG data, missing/unparseable EXIF, or a corrupt segment; callers
    fall back to the upload time whenever this returns None."""
    try:
        if data[0:2] != b"\xff\xd8":
            return None
        pos = 2
        exif_body = None
        while pos + 4 <= len(data):
            if data[pos] != 0xFF:
                break
            marker = data[pos + 1]
            if marker == 0xD8 or 0xD0 <= marker <= 0xD9:
                pos += 2
                continue
            if marker == 0xDA:  # Start of Scan — compressed image data follows
                break
            seg_len = (data[pos + 2] << 8) | data[pos + 3]
            if marker == 0xE1 and data[pos + 4:pos + 10] == b"Exif\x00\x00":
                exif_body = data[pos + 10:pos + 2 + seg_len]
                break
            pos += 2 + seg_len
        if not exif_body or len(exif_body) < 8:
            return None

        endian = exif_body[0:2]
        fmt = "<" if endian == b"II" else ">" if endian == b"MM" else None
        if fmt is None:
            return None

        def u16(off):
            return struct.unpack(fmt + "H", exif_body[off:off + 2])[0]

        def u32(off):
            return struct.unpack(fmt + "I", exif_body[off:off + 4])[0]

        def read_ifd(offset):
            count = u16(offset)
            entries = {}
            for i in range(count):
                entry_off = offset + 2 + i * 12
                tag = u16(entry_off)
                num = u32(entry_off + 4)
                value_off = entry_off + 8
                entries[tag] = (num, value_off)
            return entries

        def get_ascii(entries, tag):
            if tag not in entries:
                return None
            num, value_off = entries[tag]
            raw = exif_body[value_off:value_off + num] if num <= 4 else exif_body[u32(value_off):u32(value_off) + num]
            return raw.split(b"\x00")[0].decode("ascii", "ignore")

        ifd0 = read_ifd(u32(4))
        exif_ifd_ptr = ifd0.get(0x8769)
        if exif_ifd_ptr is not None:
            _num, value_off = exif_ifd_ptr
            sub_ifd = read_ifd(u32(value_off))
            text = get_ascii(sub_ifd, 0x9003)  # DateTimeOriginal
            if text:
                return datetime.strptime(text, "%Y:%m:%d %H:%M:%S")

        text = get_ascii(ifd0, 0x0132)  # plain DateTime, IFD0-level fallback
        if text:
            return datetime.strptime(text, "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None
    return None


def _parse_multipart(body, content_type_header):
    """Text fields and uploaded files out of a multipart/form-data POST
    body. Built on the stdlib email package rather than a hand-rolled
    boundary splitter — multipart/form-data is structurally the same as a
    MIME multipart message, so prepending a Content-Type header and handing
    the whole thing to email.message_from_bytes lets a well-tested stdlib
    parser handle the fiddly part (boundary matching, binary payloads)
    instead of a bespoke one.

    `files` maps each field name to a LIST of (filename, bytes) — a
    multi-file <input multiple> sends several parts under the same field
    name, and a dict keyed by name alone would silently keep only the
    last one.
    """
    header = f"Content-Type: {content_type_header}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii", "ignore")
    msg = email.message_from_bytes(header + body)
    fields, files = {}, {}
    if not msg.is_multipart():
        return fields, files
    for part in msg.get_payload():
        name = filename = None
        for piece in part.get("Content-Disposition", "").split(";"):
            piece = piece.strip()
            if piece.startswith("name="):
                name = piece[len("name="):].strip('"')
            elif piece.startswith("filename="):
                filename = piece[len("filename="):].strip('"')
        if name is None:
            continue
        payload = part.get_payload(decode=True) or b""
        if filename:
            files.setdefault(name, []).append((filename, payload))
        else:
            fields[name] = payload.decode("utf-8", errors="replace")
    return fields, files


# --- "What can I make?" ingredient matching ---------------------------------
# Deliberately simple word-overlap scoring, not real NLP: normalize both
# sides to a bag of singular-ish lowercase words and call a recipe
# ingredient line "have it" if any of its words appears in what you typed.
# Good enough to rank "closest dish" and surface a rough percentage without
# pulling in a matching library for a personal single-user tool.

_WORD_RE = re.compile(r"[a-z0-9]+")


def _word_variants(word):
    variants = {word}
    if word.endswith("es") and len(word) > 3:
        variants.add(word[:-2])
    if word.endswith("s") and len(word) > 2:
        variants.add(word[:-1])
    return variants


def _tokenize(text):
    words = set()
    for w in _WORD_RE.findall(text.lower()):
        words |= _word_variants(w)
    return words


def score_recipe(have_words, recipe):
    """None if the recipe has no parsed ingredient list to match against;
    otherwise a dict with a 0-100 percentage, and which ingredient lines
    matched vs. are missing."""
    all_items = [item for _, items in recipe["ingredient_groups"] for item in items]
    if not all_items:
        return None
    matched, missing = [], []
    for item in all_items:
        (matched if _tokenize(item) & have_words else missing).append(item)
    return {
        "pct": round(len(matched) / len(all_items) * 100),
        "matched": matched,
        "missing": missing,
        "total": len(all_items),
    }


# --- HTML shell --------------------------------------------------------------

CSS = """
:root {
  --bg: #000000;
  --surface: #0A0A0A;
  --surface-2: #141414;
  --border: #262626;
  --text: #ECECEC;
  --text-dim: #96A0A0;
  --accent: #2DD4BF;
  --accent-2: #38BDF8;
  --accent-soft: rgba(45, 212, 191, 0.12);
  --mint: #2DD4BF;
  --danger: #F0475B;
  --chrome: #4A5555;
  --gradient: linear-gradient(135deg, var(--accent), var(--accent-2));
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
  font-size: 16px; line-height: 1.6;
}
h1, h2, h3 {
  font-family: 'Fraunces', Georgia, serif;
  font-weight: 600;
  letter-spacing: -0.01em;
}
a { color: var(--text); text-decoration: none; }
.wrap { max-width: 880px; margin: 0 auto; padding: 0 1.25rem 4rem; }
header.top {
  border-bottom: 1px solid var(--border);
  padding: 1.75rem 0 1.25rem;
  display: flex; flex-direction: column; align-items: flex-start; gap: 0.5rem;
}
header.top .brand { display: flex; align-items: center; gap: 0.65rem; }
header.top h1 { font-size: 1.6rem; margin: 0; }
header.top a.back { font-size: 0.85rem; color: var(--text-dim); border-bottom: 1px dotted var(--chrome); }
header.top a.back:hover { color: var(--text); }

.recipe-card {
  background: linear-gradient(160deg, var(--surface), var(--surface) 70%, var(--surface-2));
  border: 1px solid var(--border); border-radius: 16px;
  padding: 1.4rem 1.5rem; margin: 1.1rem 0;
  box-shadow: 0 1px 0 rgba(255,255,255,0.02) inset, 0 12px 28px -18px rgba(0,0,0,0.7);
}
.recipe-card h2 { margin: 0 0 0.45rem; font-size: 1.3rem; }
.recipe-card .meta { color: var(--accent); opacity: 0.9; font-size: 0.82rem; font-weight: 600; letter-spacing: 0.02em; }
.recipe-card .intro { color: var(--text-dim); font-size: 0.92rem; margin: 0.6rem 0 0; }
.recipe-card .intro p { margin: 0; }
.actions { margin-top: 1.15rem; display: flex; gap: 0.7rem; flex-wrap: wrap; }
.btn {
  display: inline-block; padding: 0.6rem 1.1rem; border-radius: 8px;
  font-weight: 600; font-size: 0.88rem; border: 1px solid var(--border);
  background: var(--surface-2); color: var(--text); transition: border-color 0.15s, transform 0.15s;
}
.btn:hover { border-color: var(--chrome); }
.btn.primary { background: var(--gradient); border-color: transparent; color: #04211D; }
.btn.primary:hover { transform: translateY(-1px); }
.btn.danger { border-color: color-mix(in srgb, var(--danger) 55%, var(--border)); color: var(--danger); }
.btn.danger:hover { background: var(--danger); border-color: var(--danger); color: #fff; }

.section-title { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-dim); margin: 1.7rem 0 0.6rem; }
.ing-group h3 { font-size: 1.02rem; color: var(--accent); margin: 1rem 0 0.4rem; font-family: 'Fraunces', Georgia, serif; }
.ing-group ul { margin: 0; padding-left: 1.3rem; }
.ing-group li { margin: 0.3rem 0; }
.notes-block h3 { font-size: 0.95rem; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.04em; margin: 1.4rem 0 0.4rem; font-family: 'Inter', sans-serif; font-weight: 700; }
.notes-block ul, .notes-block ol { padding-left: 1.3rem; }
.notes-block p { margin: 0.4rem 0; }
.intro-block p { margin: 0.5rem 0 0; color: var(--text-dim); }
.empty { color: var(--text-dim); padding: 1.5rem 0; }
.pager { display: flex; align-items: center; justify-content: space-between; gap: 1rem; margin: 1.2rem 0; }
.pager-status { color: var(--text-dim); font-size: 0.85rem; }

.card-stats { color: var(--text-dim); font-size: 0.85rem; margin: 0.5rem 0 0; }

.search-bar { display: flex; gap: 0.6rem; margin: 1.1rem 0 0.9rem; }
.search-bar input {
  flex: 1; background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
  color: var(--text); padding: 0.6rem 0.7rem; font-size: 0.95rem; font-family: inherit;
}
.filter-row { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.6rem; }
.filter-label { color: var(--text-dim); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.06em; margin-right: 0.2rem; }
.chip {
  padding: 0.35rem 0.8rem; border-radius: 999px; border: 1px solid var(--border);
  background: var(--surface-2); color: var(--text-dim); font-size: 0.82rem; font-weight: 600;
}
.chip.active { background: var(--gradient); border-color: transparent; color: #04211D; }

form.stack { display: flex; flex-direction: column; gap: 0.9rem; max-width: 560px; margin-top: 1.2rem; }
form.stack label { font-size: 0.82rem; color: var(--text-dim); display: flex; flex-direction: column; gap: 0.35rem; }
input, textarea, select {
  background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
  color: var(--text); padding: 0.6rem 0.7rem; font-size: 0.95rem; font-family: inherit;
}
input:focus, textarea:focus, select:focus { outline: 2px solid var(--accent); outline-offset: 1px; border-color: var(--accent); }
textarea { resize: vertical; min-height: 6em; }
.meta-fields { display: flex; gap: 0.7rem; flex-wrap: wrap; }
.meta-fields label { flex: 1; min-width: 120px; }
.checkbox-label { flex-direction: row !important; align-items: center; gap: 0.5rem !important; }
.checkbox-label input { width: auto; }
.hint { color: var(--text-dim); font-size: 0.78rem; }

.list-adder { display: flex; gap: 0.6rem; }
.list-adder input { flex: 1; }
.list-adder button {
  padding: 0.6rem 1.1rem; border-radius: 8px; font-weight: 600; font-size: 0.9rem;
  border: 1px solid var(--border); background: var(--gradient); color: #04211D; cursor: pointer; flex: none;
}
.item-list { list-style: none; margin: 0.7rem 0 0; padding: 0; display: flex; flex-direction: column; gap: 0.4rem; }
.item-list li {
  display: flex; align-items: center; justify-content: space-between; gap: 0.6rem;
  background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
  padding: 0.5rem 0.7rem; font-size: 0.9rem;
}
.item-list .item-text { flex: 1; }
.item-remove {
  background: transparent; border: none; color: var(--text-dim); font-size: 1rem;
  cursor: pointer; padding: 0 0.3rem; flex: none;
}
.item-remove:hover { color: var(--danger); }

/* --- Match cards ("What Can I Make?") ----------------------------------- */
.match-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 16px;
  padding: 1.2rem 1.4rem; margin: 1rem 0; position: relative; overflow: hidden;
}
.match-card .match-bar { position: absolute; inset: 0 auto 0 0; width: var(--pct, 0%); background: var(--accent-soft); z-index: 0; }
.match-card > * { position: relative; z-index: 1; }
.match-card .row-head { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; flex-wrap: wrap; }
.match-card h2 { margin: 0; font-size: 1.2rem; }
.match-pct { font-family: 'Fraunces', Georgia, serif; font-weight: 600; font-size: 1.4rem; color: var(--accent); white-space: nowrap; }
.match-detail { color: var(--text-dim); font-size: 0.85rem; margin-top: 0.5rem; }
.match-detail b { color: var(--mint); font-weight: 600; }

/* --- Confirm modal (Android WebView never implements window.confirm()) - */
.confirm-overlay {
  position: fixed; inset: 0; background: rgba(0, 0, 0, 0.75);
  display: flex; align-items: center; justify-content: center;
  padding: 1.5rem; z-index: 1000;
}
.confirm-overlay[hidden] { display: none; }
.confirm-box {
  background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
  padding: 1.4rem 1.5rem; max-width: 360px; width: 100%;
}
.confirm-box p { margin: 0 0 1.2rem; font-size: 0.98rem; line-height: 1.5; }
.confirm-actions { display: flex; justify-content: flex-end; gap: 0.6rem; }

/* --- Recipe photos: teaser grid, full list, lightbox -------------------- */
.recipe-photo { cursor: zoom-in; display: block; }
.photo-grid { display: grid; grid-template-columns: 1fr; gap: 0.5rem; margin-bottom: 1.1rem; }
.photo-grid[data-count="2"], .photo-grid[data-count="3"], .photo-grid[data-count="4plus"] {
  grid-template-columns: 1fr 1fr;
}
.photo-tile {
  position: relative; border-radius: 14px; overflow: hidden; border: 1px solid var(--border);
}
.photo-grid[data-count="1"] .photo-tile { height: 260px; }
.photo-grid[data-count="2"] .photo-tile,
.photo-grid[data-count="3"] .photo-tile,
.photo-grid[data-count="4plus"] .photo-tile { height: 160px; }
.photo-tile img.recipe-photo { width: 100%; height: 100%; object-fit: cover; }
.photo-tile.see-more { cursor: pointer; }
.photo-tile.see-more .see-more-label {
  position: absolute; inset: 0; background: rgba(45, 212, 191, 0.88); color: #052420;
  display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 0.95rem;
}
.photo-list { display: flex; flex-direction: column; gap: 1rem; margin: 1rem 0; }
.photo-list-item img.recipe-photo {
  width: 100%; max-height: 320px; object-fit: cover; border-radius: 14px; border: 1px solid var(--border);
}
.photo-list-caption { color: var(--text-dim); font-size: 0.85rem; margin-top: 0.4rem; }
.photo-list-item form { margin-top: 0.5rem; }

.photo-lightbox {
  position: fixed; inset: 0; background: rgba(0, 0, 0, 0.9); z-index: 1100;
  display: flex; align-items: center; justify-content: center; padding: 1.5rem;
  overflow: auto;
}
.photo-lightbox[hidden] { display: none; }
.photo-lightbox-content { display: flex; flex-direction: column; align-items: center; gap: 0.7rem; max-width: 100%; }
.photo-lightbox-content img { max-width: 100%; max-height: 75vh; border-radius: 8px; }
.photo-lightbox-caption { color: var(--text-dim); font-size: 0.9rem; text-align: center; }
.photo-lightbox-close {
  position: fixed; top: 1rem; right: 1rem; z-index: 1101;
  width: 42px; height: 42px; border-radius: 50%; border: 1px solid var(--border);
  background: var(--surface); color: var(--text); font-size: 1.2rem;
  display: flex; align-items: center; justify-content: center; cursor: pointer;
}
.photo-lightbox-arrow {
  position: fixed; top: 50%; transform: translateY(-50%); z-index: 1101;
  width: 46px; height: 46px; border-radius: 50%; border: 1px solid var(--border);
  background: var(--surface); color: var(--text); font-size: 1.3rem;
  display: flex; align-items: center; justify-content: center; cursor: pointer;
}
.photo-lightbox-arrow[hidden] { display: none; }
.photo-lightbox-prev { left: 1rem; }
.photo-lightbox-next { right: 1rem; }

.photo-uploader { display: flex; align-items: center; gap: 0.8rem; flex-wrap: wrap; margin: 0 0 1.1rem; }
.photo-uploader progress { width: 140px; height: 8px; accent-color: var(--accent); }
.photo-uploader .hint { margin: 0; }

/* --- Step viewer: full-black flashcard deck, swipe/arrow between cards -- */
.step-page {
  background: #000000; height: 100vh; height: 100dvh;
  display: flex; flex-direction: column; overflow: hidden;
}
.step-top {
  display: flex; align-items: center; justify-content: space-between;
  padding: 1rem 1.25rem; gap: 1rem; flex: none;
}
.step-top a.back { color: var(--text-dim); font-size: 0.85rem; }
.step-progress { padding: 0 1.25rem; color: var(--accent); font-family: 'Inter', sans-serif; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; font-size: 0.85rem; flex: none; }
.step-bar { height: 3px; background: var(--surface-2); margin: 0.6rem 1.25rem 0; border-radius: 2px; overflow: hidden; flex: none; }
.step-bar-fill { height: 100%; background: var(--gradient); transition: width 0.28s ease; }
.deck-viewport { flex: 1; overflow: hidden; min-height: 0; }
.deck-track { display: flex; height: 100%; will-change: transform; }
.card {
  flex: 0 0 100%; height: 100%; box-sizing: border-box;
  display: flex; align-items: center; justify-content: center;
  padding: 1.5rem 1.5rem 2rem; text-align: center; overflow-y: auto;
}
.card-text { font-family: 'Fraunces', Georgia, serif; font-weight: 500; font-size: 1.55rem; line-height: 1.5; max-width: 640px; }
.card-text .step-label { display: block; color: var(--accent); font-size: 1rem; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 0.6rem; font-family: 'Inter', sans-serif; font-weight: 700; }
.step-nav { display: flex; gap: 0.8rem; padding: 0 1.25rem 1.5rem; flex: none; }
.step-arrow {
  flex: 1; display: flex; align-items: center; justify-content: center;
  padding: 1.4rem; border-radius: 16px; border: 1px solid var(--border);
  background: var(--surface); color: var(--text); font-size: 2rem;
  -webkit-tap-highlight-color: transparent; user-select: none;
  -webkit-appearance: none; appearance: none; font-family: inherit; cursor: pointer; margin: 0;
}
.step-arrow.disabled { opacity: 0.3; pointer-events: none; }
.step-arrow.primary { background: var(--gradient); border-color: transparent; color: #04211D; }
.step-arrow .lbl { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; margin-left: 0.5rem; font-family: 'Inter', sans-serif; font-weight: 700; }

/* --- Cook-mode timer -------------------------------------------------- */
.timer-btn {
  width: 42px; height: 42px; border-radius: 50%; flex: none;
  background: var(--surface); border: 1px solid var(--border); color: var(--accent);
  font-size: 1.2rem; display: flex; align-items: center; justify-content: center;
  cursor: pointer; -webkit-tap-highlight-color: transparent;
}
.timer-btn.running { border-color: var(--accent); }
.timer-panel {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 900;
  background: var(--surface); border-top: 1px solid var(--border);
  border-radius: 20px 20px 0 0; padding: 1.5rem 1.5rem 2rem;
  box-shadow: 0 -12px 32px -12px rgba(0,0,0,0.8);
}
.timer-panel[hidden] { display: none; }
.timer-display {
  font-family: 'Fraunces', Georgia, serif; font-size: 3rem; font-weight: 600;
  text-align: center; color: var(--accent); margin-bottom: 1rem;
}
.timer-presets { display: flex; gap: 0.6rem; flex-wrap: wrap; justify-content: center; margin-bottom: 1rem; }
.timer-presets button {
  padding: 0.55rem 1rem; border-radius: 8px; border: 1px solid var(--border);
  background: var(--surface-2); color: var(--text); font-weight: 600; font-size: 0.9rem;
  cursor: pointer;
}
.timer-custom { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
.timer-custom input {
  flex: 0 0 56px; min-width: 0; background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
  color: var(--text); padding: 0.6rem 0.4rem; font-size: 0.95rem; text-align: center;
}
.timer-custom button, .timer-actions button {
  flex: 1;
  padding: 0.6rem 1.1rem; border-radius: 8px; font-weight: 600; font-size: 0.9rem;
  border: 1px solid var(--border); background: var(--gradient); color: #04211D; cursor: pointer;
}
.timer-actions { display: flex; justify-content: center; gap: 0.7rem; }
.timer-actions button { background: var(--surface-2); color: var(--text); }

@media (min-width: 820px) {
  body { font-size: 19px; }
  .wrap { max-width: 1000px; padding: 0 2rem 5rem; }
  header.top { padding: 2.4rem 0 1.6rem; }
  header.top h1 { font-size: 2.1rem; }
  .recipe-card { padding: 1.8rem 2rem; }
  .recipe-card h2 { font-size: 1.55rem; }
  .btn { font-size: 1.02rem; padding: 0.7rem 1.3rem; border-radius: 9px; }
  form.stack { max-width: 620px; gap: 1.2rem; }
  input, textarea { font-size: 1.05rem; padding: 0.7rem 0.85rem; }
  .card-text { font-size: 2.1rem; }
  .step-arrow { font-size: 2.6rem; padding: 2rem; }
  .match-pct { font-size: 1.7rem; }
}
"""

HEAD = (
    "<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<title>My Kitchen</title>"
    "<link rel='icon' href=\"data:image/svg+xml,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
    "<text y='.9em' font-size='90'>%F0%9F%8D%B3</text></svg>\">"
    "<link rel='preconnect' href='https://fonts.googleapis.com'>"
    "<link href='https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=Inter:wght@400;500;600;700&display=swap' rel='stylesheet'>"
    f"<style>{CSS}</style>"
)

# Android's WebView (the Cookbook app) never implements window.confirm() —
# a tap on Delete/Empty Trash there would just silently no-op with no
# dialog. This modal is plain HTML/CSS/JS with no dependency on that
# browser API, so it behaves identically in the app and in a real browser.
CONFIRM_MODAL = (
    "<div class='confirm-overlay' id='confirmOverlay' hidden>"
    "<div class='confirm-box'><p id='confirmMessage'></p>"
    "<div class='confirm-actions'>"
    "<button type='button' class='btn' id='confirmCancelBtn'>Cancel</button>"
    "<button type='button' class='btn primary' id='confirmOkBtn'>Confirm</button>"
    "</div></div></div>"
    "<script>"
    "(function(){"
    "var pending=null;"
    "var overlay=document.getElementById('confirmOverlay');"
    "var msg=document.getElementById('confirmMessage');"
    "window.confirmAction=function(form,message){"
    "pending=form;msg.textContent=message;overlay.hidden=false;return false;"
    "};"
    "document.getElementById('confirmCancelBtn').addEventListener('click',function(){"
    "pending=null;overlay.hidden=true;"
    "});"
    "document.getElementById('confirmOkBtn').addEventListener('click',function(){"
    "overlay.hidden=true;if(pending){pending.submit();}"
    "});"
    "})();"
    "</script>"
)


# Tapping any photo on the page opens it full-size here, with the date/time
# caption underneath and Prev/Next to step through every other photo on
# that same page (its own click-to-open list — the recipe page's teaser
# grid, the full /photos gallery, and the Edit form's management list each
# get an independent Prev/Next range this way, in DOM/newest-first order).
# Closes via the X button, a tap outside the image, arrow keys, or swipe.
PHOTO_LIGHTBOX = (
    "<div class='photo-lightbox' id='photoLightbox' hidden>"
    "<button type='button' class='photo-lightbox-close' id='photoLightboxClose' aria-label='Close'>&times;</button>"
    "<button type='button' class='photo-lightbox-arrow photo-lightbox-prev' id='photoLightboxPrev' aria-label='Previous photo'>&larr;</button>"
    "<div class='photo-lightbox-content'>"
    "<img id='photoLightboxImg' src='' alt=''>"
    "<div class='photo-lightbox-caption' id='photoLightboxCaption'></div>"
    "</div>"
    "<button type='button' class='photo-lightbox-arrow photo-lightbox-next' id='photoLightboxNext' aria-label='Next photo'>&rarr;</button>"
    "</div>"
    "<script>"
    "(function(){"
    "var overlay=document.getElementById('photoLightbox');"
    "var img=document.getElementById('photoLightboxImg');"
    "var caption=document.getElementById('photoLightboxCaption');"
    "var closeBtn=document.getElementById('photoLightboxClose');"
    "var prevBtn=document.getElementById('photoLightboxPrev');"
    "var nextBtn=document.getElementById('photoLightboxNext');"
    "var thumbs=Array.prototype.slice.call(document.querySelectorAll('.recipe-photo[data-caption]'));"
    "var idx=0;"
    "function show(i){"
    "if(!thumbs.length) return;"
    "idx=Math.max(0,Math.min(i,thumbs.length-1));"
    "img.src=thumbs[idx].src;"
    "caption.textContent=thumbs[idx].getAttribute('data-caption')||'';"
    "prevBtn.hidden=idx===0;"
    "nextBtn.hidden=idx===thumbs.length-1;"
    "}"
    "thumbs.forEach(function(thumb,i){"
    "thumb.addEventListener('click',function(){ show(i); overlay.hidden=false; });"
    "});"
    "overlay.addEventListener('click',function(e){"
    "if(e.target===overlay){ overlay.hidden=true; }"
    "});"
    "closeBtn.addEventListener('click',function(){ overlay.hidden=true; });"
    "prevBtn.addEventListener('click',function(){ show(idx-1); });"
    "nextBtn.addEventListener('click',function(){ show(idx+1); });"
    "document.addEventListener('keydown',function(e){"
    "if(overlay.hidden) return;"
    "if(e.key==='ArrowLeft') show(idx-1);"
    "else if(e.key==='ArrowRight') show(idx+1);"
    "else if(e.key==='Escape') overlay.hidden=true;"
    "});"
    "var sx=null;"
    "overlay.addEventListener('touchstart',function(e){ sx=e.changedTouches[0].clientX; },{passive:true});"
    "overlay.addEventListener('touchend',function(e){"
    "if(sx===null) return;"
    "var dx=e.changedTouches[0].clientX-sx;"
    "if(Math.abs(dx)>50){ if(dx<0) show(idx+1); else show(idx-1); }"
    "sx=null;"
    "},{passive:true});"
    "})();"
    "</script>"
)


# Ingredients and Steps on the New/Edit forms are both "type one, click Add,
# repeat" list builders rather than a single multi-line textarea — the
# visible <ul> is pure JS state; a hidden textarea (still a real form field)
# is what actually submits, newline-joined, matching the one-item-per-line
# convention _build_markdown/extract_raw_steps already use everywhere else.
LIST_BUILDER_SCRIPT = (
    "<script>"
    "(function(){"
    "function initListBuilder(prefix){"
    "var input=document.getElementById(prefix+'Input');"
    "var addBtn=document.getElementById(prefix+'AddBtn');"
    "var list=document.getElementById(prefix+'List');"
    "var field=document.getElementById(prefix+'Field');"
    "var items=(field.value||'').split('\\n').map(function(s){return s.trim();}).filter(Boolean);"
    "function render(){"
    "list.innerHTML='';"
    "items.forEach(function(item,idx){"
    "var li=document.createElement('li');"
    "var span=document.createElement('span');"
    "span.className='item-text'; span.textContent=item;"
    "var rm=document.createElement('button');"
    "rm.type='button'; rm.className='item-remove'; rm.setAttribute('aria-label','Remove');"
    "rm.textContent='\\u2715';"
    "rm.addEventListener('click',function(){ items.splice(idx,1); sync(); });"
    "li.appendChild(span); li.appendChild(rm);"
    "list.appendChild(li);"
    "});"
    "}"
    "function sync(){ field.value=items.join('\\n'); render(); }"
    "function addItem(){"
    "var v=input.value.trim();"
    "if(v){ items.push(v); input.value=''; sync(); input.focus(); }"
    "}"
    "addBtn.addEventListener('click',addItem);"
    "input.addEventListener('keydown',function(e){"
    "if(e.key==='Enter'){ e.preventDefault(); addItem(); }"
    "});"
    "render();"
    "}"
    "initListBuilder('ingredients');"
    "initListBuilder('steps');"
    "})();"
    "</script>"
)


def _list_builder_field(name, label, placeholder, add_label, initial_items=(), hint=""):
    initial_value = html.escape("\n".join(initial_items))
    hint_html = f"<span class='hint'>{html.escape(hint)}</span>" if hint else ""
    return (
        f"<label>{html.escape(label)}"
        f"<div class='list-adder'>"
        f"<input type='text' id='{name}Input' placeholder='{html.escape(placeholder)}'>"
        f"<button type='button' id='{name}AddBtn'>{html.escape(add_label)}</button>"
        f"</div>"
        f"<ul class='item-list' id='{name}List'></ul>"
        f"<textarea name='{name}' id='{name}Field' hidden>{initial_value}</textarea>"
        f"{hint_html}"
        f"</label>"
    )


_DIFFICULTY_OPTIONS = ["", "Easy", "Medium", "Hard"]


def _meta_fields_html(prep_time="", cook_time="", difficulty=""):
    difficulty_l = difficulty.strip().lower()
    options = "".join(
        f"<option value='{html.escape(opt)}'{' selected' if opt.lower() == difficulty_l else ''}>"
        f"{html.escape(opt) if opt else 'Not set'}</option>"
        for opt in _DIFFICULTY_OPTIONS
    )
    return (
        "<div class='meta-fields'>"
        f"<label>Prep Time<input name='prep_time' value='{html.escape(prep_time)}' placeholder='e.g. 15 min'></label>"
        f"<label>Cook Time<input name='cook_time' value='{html.escape(cook_time)}' placeholder='e.g. 20 min'></label>"
        f"<label>Difficulty<select name='difficulty'>{options}</select></label>"
        "</div>"
    )


def _photo_uploader_html(slug):
    """A standalone widget, deliberately outside the big Edit <form> —
    adding photos is its own independent action (upload right away, no
    'Save changes' needed, no confirm dialog), not part of editing the
    recipe's text. Lives on both the recipe page and the Edit form."""
    slug_q = quote(slug, safe="")
    return (
        f"<div class='photo-uploader' data-upload-url='/recipe/{slug_q}/photos/upload'>"
        "<input type='file' id='quickPhotoInput' accept='image/*' multiple hidden>"
        "<button type='button' class='btn' id='quickPhotoBtn'>+ Add Photo</button>"
        "<progress id='quickPhotoProgress' max='100' value='0' hidden></progress>"
        "<span class='hint' id='quickPhotoStatus'></span>"
        "</div>"
    )


# Multiple files in one go, with a real upload-progress bar — XMLHttpRequest
# rather than fetch() because only XHR exposes upload progress without extra
# plumbing. Posts straight to /recipe/<slug>/photos/upload (its own tiny
# endpoint, independent of the recipe-edit form) and reloads the page on
# success so the gallery picks up the new photo(s); this widget can appear
# more than once in the DOM only in the sense that only one instance is
# ever rendered per page, so a plain querySelector is fine.
PHOTO_UPLOADER_SCRIPT = (
    "<script>"
    "(function(){"
    "var root=document.querySelector('.photo-uploader');"
    "if(!root) return;"
    "var url=root.getAttribute('data-upload-url');"
    "var input=document.getElementById('quickPhotoInput');"
    "var btn=document.getElementById('quickPhotoBtn');"
    "var bar=document.getElementById('quickPhotoProgress');"
    "var status=document.getElementById('quickPhotoStatus');"
    "btn.addEventListener('click',function(){ input.click(); });"
    "input.addEventListener('change',function(){"
    "var files=input.files;"
    "if(!files || !files.length) return;"
    "var form=new FormData();"
    "for(var i=0;i<files.length;i++){ form.append('photo', files[i]); }"
    "var xhr=new XMLHttpRequest();"
    "xhr.open('POST', url);"
    "btn.disabled=true; bar.hidden=false; bar.value=0;"
    "status.textContent='Uploading '+files.length+(files.length>1?' photos...':' photo...');"
    "xhr.upload.addEventListener('progress',function(e){"
    "if(e.lengthComputable){ bar.value=Math.round((e.loaded/e.total)*100); }"
    "});"
    "xhr.onload=function(){"
    "if(xhr.status>=200 && xhr.status<300){"
    "status.textContent='Done — refreshing...';"
    "window.location.reload();"
    "}else{"
    "status.textContent='Upload failed. Try again.'; btn.disabled=false; bar.hidden=true;"
    "}"
    "};"
    "xhr.onerror=function(){ status.textContent='Upload failed. Try again.'; btn.disabled=false; bar.hidden=true; };"
    "xhr.send(form);"
    "});"
    "})();"
    "</script>"
)


def page(title, body_html, back_href=None, back_label=None):
    # title=None skips the header entirely — used on the home page, where
    # the Android app's own native toolbar already shows "My Kitchen / by
    # Ned Nguyen" right above this content, so an in-page heading here
    # would just repeat it. The browser tab title (set once in HEAD, not
    # per-page) still says "My Kitchen" regardless.
    if title is None:
        header = ""
    else:
        back = f"<a class='back' href='{back_href}'>&larr; {html.escape(back_label or 'Back')}</a>" if back_href else ""
        header = f"<header class='top'>{back}<div class='brand'><h1>{html.escape(title)}</h1></div></header>"
    return (
        f"<!doctype html><html><head>{HEAD}</head><body><div class='wrap'>"
        f"{header}"
        f"{body_html}"
        f"</div>{CONFIRM_MODAL}{PHOTO_LIGHTBOX}</body></html>"
    )


RECIPES_PER_PAGE = 10


_TIME_BUCKETS = [
    ("15", "15 min or less"),
    ("30", "30 min or less"),
    ("60", "1 hour or less"),
    ("60plus", "Over 1 hour"),
]
_DIFFICULTIES = ["Easy", "Medium", "Hard"]


def _in_time_bucket(minutes, bucket):
    if minutes is None:
        return False
    if bucket == "15":
        return minutes <= 15
    if bucket == "30":
        return minutes <= 30
    if bucket == "60":
        return minutes <= 60
    if bucket == "60plus":
        return minutes > 60
    return True


def _filter_href(q, difficulty, time_bucket):
    parts = []
    if q:
        parts.append(f"q={quote(q, safe='')}")
    if difficulty:
        parts.append(f"difficulty={quote(difficulty, safe='')}")
    if time_bucket:
        parts.append(f"time={quote(time_bucket, safe='')}")
    return "/?" + "&".join(parts) if parts else "/"


def _chip(label, active, href):
    cls = "chip active" if active else "chip"
    return f"<a class='{cls}' href='{href}'>{html.escape(label)}</a>"


def _page_href(page_num, q, difficulty, time_bucket):
    parts = [f"page={page_num}"]
    if q:
        parts.append(f"q={quote(q, safe='')}")
    if difficulty:
        parts.append(f"difficulty={quote(difficulty, safe='')}")
    if time_bucket:
        parts.append(f"time={quote(time_bucket, safe='')}")
    return "/?" + "&".join(parts)


def render_home(page_num=1, q=None, difficulty=None, time_bucket=None):
    all_recipes = load_recipes()
    trash_count = len(load_trash())
    top_actions = (
        "<div class='actions'>"
        "<a class='btn primary' href='/recipes/new'>+ New Recipe</a>"
        "<a class='btn' href='/recommend'>What Can I Make?</a>"
        + (f"<a class='btn' href='/trash'>Trash ({trash_count})</a>" if trash_count else "")
        + "</div>"
    )
    new_recipe_btn = "<div class='actions'><a class='btn primary' href='/recipes/new'>+ New Recipe</a></div>"

    if not all_recipes:
        body = (
            f"<p class='empty'>No recipes yet. Drop a .md file into "
            f"<code>{html.escape(RECIPES_DIR)}</code>, or type one in.</p>"
            f"{top_actions}"
        )
        return page(None, body)

    search_bar = (
        f"<form class='search-bar' method='get' action='/'>"
        f"<input type='hidden' name='difficulty' value='{html.escape(difficulty or '')}'>"
        f"<input type='hidden' name='time' value='{html.escape(time_bucket or '')}'>"
        f"<input type='text' name='q' value='{html.escape(q or '')}' placeholder='Search recipes by title...'>"
        f"<button type='submit' class='btn primary'>Search</button>"
        f"</form>"
    )
    difficulty_chips = "".join(
        _chip(d, (difficulty or "").lower() == d.lower(),
              _filter_href(q, None if (difficulty or "").lower() == d.lower() else d, time_bucket))
        for d in _DIFFICULTIES
    )
    time_chips = "".join(
        _chip(label, time_bucket == key,
              _filter_href(q, difficulty, None if time_bucket == key else key))
        for key, label in _TIME_BUCKETS
    )
    filter_bar = (
        f"<div class='filter-row'><span class='filter-label'>Difficulty</span>{difficulty_chips}</div>"
        f"<div class='filter-row'><span class='filter-label'>Time</span>{time_chips}</div>"
    )

    recipes = all_recipes
    if q:
        ql = q.strip().lower()
        recipes = [r for r in recipes if ql in r["title"].lower()]
    if difficulty:
        recipes = [r for r in recipes if (r["meta"]["difficulty"] or "").strip().lower() == difficulty.lower()]
    if time_bucket:
        recipes = [r for r in recipes if _in_time_bucket(r["meta"]["total_time_minutes"], time_bucket)]

    if not recipes:
        body = (
            top_actions + search_bar + filter_bar
            + "<p class='empty'>No recipes match those filters.</p>"
            + new_recipe_btn
        )
        return page(None, body)

    total_pages = max(1, -(-len(recipes) // RECIPES_PER_PAGE))  # ceiling division
    page_num = max(1, min(page_num, total_pages))
    start = (page_num - 1) * RECIPES_PER_PAGE
    page_recipes = recipes[start:start + RECIPES_PER_PAGE]

    cards = []
    for r in page_recipes:
        n_steps = len(r["steps"])
        n_ing = sum(len(items) for _, items in r["ingredient_groups"])
        meta_bits = []
        if n_ing:
            meta_bits.append(f"{n_ing} ingredient{'s' if n_ing != 1 else ''}")
        if n_steps:
            meta_bits.append(f"{n_steps} step{'s' if n_steps != 1 else ''}")
        meta_line = " · ".join(meta_bits)

        m = r["meta"]
        stat_bits = []
        if m["servings"]:
            stat_bits.append(f"Servings: {html.escape(m['servings'])}")
        if m["total_time"]:
            stat_bits.append(f"Total Time: {html.escape(m['total_time'])}")
        if m["difficulty"]:
            stat_bits.append(f"Difficulty: {html.escape(m['difficulty'])}")
        stats_html = f"<div class='card-stats'>{' &middot; '.join(stat_bits)}</div>" if stat_bits else ""

        slug_q = quote(r["slug"], safe="")
        cards.append(
            f"<div class='recipe-card'>"
            f"<h2><a href='/recipe/{slug_q}'>{html.escape(r['title'])}</a></h2>"
            f"<div class='meta'>{meta_line}</div>"
            f"{stats_html}"
            f"<div class='actions'>"
            f"<a class='btn' href='/recipe/{slug_q}'>View</a>"
            f"</div></div>"
        )

    pager = ""
    if total_pages > 1:
        prev_link = (
            f"<a class='btn' href='{_page_href(page_num - 1, q, difficulty, time_bucket)}'>&larr; Prev</a>"
            if page_num > 1 else "<span></span>"
        )
        next_link = (
            f"<a class='btn' href='{_page_href(page_num + 1, q, difficulty, time_bucket)}'>Next &rarr;</a>"
            if page_num < total_pages else "<span></span>"
        )
        pager = (
            f"<div class='pager'>{prev_link}"
            f"<span class='pager-status'>Page {page_num} of {total_pages}</span>"
            f"{next_link}</div>"
        )

    body = top_actions + search_bar + filter_bar + "".join(cards) + pager + new_recipe_btn
    return page(None, body)


def _format_photo_label(when, source):
    verb = "Captured" if source == "exif" else "Uploaded"
    return f"{verb} on {when.strftime('%Y-%b-%d')} {when.strftime('%H:%M')}"


def _photo_grid_html(slug, photos):
    """Teaser grid at the top of the recipe page: one big photo, two side
    by side, three wrapping to a second row, or — four or more — the
    newest 3 plus a square 'See more' tile linking to the full gallery.
    Newest first, since that's the one you actually opened this page to
    check against what's in the pan right now."""
    if not photos:
        return ""
    newest_first = list(reversed(photos))
    slug_q = quote(slug, safe="")
    count = len(newest_first)
    count_key = {1: "1", 2: "2", 3: "3"}.get(count, "4plus")
    shown = newest_first[:3] if count >= 4 else newest_first

    tiles = []
    for p in shown:
        label = html.escape(_format_photo_label(p["when"], p["source"]))
        filename_q = quote(p["filename"], safe="")
        tiles.append(
            "<div class='photo-tile'>"
            f"<img class='recipe-photo' src='/recipe/{slug_q}/photo/{filename_q}' data-caption='{label}' alt=''>"
            "</div>"
        )
    if count >= 4:
        tiles.append(
            f"<a class='photo-tile see-more' href='/recipe/{slug_q}/photos'>"
            "<span class='see-more-label'>See more</span></a>"
        )
    return f"<div class='photo-grid' data-count='{count_key}'>{''.join(tiles)}</div>"


def render_photos_page(slug):
    r = load_recipe(slug)
    if r is None:
        return None
    slug_q = quote(slug, safe="")
    photos = list(reversed(list_recipe_photos(slug)))
    if not photos:
        body = "<p class='empty'>No photos yet.</p>"
    else:
        items = []
        for p in photos:
            label = html.escape(_format_photo_label(p["when"], p["source"]))
            filename_q = quote(p["filename"], safe="")
            items.append(
                "<div class='photo-list-item'>"
                f"<img class='recipe-photo' src='/recipe/{slug_q}/photo/{filename_q}' data-caption='{label}' alt=''>"
                f"<div class='photo-list-caption'>{label}</div>"
                "</div>"
            )
        body = f"<div class='photo-list'>{''.join(items)}</div>"
    return page(f"Photos — {r['title']}", body, back_href=f"/recipe/{slug_q}", back_label=r["title"])


def _photo_manage_html(slug, photos):
    """The Edit form's existing-photos section — same list styling as the
    full gallery page, but each photo also gets its own Delete button."""
    if not photos:
        return ""
    ordered = list(reversed(photos))
    slug_q = quote(slug, safe="")
    items = []
    for p in ordered:
        label = html.escape(_format_photo_label(p["when"], p["source"]))
        filename_q = quote(p["filename"], safe="")
        delete_msg = html.escape("Delete this photo? This can't be undone.")
        items.append(
            "<div class='photo-list-item'>"
            f"<img class='recipe-photo' src='/recipe/{slug_q}/photo/{filename_q}' data-caption='{label}' alt=''>"
            f"<div class='photo-list-caption'>{label}</div>"
            f"<form method='post' action='/recipe/{slug_q}/photo/delete'>"
            f"<input type='hidden' name='filename' value='{html.escape(p['filename'])}'>"
            f"<button type='submit' class='btn danger' data-confirm='{delete_msg}' "
            f"onclick='return confirmAction(this.form, this.dataset.confirm)'>Delete photo</button>"
            f"</form>"
            "</div>"
        )
    return f"<div class='hint'>Existing photos</div><div class='photo-list'>{''.join(items)}</div>"


def render_recipe(slug):
    r = load_recipe(slug)
    if r is None:
        return None
    slug_q = quote(r["slug"], safe="")

    groups_html = []
    for label, items in r["ingredient_groups"]:
        heading = f"<h3>{html.escape(label)}</h3>" if label else ""
        items_html = "".join(f"<li>{render_inline(i)}</li>" for i in items)
        groups_html.append(f"<div class='ing-group'>{heading}<ul>{items_html}</ul></div>")

    notes_html = []
    for heading, body in r["notes_sections"]:
        notes_html.append(f"<div class='notes-block'><h3>{html.escape(heading)}</h3>{body}</div>")

    cook_btn = (
        f"<a class='btn primary' href='/recipe/{slug_q}/step/1'>Start Cooking &rarr;</a>"
        if r["steps"] else ""
    )

    # Delete lives on the Edit screen, not here — the main recipe view is a
    # read/cook surface, so it only offers Cook and Edit; a destructive
    # action shouldn't sit one tap away from just viewing a recipe.
    actions = (
        f"<div class='actions'>"
        f"{cook_btn}"
        f"<a class='btn' href='/recipe/{slug_q}/edit'>Edit</a>"
        f"</div>"
    )

    photo_html = _photo_grid_html(r["slug"], list_recipe_photos(r["slug"]))
    uploader_html = _photo_uploader_html(r["slug"])

    body = (
        f"{photo_html}{uploader_html}"
        f"<div class='intro-block'>{r['intro_html']}</div>"
        f"{actions}"
        f"{'<div class=\"section-title\">Ingredients</div>' + ''.join(groups_html) if groups_html else ''}"
        f"{''.join(notes_html)}"
        f"{PHOTO_UPLOADER_SCRIPT}"
    )
    return page(r["title"], body, back_href="/", back_label="My Kitchen")


def render_step(slug, n):
    """Renders the whole cooking-mode deck in one page load: every step is
    already in the DOM as a card, and Prev/Next/swipe/arrow-keys just slide
    the track client-side (see the inline script) — no page reload between
    steps, so it feels like flipping flashcards rather than following
    links. `n` only picks which card is showing on first paint (so a direct
    link/bookmark/refresh lands on the right one); the URL keeps tracking
    the current card via history.replaceState as you move through it."""
    r = load_recipe(slug)
    if r is None or not r["steps"]:
        return None
    total = len(r["steps"])
    n = max(1, min(n, total))
    slug_q = quote(r["slug"], safe="")

    def render_card(s):
        label_html = f"<span class='step-label'>{html.escape(s['label'])}</span>" if s["label"] else ""
        return f"<div class='card'><div class='card-text'>{label_html}{s['html']}</div></div>"

    cards_html = "".join(render_card(s) for s in r["steps"])

    timer_panel = (
        "<div class='timer-panel' id='timerPanel' hidden>"
        "<div class='timer-display' id='timerDisplay'>00:00</div>"
        "<div class='timer-presets'>"
        "<button type='button' data-min='1'>1 min</button>"
        "<button type='button' data-min='5'>5 min</button>"
        "<button type='button' data-min='10'>10 min</button>"
        "<button type='button' data-min='15'>15 min</button>"
        "<button type='button' data-min='20'>20 min</button>"
        "</div>"
        "<div class='timer-custom'>"
        "<input type='number' id='timerCustomHr' min='0' step='1' placeholder='Hr'>"
        "<input type='number' id='timerCustomMin' min='0' max='59' step='1' placeholder='Min'>"
        "<input type='number' id='timerCustomSec' min='0' max='59' step='1' placeholder='Sec'>"
        "<button type='button' id='timerCustomStart'>Start</button>"
        "</div>"
        "<div class='timer-actions'>"
        "<button type='button' id='timerCancel'>Cancel</button>"
        "<button type='button' id='timerClose'>Close</button>"
        "</div>"
        "</div>"
    )

    body = (
        f"<div class='step-page' id='stepPage'>"
        f"<div class='step-top'>"
        f"<a class='back' href='/recipe/{slug_q}'>&larr; {html.escape(r['title'])}</a>"
        f"<button type='button' class='timer-btn' id='timerBtn' aria-label='Timer'>&#9200;</button>"
        f"</div>"
        f"{timer_panel}"
        f"<div class='step-progress' id='stepProgress'></div>"
        f"<div class='step-bar'><div class='step-bar-fill' id='stepBarFill'></div></div>"
        f"<div class='deck-viewport' id='deckViewport'><div class='deck-track' id='deckTrack'>{cards_html}</div></div>"
        f"<div class='step-nav'>"
        f"<button type='button' class='step-arrow' id='prevBtn' aria-label='Previous step'>&larr;<span class='lbl'>Prev</span></button>"
        f"<button type='button' class='step-arrow' id='nextBtn' aria-label='Next step'>"
        f"<span class='glyph'>&rarr;</span><span class='lbl'>Next</span></button>"
        f"</div></div>"
        "<script>"
        "(function(){"
        f"var slug={json.dumps(quote(r['slug'], safe=''))};"
        f"var total={total};"
        f"var idx={n - 1};"
        "var track=document.getElementById('deckTrack');"
        "var viewport=document.getElementById('deckViewport');"
        "var progressEl=document.getElementById('stepProgress');"
        "var barFill=document.getElementById('stepBarFill');"
        "var prevBtn=document.getElementById('prevBtn');"
        "var nextBtn=document.getElementById('nextBtn');"
        "var nextGlyph=nextBtn.querySelector('.glyph');"
        "var nextLbl=nextBtn.querySelector('.lbl');"
        "function render(animate){"
        # translateX('%') resolves against the TRACK's own full width (all
        # cards laid end to end), not the one-card-wide viewport — so a
        # percentage here is off by a factor of `total` and was leaving two
        # cards half-visible instead of showing exactly one. Pixel math
        # against the viewport's actual rendered width sidesteps that
        # entirely and is exact regardless of how many cards there are.
        "track.style.transition=animate?'transform 0.28s ease':'none';"
        "track.style.transform='translateX('+(-idx*viewport.clientWidth)+'px)';"
        "progressEl.textContent='Step '+(idx+1)+' of '+total;"
        "barFill.style.width=Math.round((idx+1)/total*100)+'%';"
        "prevBtn.classList.toggle('disabled', idx===0);"
        "var atEnd=idx===total-1;"
        "nextBtn.classList.toggle('primary', atEnd);"
        "nextGlyph.innerHTML=atEnd?'&#10003;':'&rarr;';"
        "nextLbl.textContent=atEnd?'Done':'Next';"
        "history.replaceState(null,'','/recipe/'+slug+'/step/'+(idx+1));"
        "}"
        "function goNext(){"
        "if(idx<total-1){idx++;render(true);}"
        "else{window.location='/recipe/'+slug;}"
        "}"
        "function goPrev(){if(idx>0){idx--;render(true);}}"
        "prevBtn.addEventListener('click',goPrev);"
        "nextBtn.addEventListener('click',goNext);"
        "document.addEventListener('keydown',function(e){"
        "if(e.key==='ArrowRight')goNext();"
        "if(e.key==='ArrowLeft')goPrev();"
        "});"
        "var root=document.getElementById('stepPage');"
        "var sx=null, sy=null;"
        "root.addEventListener('touchstart',function(e){var t=e.changedTouches[0];sx=t.clientX;sy=t.clientY;},{passive:true});"
        "root.addEventListener('touchend',function(e){"
        "if(sx===null)return;"
        "var t=e.changedTouches[0];var dx=t.clientX-sx;var dy=t.clientY-sy;"
        "if(Math.abs(dx)>60 && Math.abs(dx)>Math.abs(dy)){if(dx<0)goNext();else goPrev();}"
        "sx=null;sy=null;"
        "},{passive:true});"
        "window.addEventListener('resize',function(){render(false);});"
        "render(false);"
        # Best-effort Screen Wake Lock: only actually works in a secure
        # context (https, or localhost) per spec, so on a plain-HTTP LAN
        # address like this one it silently does nothing — which is why
        # the Android app also sets FLAG_KEEP_SCREEN_ON natively while a
        # /step/ URL is showing (see MainActivity.kt). Kept here anyway so
        # it works for free the day this ever sits behind HTTPS.
        "var wakeLock=null;"
        "function requestWake(){"
        "if('wakeLock' in navigator){"
        "navigator.wakeLock.request('screen').then(function(l){wakeLock=l;}).catch(function(){});"
        "}"
        "}"
        "requestWake();"
        "document.addEventListener('visibilitychange',function(){"
        "if(document.visibilityState==='visible')requestWake();"
        "});"
        "})();"
        "</script>"
        "<script>"
        "(function(){"
        # Countdown lives entirely in this one page load (see the deck
        # script above) so it keeps running across step swipes, which are
        # client-side only. Audio is a few square-wave beeps via
        # AudioContext rather than a bundled sound file — this server is
        # stdlib-only with no static asset pipeline, and it sidesteps
        # mobile autoplay restrictions since the context is created/resumed
        # inside a real button-tap handler (Start), not from the timer
        # callback that fires later on its own.
        "var btn=document.getElementById('timerBtn');"
        "var panel=document.getElementById('timerPanel');"
        "var display=document.getElementById('timerDisplay');"
        "var closeBtn=document.getElementById('timerClose');"
        "var cancelBtn=document.getElementById('timerCancel');"
        "var customHrInput=document.getElementById('timerCustomHr');"
        "var customMinInput=document.getElementById('timerCustomMin');"
        "var customSecInput=document.getElementById('timerCustomSec');"
        "var customBtn=document.getElementById('timerCustomStart');"
        "var presetBtns=panel.querySelectorAll('[data-min]');"
        "var endTime=null, intervalId=null, audioCtx=null;"
        "function fmt(sec){"
        "sec=Math.max(0,Math.round(sec));"
        "var h=Math.floor(sec/3600), m=Math.floor((sec%3600)/60), s=sec%60;"
        "var mm=(m<10?'0':'')+m, ss=(s<10?'0':'')+s;"
        "return h>0 ? (h+':'+mm+':'+ss) : (mm+':'+ss);"
        "}"
        "function ensureAudio(){"
        "if(!audioCtx){"
        "var Ctx=window.AudioContext||window.webkitAudioContext;"
        "if(Ctx) audioCtx=new Ctx();"
        "}"
        "if(audioCtx && audioCtx.state==='suspended') audioCtx.resume();"
        "}"
        "function ring(){"
        "try{"
        "if(audioCtx){"
        "[0,0.35,0.7,1.05,1.4].forEach(function(t){"
        "var o=audioCtx.createOscillator(), g=audioCtx.createGain();"
        "o.type='square'; o.frequency.value=880;"
        "g.gain.setValueAtTime(0.35, audioCtx.currentTime+t);"
        "o.connect(g); g.connect(audioCtx.destination);"
        "o.start(audioCtx.currentTime+t); o.stop(audioCtx.currentTime+t+0.25);"
        "});"
        "}"
        "}catch(e){}"
        "if(navigator.vibrate) navigator.vibrate([300,150,300,150,300]);"
        "panel.hidden=false;"
        "display.textContent=\"Time's up!\";"
        "btn.classList.remove('running');"
        "}"
        "function tick(){"
        "var remaining=(endTime-Date.now())/1000;"
        "if(remaining<=0){"
        "clearInterval(intervalId); intervalId=null; endTime=null;"
        "ring();"
        "return;"
        "}"
        "display.textContent=fmt(remaining);"
        "}"
        "function start(totalSeconds){"
        "if(!(totalSeconds>0)) return;"
        "ensureAudio();"
        "endTime=Date.now()+totalSeconds*1000;"
        "display.textContent=fmt(totalSeconds);"
        "if(intervalId) clearInterval(intervalId);"
        "intervalId=setInterval(tick,250);"
        "btn.classList.add('running');"
        "panel.hidden=false;"
        "}"
        "function cancelTimer(){"
        "if(intervalId) clearInterval(intervalId);"
        "intervalId=null; endTime=null;"
        "display.textContent='00:00';"
        "btn.classList.remove('running');"
        "}"
        "btn.addEventListener('click',function(){ panel.hidden=!panel.hidden; });"
        "closeBtn.addEventListener('click',function(){ panel.hidden=true; });"
        "cancelBtn.addEventListener('click',cancelTimer);"
        "presetBtns.forEach(function(b){"
        "b.addEventListener('click',function(){ start(parseFloat(b.dataset.min)*60); });"
        "});"
        "customBtn.addEventListener('click',function(){"
        "var hrs=parseFloat(customHrInput.value)||0;"
        "var mins=parseFloat(customMinInput.value)||0;"
        "var secs=parseFloat(customSecInput.value)||0;"
        "start(hrs*3600+mins*60+secs);"
        "});"
        "})();"
        "</script>"
    )
    return (
        f"<!doctype html><html><head>{HEAD}</head><body>{body}</body></html>",
        r["title"],
    )


def render_new_recipe_form():
    body = (
        "<form class='stack' method='post' action='/recipes/new'>"
        "<label>Title<input name='title' placeholder='Grandma’s Fried Rice' required></label>"
        + _meta_fields_html()
        + _list_builder_field(
            "ingredients", "Ingredients", "e.g. 2 cups jasmine rice", "Add",
            hint="Type one ingredient, click Add, repeat.",
        )
        + _list_builder_field(
            "steps", "Steps", "e.g. Heat oil in a wok over high heat", "Add Step",
            hint="Type one step, click Add Step, repeat — each becomes its own card in the cooking view.",
        )
        + "<label>Summary (optional)<textarea name='summary' placeholder='A quick one-pan weeknight fried rice with whatever vegetables are in the fridge.'></textarea>"
        "<span class='hint'>Timing, notes, anything else worth knowing before you start. Shown at the top of the recipe.</span></label>"
        "<span class='hint'>Photos are added from the recipe page once it's saved.</span>"
        "<div class='actions'><button class='btn primary' type='submit'>Save recipe</button></div>"
        "</form>"
        + LIST_BUILDER_SCRIPT
    )
    return page("New Recipe", body, back_href="/", back_label="My Kitchen")


def render_edit_form(slug):
    raw = load_recipe_raw(slug)
    if raw is None:
        return None
    r = parse_recipe(raw)
    title = html.escape(r["title"])
    summary = html.escape(extract_editable_summary(raw))
    slug_q = quote(slug, safe="")

    # The structured Ingredients box can only cleanly own a recipe with one
    # plain (ungrouped) '## Ingredients' section — see extract_editable_summary.
    # Anything split across several named sections (Sauce/Beef/Noodles, ...)
    # keeps living in Summary above, verbatim, and this box starts empty
    # rather than risk silently flattening/losing that grouping on save.
    groups = r["ingredient_groups"]
    simple_ingredients = groups[0][1] if len(groups) == 1 and groups[0][0] is None else []
    ingredients_hint = (
        "Type one ingredient, click Add, repeat."
        if not groups or simple_ingredients
        else "This recipe's ingredients are grouped (Sauce/Beef/...) and stay in Summary above — "
             "anything added here becomes a separate plain list alongside them."
    )

    delete_msg = html.escape(f'Move "{r["title"]}" to trash? You can restore it later.')

    photos = list_recipe_photos(slug)

    body = (
        f"<form class='stack' method='post' action='/recipe/{slug_q}/edit' "
        f"onsubmit='return confirmAction(this, \"Save changes to this recipe?\")'>"
        f"<label>Title<input name='title' value='{title}' required></label>"
        + _meta_fields_html(
            prep_time=r["meta"]["prep_time"] or "",
            cook_time=r["meta"]["cook_time"] or "",
            difficulty=r["meta"]["difficulty"] or "",
        )
        + _list_builder_field(
            "ingredients", "Ingredients", "e.g. 2 cups jasmine rice", "Add",
            initial_items=simple_ingredients, hint=ingredients_hint,
        )
        + _list_builder_field(
            "steps", "Steps", "e.g. Heat oil in a wok over high heat", "Add Step",
            initial_items=extract_raw_steps(raw),
            hint="Type one step, click Add Step, repeat — each becomes its own card in the cooking view.",
        )
        + f"<label>Summary (optional)<textarea name='summary'>{summary}</textarea>"
        f"<span class='hint'>Timing, notes, anything else worth knowing before you start. Shown at the top of the recipe.</span></label>"
        + f"<div class='actions'>"
        f"<button class='btn primary' type='submit'>Save changes</button>"
        f"<a class='btn' href='/recipe/{slug_q}'>Cancel</a>"
        f"</div>"
        f"</form>"
        f"{LIST_BUILDER_SCRIPT}"
        + _photo_uploader_html(slug)
        + _photo_manage_html(slug, photos)
        + f"{PHOTO_UPLOADER_SCRIPT}"
        f"<div class='actions'>"
        f"<form method='post' action='/recipe/{slug_q}/delete'>"
        f"<button type='submit' class='btn danger' data-confirm='{delete_msg}' "
        f"onclick='return confirmAction(this.form, this.dataset.confirm)'>Delete</button>"
        f"</form>"
        f"</div>"
    )
    return page(f"Edit — {r['title']}", body, back_href=f"/recipe/{slug_q}", back_label=r["title"])


def render_trash():
    trashed = load_trash()
    if not trashed:
        body = "<p class='empty'>Trash is empty.</p>"
        return page("Trash", body, back_href="/", back_label="My Kitchen")

    cards = []
    for r in trashed:
        slug_q = quote(r["slug"], safe="")
        restore_msg = html.escape(f'Restore "{r["title"]}"?')
        delete_msg = html.escape(f'Permanently delete "{r["title"]}"? This cannot be undone.')
        cards.append(
            f"<div class='recipe-card'>"
            f"<h2>{html.escape(r['title'])}</h2>"
            f"<div class='actions'>"
            f"<form method='post' action='/recipe/{slug_q}/restore'>"
            f"<button type='submit' class='btn primary' data-confirm='{restore_msg}' "
            f"onclick='return confirmAction(this.form, this.dataset.confirm)'>Restore</button>"
            f"</form>"
            f"<form method='post' action='/recipe/{slug_q}/delete-forever'>"
            f"<button type='submit' class='btn danger' data-confirm='{delete_msg}' "
            f"onclick='return confirmAction(this.form, this.dataset.confirm)'>Delete forever</button>"
            f"</form>"
            f"</div></div>"
        )

    empty_msg = html.escape(f"Permanently delete all {len(trashed)} item(s) in trash? This cannot be undone.")
    empty_btn = (
        f"<div class='actions'><form method='post' action='/trash/empty'>"
        f"<button type='submit' class='btn danger' data-confirm='{empty_msg}' "
        f"onclick='return confirmAction(this.form, this.dataset.confirm)'>Empty Trash</button>"
        f"</form></div>"
    )

    body = (
        "<p class='hint'>Deleted recipes land here first. Restore to bring one "
        "back, or delete forever to remove it for good.</p>"
        f"{empty_btn}{''.join(cards)}"
    )
    return page("Trash", body, back_href="/", back_label="My Kitchen")


def render_recommend(have_text=""):
    body = (
        "<form class='stack' method='post' action='/recommend'>"
        f"<label>What ingredients do you have?<textarea name='have' placeholder='eggs&#10;garlic&#10;soy sauce&#10;green onion' required>{html.escape(have_text)}</textarea>"
        "<span class='hint'>One per line. Matching is approximate — based on shared words with each recipe's ingredient list.</span></label>"
        "<div class='actions'><button class='btn primary' type='submit'>Find Recipes</button></div>"
        "</form>"
    )

    if have_text.strip():
        have_words = _tokenize(have_text)
        scored = []
        for r in load_recipes():
            s = score_recipe(have_words, r)
            if s is not None:
                scored.append((r, s))
        scored.sort(key=lambda pair: (-pair[1]["pct"], -len(pair[1]["matched"])))
        top = [pair for pair in scored if pair[1]["pct"] > 0][:5]

        if not scored:
            body += "<p class='empty'>None of your recipes have a parsed ingredient list to match against yet.</p>"
        elif not top:
            body += "<p class='empty'>No close matches — none of your recipes share an ingredient with that list.</p>"
        else:
            result_cards = []
            for r, s in top:
                slug_q = quote(r["slug"], safe="")
                missing = ", ".join(s["missing"][:6]) + ("…" if len(s["missing"]) > 6 else "") if s["missing"] else ""
                missing_html = f"<div class='match-detail'>Missing: {html.escape(missing)}</div>" if missing else "<div class='match-detail'><b>You have everything.</b></div>"
                result_cards.append(
                    f"<div class='match-card' style='--pct:{s['pct']}%'>"
                    f"<div class='match-bar'></div>"
                    f"<div class='row-head'><h2><a href='/recipe/{slug_q}'>{html.escape(r['title'])}</a></h2>"
                    f"<span class='match-pct'>{s['pct']}%</span></div>"
                    f"<div class='match-detail'>{len(s['matched'])} of {s['total']} ingredients you have</div>"
                    f"{missing_html}"
                    f"</div>"
                )
            body += f"<div class='section-title'>Closest matches</div>{''.join(result_cards)}"

    return page("What Can I Make?", body, back_href="/", back_label="My Kitchen")


# --- HTTP handler ------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "Cookbook/1.0"

    def log_message(self, fmt, *args):
        pass  # keep stdout quiet; nothing sensitive, just less noise

    def _check_auth(self):
        header = self.headers.get("Authorization")
        expected = "Basic " + base64.b64encode(f"{AUTH_USER}:{AUTH_PASS}".encode()).decode()
        if header != expected:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="My Kitchen"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        return True

    def _send_html(self, html_body, status=200):
        encoded = html_body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _not_found(self):
        self._send_html(page("Not Found", "<p class='empty'>Nothing here.</p>", back_href="/", back_label="My Kitchen"), status=404)

    def _read_form(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("multipart/form-data"):
            fields, self.uploaded_files = _parse_multipart(body, content_type)
            return fields
        self.uploaded_files = {}
        parsed = parse_qs(body.decode("utf-8"))
        return {k: v[0] for k, v in parsed.items()}

    def do_GET(self):
        if not self._check_auth():
            return
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        parts = [unquote(p) for p in path.split("/") if p]

        if not parts:
            query = parse_qs(parsed_url.query)
            try:
                page_num = max(1, int(query.get("page", ["1"])[0]))
            except ValueError:
                page_num = 1
            q = query.get("q", [""])[0].strip() or None
            difficulty = query.get("difficulty", [""])[0].strip() or None
            time_bucket = query.get("time", [""])[0].strip() or None
            return self._send_html(render_home(page_num, q, difficulty, time_bucket))
        if parts == ["recipes", "new"]:
            return self._send_html(render_new_recipe_form())
        if parts == ["recommend"]:
            return self._send_html(render_recommend())
        if parts == ["trash"]:
            return self._send_html(render_trash())
        if len(parts) == 3 and parts[0] == "recipe" and parts[2] == "edit":
            out = render_edit_form(parts[1])
            return self._send_html(out) if out else self._not_found()
        if len(parts) == 3 and parts[0] == "recipe" and parts[2] == "photos":
            out = render_photos_page(parts[1])
            return self._send_html(out) if out else self._not_found()
        if len(parts) == 4 and parts[0] == "recipe" and parts[2] == "photo":
            slug, filename = parts[1], parts[3]
            if not _valid_slug(slug) or filename != os.path.basename(filename):
                return self._not_found()
            path = os.path.join(_recipe_photos_dir(slug), filename)
            if not os.path.isfile(path):
                return self._not_found()
            ext = os.path.splitext(filename)[1].lstrip(".").lower()
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", PHOTO_CONTENT_TYPES.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if len(parts) == 2 and parts[0] == "recipe":
            out = render_recipe(parts[1])
            return self._send_html(out) if out else self._not_found()
        if len(parts) == 4 and parts[0] == "recipe" and parts[2] == "step" and parts[3].isdigit():
            out = render_step(parts[1], int(parts[3]))
            if out is None:
                return self._not_found()
            html_body, _title = out
            return self._send_html(html_body)
        self._not_found()

    def do_POST(self):
        if not self._check_auth():
            return
        path = urlparse(self.path).path
        parts = [unquote(p) for p in path.split("/") if p]
        form = self._read_form()

        if parts == ["recipes", "new"]:
            title = form.get("title", "").strip()
            if not title:
                return self._redirect("/recipes/new")
            slug = save_recipe(
                title=title,
                summary=form.get("summary", ""),
                steps=form.get("steps", ""),
                ingredients=form.get("ingredients", ""),
                prep_time=form.get("prep_time", ""),
                cook_time=form.get("cook_time", ""),
                difficulty=form.get("difficulty", ""),
            )
            return self._redirect(f"/recipe/{quote(slug, safe='')}")

        if parts == ["recommend"]:
            return self._send_html(render_recommend(form.get("have", "")))

        if len(parts) == 3 and parts[0] == "recipe" and parts[2] == "edit":
            slug = parts[1]
            title = form.get("title", "").strip()
            if not title:
                return self._redirect(f"/recipe/{quote(slug, safe='')}/edit")
            ok = update_recipe(
                slug, title=title, summary=form.get("summary", ""),
                steps=form.get("steps", ""), ingredients=form.get("ingredients", ""),
                prep_time=form.get("prep_time", ""),
                cook_time=form.get("cook_time", ""),
                difficulty=form.get("difficulty", ""),
            )
            return self._redirect(f"/recipe/{quote(slug, safe='')}") if ok else self._not_found()

        if len(parts) == 4 and parts[0] == "recipe" and parts[2] == "photos" and parts[3] == "upload":
            slug = parts[1]
            for filename, data in self.uploaded_files.get("photo", []):
                if filename:
                    add_recipe_photo(slug, filename, data)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if len(parts) == 4 and parts[0] == "recipe" and parts[2] == "photo" and parts[3] == "delete":
            # Trailing "-delete" would collide with a legitimately weird
            # filename, so this is /recipe/<slug>/photo/delete with the
            # filename as a form field instead of a path segment.
            delete_recipe_photo(parts[1], form.get("filename", ""))
            return self._redirect(f"/recipe/{quote(parts[1], safe='')}/edit")

        if len(parts) == 3 and parts[0] == "recipe" and parts[2] == "delete":
            trash_recipe(parts[1])
            return self._redirect("/")

        if len(parts) == 3 and parts[0] == "recipe" and parts[2] == "restore":
            restore_recipe(parts[1])
            return self._redirect("/trash")

        if len(parts) == 3 and parts[0] == "recipe" and parts[2] == "delete-forever":
            delete_forever(parts[1])
            return self._redirect("/trash")

        if parts == ["trash", "empty"]:
            empty_trash()
            return self._redirect("/trash")

        self._not_found()


def main():
    os.makedirs(RECIPES_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Cookbook listening on 0.0.0.0:{PORT}")
    print(f"Recipes: {RECIPES_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
