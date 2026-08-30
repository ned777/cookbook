# Cookbook

Self-hosted recipe box: browse recipes, then cook through them one step
at a time — a big **Next** arrow, a big **Previous** arrow, or just swipe
on a phone.

Recipes are plain Markdown files sitting in a folder
(`~/Nextcloud/Documents/Cooking`). There's no database and no build step —
the page is rendered from whatever's in that folder on every request, so a
new `.md` file dropped in from any device (Nextcloud sync, upload, `scp`,
whatever) shows up the next time the page loads. New recipes can also be
typed straight into the app, which writes the same kind of Markdown file
back into that folder.

Same philosophy as this author's other self-hosted tools
([Vehicle Maintenance Record](https://github.com/ned777/vehicle-maintenance)
being the closest relative): a single Python file using only
`http.server`, nothing to `pip install`, nothing to break on a Python
update.

## Features

- **Recipe list** — every `.md` file directly in the Cooking folder shows
  up as a card with its ingredient/step counts and intro text.
- **Cooking mode** — a full-black, flashcard-style deck: one step per
  card, a big arrow on each side to slide forward/back, or just swipe
  (touch) / use the arrow keys (desktop). Cards slide in place client-side
  — no page reload between steps. The Android app also keeps the screen
  from sleeping the whole time you're on a step.
- **Type a recipe in** — a form for just title, steps (one per line — each
  becomes its own card), and a free-text summary for whatever else is
  worth knowing (ingredients, timing, notes). Saves as a Markdown file with
  the same shape the parser reads back.
- **Reads real recipe-site Markdown too** — the parser is heuristic, not
  strict: it copes with an `## Instructions` *or* `## How to Make It` *or*
  `## Preparation Instructions` heading, ingredients as a bullet list *or*
  a table, and ingredients split across a few headed groups (`Sauce`,
  `Beef`, `Noodles`, …) as long as they come before the instructions.
- A subfolder like `Original sources/` is ignored — only files directly in
  the Cooking folder are treated as recipes.

## Running it

Needs nothing but Python 3 — no `pip install`, no virtualenv.

1. Copy `local_secrets.py.example` to `local_secrets.py` and set your own
   `AUTH_USER`/`AUTH_PASS`. This file is gitignored; the server refuses to
   start with the real credentials committed anywhere.
2. Run it:
   ```sh
   python3 server.py
   ```
   Serves on `0.0.0.0:8092`, protected by HTTP Basic Auth.

Example `systemd --user` unit (no root needed):

```ini
# ~/.config/systemd/user/cookbook.service
[Unit]
Description=Cookbook server
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/this/repo
ExecStart=/usr/bin/python3 /path/to/this/repo/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```sh
loginctl enable-linger $USER   # so it survives logout, once
systemctl --user daemon-reload
systemctl --user enable --now cookbook.service
```

## Recipe format

```markdown
# Recipe Title

Optional summary — ingredients, servings, timing, notes, whatever's
worth knowing before you start.

## Instructions

1. Do the first thing.
2. Do the second thing.
```

Each numbered item under Instructions becomes its own card in cooking
mode. Recipes pasted or copied in from an actual recipe site tend to have
a proper `## Ingredients` list (or table) too, and those still render fine
on the recipe's overview page — the New Recipe form just doesn't bother
asking for one separately.

## Data

Recipes live in `~/Nextcloud/Documents/Cooking`, not in this repo —
they're your data, already synced by Nextcloud, not part of the app.

## Android app

`android/` is a thin native wrapper around the web app — a WebView pointed
at the server, same idea as
[Vehicle Maintenance Record](https://github.com/ned777/vehicle-maintenance)'s
Android client. It has no data or logic of its own; every screen is just
this same server rendering a page.

1. Open the `android/` folder in Android Studio (or build from the
   command line with the Gradle install at
   `~/android-toolchain/gradle-8.7`).
2. Copy `android/secrets.properties.example` to
   `android/secrets.properties` and fill in your server's address and
   Basic Auth credentials. Gitignored — never committed.
3. Build & install the debug APK:
   ```sh
   cd android && gradle assembleDebug
   # APK lands at android/app/build/outputs/apk/debug/app-debug.apk
   ```
