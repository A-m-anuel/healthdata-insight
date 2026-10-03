"""Build docs/technical-report.pdf from docs/technical-report.md.

Usage: python scripts/build_report.py [--browser PATH]

Converts the Markdown to a print-styled HTML page (needs the ``markdown`` package) and prints it to PDF with
headless Chrome or Edge.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "technical-report.md"
TARGET = ROOT / "docs" / "technical-report.pdf"
BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "google-chrome", "chromium", "chromium-browser", "microsoft-edge",
)

CSS = """
@page { size: Letter; margin: 0.6in 0.65in; }
html { color-scheme: light; }
body { font-family: "Segoe UI", system-ui, -apple-system, sans-serif; font-size: 9.4pt; line-height: 1.38;
       color: #1b1b1b; background: #ffffff; }
h1 { font-size: 15pt; line-height: 1.2; margin: 0 0 2pt; color: #0d366b; }
h2 { font-size: 11pt; margin: 9pt 0 3pt; color: #0d366b; border-bottom: 1px solid #d6d6d6; padding-bottom: 1pt; }
p { margin: 3pt 0 4pt; }
ul, ol { margin: 2pt 0 4pt; padding-left: 16pt; }
li { margin: 1pt 0; }
blockquote { margin: 4pt 0 6pt; padding: 4pt 8pt; background: #f4f6f9; border-left: 3px solid #2a78d6; }
blockquote p { margin: 0; }
table { border-collapse: collapse; width: 100%; margin: 4pt 0 6pt; font-size: 8.6pt; }
th, td { border-bottom: 1px solid #e1e0d9; padding: 2.4pt 5pt; text-align: left; vertical-align: top; }
th { background: #f4f6f9; }
td:first-child { width: 31%; font-weight: 600; }
code { font-family: Consolas, monospace; font-size: 8.4pt; }
"""


def find_browser(explicit: str | None) -> str:
    for candidate in ([explicit] if explicit else []) + list(BROWSERS):
        if candidate and (Path(candidate).is_file() or shutil.which(candidate)):
            return candidate
    raise SystemExit("No Chrome/Edge found; pass --browser PATH.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser")
    args = parser.parse_args()

    body = markdown.markdown(SOURCE.read_text(encoding="utf-8"), extensions=["tables"])
    html = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>HealthData Insight - Technical "
            f"report</title><style>{CSS}</style></head><body>{body}</body></html>")
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "report.html"
        page.write_text(html, encoding="utf-8")
        subprocess.run([find_browser(args.browser), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                        "--disable-features=WebContentsForceDark,AutoDarkMode",
                        f"--user-data-dir={Path(tmp) / 'profile'}", f"--print-to-pdf={TARGET}", page.as_uri()],
                       check=True, capture_output=True, timeout=120)
    pages = TARGET.read_bytes().count(b"/Type /Page") - TARGET.read_bytes().count(b"/Type /Pages")
    print(f"wrote {TARGET.relative_to(ROOT)} ({pages} pages)")


if __name__ == "__main__":
    main()
