#!/usr/bin/env python3
"""Build the GitHub Pages copy of the web app into docs/.

Pages serves files, and this app has a Python API behind it, so the Pages copy runs in
static mode: `window.SANAD_STATIC` is set and every read comes from a committed snapshot
of the real API under docs/api/. The snapshots are captured by hand from a running
server, so they are real responses rather than fixtures somebody typed:

    uvicorn sanad.api.app:app --app-dir src --port 8099
    curl -s localhost:8099/api/config  > docs/api/config.json
    curl -s localhost:8099/api/ledger  > docs/api/ledger.json
    curl -s -X POST localhost:8099/api/runs/preview -H 'content-type: application/json' \
         -d @run.json > docs/api/preview.json

Then: python scripts/build_pages.py
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB, DOCS = ROOT / "web", ROOT / "docs"

BANNER = """<div class="static-note">
  <b>Static copy.</b> This is the app on GitHub Pages, so the Python API is not running.
  Every reading below is a committed snapshot of the real API taken on {date}, at Arc
  block {block}. The audit tab works in full, because auditing is a read. Settling needs
  the server: clone the repo and run
  <code>uvicorn sanad.api.app:app --app-dir src --port 8099</code>.
</div>"""

CSS = """
.static-note{margin:0 0 18px;padding:12px 14px;border:1px solid #d8b866;border-radius:8px;
  background:#fdf6e3;color:#5b4a12;font-size:13.5px;line-height:1.5}
.static-note code{background:#f3ead1;padding:1px 4px;border-radius:4px}
"""


def main() -> int:
    config = json.loads((DOCS / "api" / "config.json").read_text())
    stamp = (DOCS / "api" / "ledger.json").stat().st_mtime
    import datetime

    date = datetime.date.fromtimestamp(stamp).isoformat()
    block = f"{config['latestBlock']:,}"

    DOCS.mkdir(exist_ok=True)
    for name in ("styles.css", "app.js"):
        shutil.copyfile(WEB / name, DOCS / name)
    (DOCS / "styles.css").write_text((DOCS / "styles.css").read_text() + CSS)

    html = (WEB / "index.html").read_text()
    # Pages serves under /<repo>/, so absolute asset paths would 404.
    html = html.replace('href="/styles.css"', 'href="styles.css"')
    html = html.replace('src="/app.js"', 'src="app.js"')
    if "SANAD_STATIC" not in html:
        html = html.replace("<body>", "<body>\n<script>window.SANAD_STATIC = true;</script>", 1)
    html = re.sub(r"(<main>)", BANNER.format(date=date, block=block) + r"\n\1", html, count=1)
    (DOCS / "index.html").write_text(html)
    (DOCS / ".nojekyll").write_text("")

    listed = sorted(p.name for p in (DOCS / "api").glob("*.json"))
    print(f"built docs/ for Pages: snapshot {date}, block {block}, api files {listed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
