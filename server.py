#!/usr/bin/env python3
"""
Cookbook — a small, dependency-free (stdlib only) HTTP server for browsing
and cooking from recipes stored as plain Markdown files in
~/Nextcloud/Documents/Cooking. Same philosophy as Vehicle Maintenance
Record: one file, plain Python, nothing to pip install, nothing to break
on an update.

Recipes are read straight off disk on every request — there's no database
and no build step, so a file dropped into the Cooking folder from any
device (upload, Nextcloud sync, scp, whatever) just shows up the next time
the page loads.

Run:  python3 server.py
Serves on 0.0.0.0:8092, protected by HTTP Basic Auth.
"""
import base64
import datetime
import html
import os
import re
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, quote, unquote

PORT = 8092
RECIPES_DIR = os.path.join(os.path.expanduser("~"), "Nextcloud", "Documents", "Cooking")

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


def parse_recipe(text):
    lines = text.replace("\r\n", "\n").split("\n")

    title = "Untitled Recipe"
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        lines = lines[1:]

    # Split into (heading, body_lines) sections on '## ' headings; everything
    # before the first one is the intro (byline, description, meta line).
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

    ingredient_groups = []  # [(group_label_or_None, [item, ...]), ...]
    steps = []              # [{"label": str_or_None, "html": str}, ...]
    notes_sections = []     # [(heading, html), ...]
    seen_steps_section = False

    for heading, body in sections:
        non_empty = [l for l in body if l.strip()]
        bullet_ratio = (sum(1 for l in non_empty if BULLET_RE.match(l)) / len(non_empty)) if non_empty else 0
        numbered_ratio = (sum(1 for l in non_empty if NUMBERED_RE.match(l)) / len(non_empty)) if non_empty else 0

        if STEP_HEADING_RE.match(heading):
            label = STEP_HEADING_RE.sub("", heading).strip() or None
            steps.append({"label": label, "html": render_block(body)})
            seen_steps_section = True
            continue

        if INSTRUCTION_WORD_RE.search(heading) or (numbered_ratio > 0.5 and not INGREDIENT_WORD_RE.search(heading)):
            for chunk in split_numbered_steps(body):
                steps.append({"label": None, "html": render_block(chunk)})
            seen_steps_section = True
            continue

        if INGREDIENT_WORD_RE.search(heading) or (bullet_ratio > 0.5 and not seen_steps_section):
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
    }


def slugify(title):
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_")
    return slug or "recipe"


def load_recipes():
    """Every top-level .md file in RECIPES_DIR, parsed. Subfolders (e.g. an
    "Original sources" archive) are skipped on purpose — only files directly
    in Cooking/ are recipes."""
    recipes = []
    if not os.path.isdir(RECIPES_DIR):
        return recipes
    for name in sorted(os.listdir(RECIPES_DIR)):
        if not name.lower().endswith(".md") or name.startswith("."):
            continue
        path = os.path.join(RECIPES_DIR, name)
        if not os.path.isfile(path):
            continue
        slug = name[:-3]
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        parsed = parse_recipe(text)
        parsed["slug"] = slug
        recipes.append(parsed)
    return recipes


def load_recipe(slug):
    if not slug or slug != os.path.basename(slug) or "/" in slug or slug.startswith("."):
        return None
    path = os.path.join(RECIPES_DIR, slug + ".md")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    parsed = parse_recipe(text)
    parsed["slug"] = slug
    return parsed


def save_recipe(title, description, servings, prep_time, cook_time, ingredients, steps, notes):
    slug = slugify(title)
    path = os.path.join(RECIPES_DIR, slug + ".md")
    suffix = 2
    while os.path.exists(path):
        path = os.path.join(RECIPES_DIR, f"{slug}_{suffix}.md")
        suffix += 1

    parts = [f"# {title}\n"]
    if description.strip():
        parts.append(f"\n{description.strip()}\n")
    meta_bits = []
    if servings.strip():
        meta_bits.append(f"**Servings:** {servings.strip()}")
    if prep_time.strip():
        meta_bits.append(f"**Prep Time:** {prep_time.strip()}")
    if cook_time.strip():
        meta_bits.append(f"**Cook Time:** {cook_time.strip()}")
    if meta_bits:
        parts.append(f"\n{' | '.join(meta_bits)}\n")

    ingredient_lines = [l.strip() for l in ingredients.splitlines() if l.strip()]
    if ingredient_lines:
        parts.append("\n## Ingredients\n\n" + "\n".join(f"- {l}" for l in ingredient_lines) + "\n")

    step_lines = [l.strip() for l in steps.splitlines() if l.strip()]
    if step_lines:
        parts.append("\n## Instructions\n\n" + "\n".join(f"{i}. {l}" for i, l in enumerate(step_lines, 1)) + "\n")

    note_lines = [l.strip() for l in notes.splitlines() if l.strip()]
    if note_lines:
        parts.append("\n## Notes\n\n" + "\n".join(f"- {l}" for l in note_lines) + "\n")

    os.makedirs(RECIPES_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return os.path.basename(path)[:-3]


# --- HTML shell --------------------------------------------------------------

CSS = """
:root {
  --bg: #000000;
  --surface: #0D0D0F;
  --surface-2: #17171A;
  --border: #2B2B30;
  --text: #ECECEF;
  --text-dim: #8B8B93;
  --accent: #F2994A;
  --accent-hover: #FFAD66;
  --chrome: #4C4C53;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font-family: 'Barlow', 'Segoe UI', system-ui, sans-serif;
  font-size: 16px; line-height: 1.55;
}
h1, h2, h3, .num {
  font-family: 'Rajdhani', 'Barlow', sans-serif;
  font-weight: 700;
  letter-spacing: 0.01em;
}
a { color: var(--text); text-decoration: none; }
.wrap { max-width: 880px; margin: 0 auto; padding: 0 1.25rem 4rem; }
header.top {
  border-bottom: 1px solid var(--border);
  padding: 1.5rem 0 1.1rem;
  display: flex; align-items: baseline; justify-content: space-between; gap: 1rem;
}
header.top .brand { display: flex; align-items: center; gap: 0.6rem; }
header.top .brand .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 8px var(--accent); }
header.top h1 { font-size: 1.5rem; margin: 0; text-transform: uppercase; }
header.top a.back { font-size: 0.85rem; color: var(--text-dim); border-bottom: 1px dotted var(--chrome); }
header.top a.back:hover { color: var(--text); }

.recipe-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 1.3rem 1.4rem; margin: 1.1rem 0;
}
.recipe-card h2 { margin: 0 0 0.4rem; font-size: 1.25rem; }
.recipe-card .meta { color: var(--text-dim); font-size: 0.85rem; }
.recipe-card .intro { color: var(--text-dim); font-size: 0.92rem; margin: 0.6rem 0 0; }
.recipe-card .intro p { margin: 0; }
.actions { margin-top: 1.1rem; display: flex; gap: 0.7rem; flex-wrap: wrap; }
.btn {
  display: inline-block; padding: 0.55rem 1rem; border-radius: 6px;
  font-weight: 600; font-size: 0.88rem; border: 1px solid var(--border);
  background: var(--surface-2); color: var(--text);
}
.btn:hover { border-color: var(--chrome); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #1a0f04; }
.btn.primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }

.section-title { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-dim); margin: 1.6rem 0 0.5rem; }
.ing-group h3 { font-size: 1rem; color: var(--accent); margin: 1rem 0 0.4rem; }
.ing-group ul { margin: 0; padding-left: 1.3rem; }
.ing-group li { margin: 0.25rem 0; }
.notes-block h3 { font-size: 0.95rem; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.04em; margin: 1.4rem 0 0.4rem; }
.notes-block ul, .notes-block ol { padding-left: 1.3rem; }
.notes-block p { margin: 0.4rem 0; }
.intro-block p { margin: 0.4rem 0 0; color: var(--text-dim); }
.empty { color: var(--text-dim); padding: 1.5rem 0; }

form.stack { display: flex; flex-direction: column; gap: 0.9rem; max-width: 560px; margin-top: 1.2rem; }
form.stack label { font-size: 0.82rem; color: var(--text-dim); display: flex; flex-direction: column; gap: 0.35rem; }
form.stack .row3 { display: flex; gap: 0.7rem; }
form.stack .row3 label { flex: 1; }
input, textarea {
  background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px;
  color: var(--text); padding: 0.55rem 0.65rem; font-size: 0.95rem; font-family: inherit;
}
input:focus, textarea:focus { outline: 2px solid var(--accent); outline-offset: 1px; border-color: var(--accent); }
textarea { resize: vertical; min-height: 6em; }
.hint { color: var(--text-dim); font-size: 0.78rem; }

/* --- Step viewer: full-black, big-thumb navigation --------------------- */
.step-page { background: #000000; min-height: 100vh; display: flex; flex-direction: column; }
.step-top {
  display: flex; align-items: center; justify-content: space-between;
  padding: 1rem 1.25rem; gap: 1rem;
}
.step-top a.back { color: var(--text-dim); font-size: 0.85rem; }
.step-top .recipe-name { color: var(--text-dim); font-size: 0.85rem; text-align: right; }
.step-progress { padding: 0 1.25rem; color: var(--accent); font-family: 'Rajdhani', sans-serif; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; font-size: 0.85rem; }
.step-bar { height: 3px; background: var(--surface-2); margin: 0.6rem 1.25rem 0; border-radius: 2px; overflow: hidden; }
.step-bar-fill { height: 100%; background: var(--accent); }
.step-body {
  flex: 1; display: flex; align-items: center; justify-content: center;
  padding: 1.5rem 1.5rem 2rem; text-align: center;
}
.step-text { font-size: 1.5rem; line-height: 1.5; max-width: 640px; }
.step-text .step-label { display: block; color: var(--accent); font-size: 1rem; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 0.6rem; font-family: 'Rajdhani', sans-serif; font-weight: 700; }
.step-nav { display: flex; gap: 0.8rem; padding: 0 1.25rem 1.5rem; }
.step-arrow {
  flex: 1; display: flex; align-items: center; justify-content: center;
  padding: 1.4rem; border-radius: 14px; border: 1px solid var(--border);
  background: var(--surface); color: var(--text); font-size: 2rem;
  -webkit-tap-highlight-color: transparent; user-select: none;
}
.step-arrow.disabled { opacity: 0.3; pointer-events: none; }
.step-arrow.primary { background: var(--accent); border-color: var(--accent); color: #1a0f04; }
.step-arrow .lbl { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; margin-left: 0.5rem; font-family: 'Rajdhani', sans-serif; font-weight: 700; }

@media (min-width: 820px) {
  body { font-size: 19px; }
  .wrap { max-width: 1000px; padding: 0 2rem 5rem; }
  header.top { padding: 2.2rem 0 1.5rem; }
  header.top h1 { font-size: 2rem; }
  .recipe-card { padding: 1.7rem 1.9rem; }
  .recipe-card h2 { font-size: 1.5rem; }
  .btn { font-size: 1.02rem; padding: 0.7rem 1.3rem; border-radius: 8px; }
  form.stack { max-width: 620px; gap: 1.2rem; }
  input, textarea { font-size: 1.05rem; padding: 0.7rem 0.85rem; }
  .step-text { font-size: 2rem; }
  .step-arrow { font-size: 2.6rem; padding: 2rem; }
}
"""

HEAD = (
    "<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<title>Cookbook</title>"
    "<link rel='icon' href=\"data:image/svg+xml,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
    "<text y='.9em' font-size='90'>%F0%9F%8D%B3</text></svg>\">"
    "<link rel='preconnect' href='https://fonts.googleapis.com'>"
    "<link href='https://fonts.googleapis.com/css2?family=Rajdhani:wght@600;700&family=Barlow:wght@400;500;600&display=swap' rel='stylesheet'>"
    f"<style>{CSS}</style>"
)


def page(title, body_html, back_href=None, back_label=None):
    back = f"<a class='back' href='{back_href}'>&larr; {html.escape(back_label or 'Back')}</a>" if back_href else "<span></span>"
    return (
        f"<!doctype html><html><head>{HEAD}</head><body><div class='wrap'>"
        f"<header class='top'><div class='brand'><span class='dot'></span><h1>{html.escape(title)}</h1></div>{back}</header>"
        f"{body_html}"
        f"</div></body></html>"
    )


def render_home():
    recipes = load_recipes()
    if not recipes:
        body = (
            f"<p class='empty'>No recipes yet. Drop a .md file into "
            f"<code>{html.escape(RECIPES_DIR)}</code>, or type one in.</p>"
            "<div class='actions'><a class='btn primary' href='/recipes/new'>+ New Recipe</a></div>"
        )
        return page("Cookbook", body)

    cards = []
    for r in recipes:
        n_steps = len(r["steps"])
        n_ing = sum(len(items) for _, items in r["ingredient_groups"])
        meta_bits = []
        if n_ing:
            meta_bits.append(f"{n_ing} ingredient{'s' if n_ing != 1 else ''}")
        if n_steps:
            meta_bits.append(f"{n_steps} step{'s' if n_steps != 1 else ''}")
        meta = " · ".join(meta_bits)
        slug_q = quote(r["slug"], safe="")
        cook_btn = f"<a class='btn primary' href='/recipe/{slug_q}/step/1'>Cook</a>" if n_steps else ""
        cards.append(
            f"<div class='recipe-card'>"
            f"<h2><a href='/recipe/{slug_q}'>{html.escape(r['title'])}</a></h2>"
            f"<div class='meta'>{meta}</div>"
            f"<div class='intro intro-block'>{r['intro_html']}</div>"
            f"<div class='actions'>"
            f"<a class='btn' href='/recipe/{slug_q}'>View</a>"
            f"{cook_btn}"
            f"</div></div>"
        )
    body = "".join(cards) + "<div class='actions'><a class='btn' href='/recipes/new'>+ New Recipe</a></div>"
    return page("Cookbook", body)


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
        f"<div class='actions'><a class='btn primary' href='/recipe/{slug_q}/step/1'>Start Cooking &rarr;</a></div>"
        if r["steps"] else ""
    )

    body = (
        f"<div class='intro-block'>{r['intro_html']}</div>"
        f"{cook_btn}"
        f"{'<div class=\"section-title\">Ingredients</div>' + ''.join(groups_html) if groups_html else ''}"
        f"{''.join(notes_html)}"
    )
    return page(r["title"], body, back_href="/", back_label="Cookbook")


def render_step(slug, n):
    r = load_recipe(slug)
    if r is None or not r["steps"]:
        return None
    total = len(r["steps"])
    n = max(1, min(n, total))
    step = r["steps"][n - 1]
    slug_q = quote(r["slug"], safe="")

    label_html = f"<span class='step-label'>{html.escape(step['label'])}</span>" if step["label"] else ""
    pct = round(n / total * 100)

    prev_href = f"/recipe/{slug_q}/step/{n - 1}"
    next_href = f"/recipe/{slug_q}/step/{n + 1}" if n < total else f"/recipe/{slug_q}"
    prev_disabled = " disabled" if n == 1 else ""
    next_label = "Done" if n == total else "Next"
    next_class = "primary" if n == total else ""

    body = (
        f"<div class='step-page' id='stepPage' data-prev='{prev_href}' data-next='{next_href}'>"
        f"<div class='step-top'>"
        f"<a class='back' href='/recipe/{slug_q}'>&larr; {html.escape(r['title'])}</a>"
        f"</div>"
        f"<div class='step-progress'>Step {n} of {total}</div>"
        f"<div class='step-bar'><div class='step-bar-fill' style='width:{pct}%'></div></div>"
        f"<div class='step-body'><div class='step-text'>{label_html}{step['html']}</div></div>"
        f"<div class='step-nav'>"
        f"<a class='step-arrow{prev_disabled}' href='{prev_href}' aria-label='Previous step'>&larr;<span class='lbl'>Prev</span></a>"
        f"<a class='step-arrow {next_class}' href='{next_href}' aria-label='{next_label} step'>"
        f"{'&#10003;' if n == total else '&rarr;'}<span class='lbl'>{next_label}</span></a>"
        f"</div></div>"
        "<script>"
        "(function(){"
        "var el=document.getElementById('stepPage');"
        "var sx=null, sy=null;"
        "el.addEventListener('touchstart',function(e){var t=e.changedTouches[0];sx=t.clientX;sy=t.clientY;},{passive:true});"
        "el.addEventListener('touchend',function(e){"
        "if(sx===null)return;"
        "var t=e.changedTouches[0];var dx=t.clientX-sx;var dy=t.clientY-sy;"
        "if(Math.abs(dx)>60 && Math.abs(dx)>Math.abs(dy)){"
        "window.location=dx<0?el.dataset.next:el.dataset.prev;"
        "}"
        "sx=null;sy=null;"
        "},{passive:true});"
        "document.addEventListener('keydown',function(e){"
        "if(e.key==='ArrowRight')window.location=el.dataset.next;"
        "if(e.key==='ArrowLeft')window.location=el.dataset.prev;"
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
        "<label>Description (optional)<textarea name='description' placeholder='A quick one-pan weeknight fried rice.'></textarea></label>"
        "<div class='row3'>"
        "<label>Servings<input name='servings' placeholder='4'></label>"
        "<label>Prep Time<input name='prep_time' placeholder='10 min'></label>"
        "<label>Cook Time<input name='cook_time' placeholder='15 min'></label>"
        "</div>"
        "<label>Ingredients<textarea name='ingredients' placeholder='2 cups cooked rice&#10;2 eggs&#10;2 tbsp soy sauce' required></textarea>"
        "<span class='hint'>One ingredient per line.</span></label>"
        "<label>Steps<textarea name='steps' placeholder='Heat oil in a wok over high heat&#10;Scramble the eggs, then set aside&#10;Add rice and stir-fry 3 minutes' required></textarea>"
        "<span class='hint'>One step per line — each line becomes its own page in the step-by-step view.</span></label>"
        "<label>Notes (optional)<textarea name='notes' placeholder='Day-old rice works best.'></textarea></label>"
        "<div class='actions'><button class='btn primary' type='submit'>Save recipe</button></div>"
        "</form>"
    )
    return page("New Recipe", body, back_href="/", back_label="Cookbook")


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
            self.send_header("WWW-Authenticate", 'Basic realm="Cookbook"')
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
        self._send_html(page("Not Found", "<p class='empty'>Nothing here.</p>", back_href="/", back_label="Cookbook"), status=404)

    def _read_form(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = parse_qs(raw)
        return {k: v[0] for k, v in parsed.items()}

    def do_GET(self):
        if not self._check_auth():
            return
        path = urlparse(self.path).path
        parts = [unquote(p) for p in path.split("/") if p]

        if not parts:
            return self._send_html(render_home())
        if parts == ["recipes", "new"]:
            return self._send_html(render_new_recipe_form())
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
                description=form.get("description", ""),
                servings=form.get("servings", ""),
                prep_time=form.get("prep_time", ""),
                cook_time=form.get("cook_time", ""),
                ingredients=form.get("ingredients", ""),
                steps=form.get("steps", ""),
                notes=form.get("notes", ""),
            )
            return self._redirect(f"/recipe/{quote(slug, safe='')}")

        self._not_found()


def main():
    os.makedirs(RECIPES_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Cookbook listening on 0.0.0.0:{PORT}")
    print(f"Recipes: {RECIPES_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
