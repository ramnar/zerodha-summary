#!/usr/bin/env python3
"""
screenshot_to_pdf_mailer.py

Takes screenshots of specified pages on a website, compiles them into a PDF,
and emails the PDF to a given address.

Usage:
    python3 screenshot_to_pdf_mailer.py \
        --base-url https://zerodha.com/ \
        --pages https://zerodha.com/about/ https://zerodha.com/products/ \
        --email recipient@example.com \
        --sender yourname@gmail.com \
        --smtp-host smtp.gmail.com \
        --smtp-port 587 \
        --subject "Zerodha Screenshots" \
        --output-pdf zerodha_screenshots.pdf

    # For Gmail, use an App Password (Google Account → Security → App Passwords)
    # The program will securely prompt for the SMTP password if --password is not provided.

Dependencies:
    pip install playwright pillow img2pdf
    playwright install chromium
"""

import argparse
import configparser
import os
import re
import subprocess
import sys
import smtplib
import tempfile
import getpass
from datetime import date as _date
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin, urlparse

CONFIG_FILE = Path(__file__).parent / "config.ini"


def load_config() -> dict:
    """Read config.ini and return a flat dict of settings."""
    cfg = configparser.ConfigParser()
    if not CONFIG_FILE.exists():
        return {}
    cfg.read(CONFIG_FILE)
    section = cfg["settings"] if "settings" in cfg else {}
    result = {}
    for key, value in section.items():
        # Multi-line values (pages) become a list; others stay as strings
        lines = [ln.strip() for ln in value.strip().splitlines() if ln.strip()]
        result[key] = lines if len(lines) > 1 else (lines[0] if lines else "")
    return result

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    raise SystemExit("Missing dependency: pip install playwright && playwright install chromium")

try:
    from PIL import Image
except ImportError:
    raise SystemExit("Missing dependency: pip install pillow")

try:
    import img2pdf
except ImportError:
    raise SystemExit("Missing dependency: pip install img2pdf")


# ---------------------------------------------------------------------------
# Tradebook helpers
# ---------------------------------------------------------------------------

TRADEBOOK_SEGMENTS = [
    "Equity",
    "Futures & Options",
    "Currency",
    "Commodity",
    "Mutual funds",
    "Equity (external trades)",
    "MF (external trades)",
]


def _inspect_tradebook_dom(page) -> dict:
    """Return info about selects, inputs, and buttons on the tradebook page."""
    return page.evaluate("""() => {
        const selects = Array.from(document.querySelectorAll('select')).map(s => ({
            id: s.id, name: s.name, cls: s.className.substring(0, 80),
            options: Array.from(s.options).map(o => o.text)
        }));
        const inputs = Array.from(document.querySelectorAll('input')).slice(0, 8).map(i => ({
            type: i.type, id: i.id, name: i.name,
            cls: i.className.substring(0, 80), placeholder: i.placeholder, value: i.value
        }));
        const btns = Array.from(document.querySelectorAll('button')).slice(0, 8).map(b => ({
            text: b.innerText.trim().substring(0, 40), cls: b.className.substring(0, 80)
        }));
        return { selects, inputs, btns };
    }""")


def _next_tradebook_page(page) -> bool:
    """Click the next-page button in tradebook pagination.

    Returns True if navigated to next page, False if already on last page.
    Prints pagination DOM on first call to help debug selectors.
    """
    btns = page.evaluate("""() =>
        Array.from(document.querySelectorAll('button'))
            .map(b => ({text: b.innerText.trim(), cls: b.className, disabled: b.disabled}))
            .filter(b => b.text.length <= 4)   // keep short-text buttons (page numbers, arrows)
    """)
    print(f"      [pagination] {btns}")

    # Find the currently active page button then click the immediately following one
    active_idx = next((i for i, b in enumerate(btns) if 'active' in b['cls']), None)
    if active_idx is not None and active_idx + 1 < len(btns):
        candidate = btns[active_idx + 1]
        if not candidate['disabled']:
            # Use Playwright locator to click by exact text so Vue events fire
            page.locator(f"button:text-is('{candidate['text']}')").first.click()
            return True

    # Fallback: find a ">" / "›" next button that is enabled
    for arrow in ['>', '›', '»']:
        btn = page.locator(f"button:text-is('{arrow}')")
        if btn.count() > 0 and not btn.first.get_attribute('disabled'):
            btn.first.click()
            return True

    return False


def _screenshot_tradebook(page, output_dir: Path, base_index: int) -> list[Path]:
    """For every segment: set date range from Dec 1 (3 months back) to today, submit, screenshot."""
    today     = _date.today()
    m, y      = today.month - 3, today.year
    if m <= 0:
        m += 12
        y -= 1
    from_date = _date(y, m, 1)
    screenshots: list[Path] = []

    # --- Inspect DOM so we know exact selectors ---
    dom = _inspect_tradebook_dom(page)
    print("  [DOM] selects :", dom["selects"])
    print("  [DOM] inputs  :", dom["inputs"])
    print("  [DOM] buttons :", dom["btns"])

    # Find which <select> index contains segment options (has "Equity")
    seg_select_idx = next(
        (i for i, s in enumerate(dom["selects"]) if any("Equity" in o for o in s["options"])),
        None,
    )
    if seg_select_idx is None:
        raise RuntimeError(
            "Could not find segment <select> on tradebook page.\n"
            f"Selects found: {dom['selects']}"
        )

    available_options = dom["selects"][seg_select_idx]["options"]
    print(f"  Segment <select> index={seg_select_idx}, options={available_options}")

    for seg_idx, segment in enumerate(TRADEBOOK_SEGMENTS):
        if segment not in available_options:
            print(f"    Skipping '{segment}' — not in page options")
            continue

        print(f"    [{seg_idx+1}/{len(TRADEBOOK_SEGMENTS)}] Segment: {segment} ...")

        # 1. Select segment via native <select>
        page.locator("select").nth(seg_select_idx).select_option(label=segment)
        page.wait_for_timeout(400)

        # 2. Set date range directly in the input and press Enter to confirm
        date_range = f"{from_date} ~ {today}"
        inp = page.locator("input.mx-input").first
        inp.click()
        page.wait_for_timeout(300)
        inp.fill(date_range)
        inp.press("Enter")
        page.wait_for_timeout(500)

        # 3. Click the blue → arrow button to generate the report
        page.locator('button.btn-blue').click()
        page.wait_for_timeout(1500)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            page.wait_for_timeout(3_000)

        # 6. Screenshot every pagination page
        slug     = re.sub(r"[^a-z0-9]+", "_", segment.lower()).strip("_")
        pg_num   = 1
        while True:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(400)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(300)

            png_path = output_dir / f"{base_index + seg_idx:03d}_tradebook_{slug}_p{pg_num:02d}.png"
            page.screenshot(path=str(png_path), full_page=True)
            screenshots.append(png_path)
            print(f"      Page {pg_num} captured.")

            if not _next_tradebook_page(page):
                break
            page.wait_for_timeout(1_500)
            pg_num += 1

    return screenshots


# ---------------------------------------------------------------------------
# Screenshot helpers
# ---------------------------------------------------------------------------

def take_screenshots(
    base_url: str,
    pages: list[str],
    output_dir: Path,
    width: int = 1280,
) -> list[Path]:
    """Launch a visible browser, wait for login, then screenshot each page. Returns ordered list of PNG paths."""
    screenshots: list[Path] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": width, "height": 900},
            device_scale_factor=2,          # retina-quality output
        )
        page = context.new_page()

        # --- Manual login step ---
        print(f"\n  Opening login page: {base_url}")
        page.goto(base_url, wait_until="load", timeout=30_000)
        print("  --> Please log in in the browser window.")
        print("  --> Press ENTER here once you are fully logged in...")
        input()
        print("  Login confirmed. Proceeding with screenshots.\n")

        for i, path_or_url in enumerate(pages):
            # Support both absolute URLs and relative paths
            if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
                url = path_or_url
            else:
                url = urljoin(base_url.rstrip("/") + "/", path_or_url.lstrip("/"))

            print(f"  [{i+1}/{len(pages)}] Navigating to {url} ...")
            try:
                page.goto(url, wait_until="networkidle", timeout=30_000)
            except Exception:
                # Fallback for SPAs that never reach networkidle (e.g. React apps)
                page.goto(url, wait_until="load", timeout=30_000)
                page.wait_for_timeout(3_000)   # allow JS to render

            if "reports/tradebook" in url:
                # Special handling: iterate every segment with last-3-month range
                shots = _screenshot_tradebook(page, output_dir, len(screenshots))
                screenshots.extend(shots)
            else:
                # Standard: scroll to trigger lazy content, then screenshot
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(500)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(300)

                slug     = _url_to_slug(url)
                png_path = output_dir / f"{len(screenshots):03d}_{slug}.png"
                page.screenshot(path=str(png_path), full_page=True)
                screenshots.append(png_path)

        context.close()
        browser.close()

    return screenshots


def _url_to_slug(url: str) -> str:
    """Convert a URL to a safe filename slug."""
    parsed = urlparse(url)
    slug = (parsed.netloc + parsed.path).replace("/", "_").strip("_") or "home"
    return slug[:80]   # keep filenames short


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------

def images_to_pdf(image_paths: list[Path], pdf_path: Path) -> None:
    """Combine PNG screenshots into a single PDF, one page per screenshot."""
    # img2pdf works with RGB images; convert RGBA if needed
    converted: list[Path] = []
    for img_path in image_paths:
        with Image.open(img_path) as im:
            if im.mode in ("RGBA", "P"):
                rgb_path = img_path.with_suffix(".rgb.png")
                im.convert("RGB").save(rgb_path)
                converted.append(rgb_path)
            else:
                converted.append(img_path)

    with open(pdf_path, "wb") as f:
        f.write(img2pdf.convert([str(p) for p in converted]))

    # Clean up temporary RGB conversions
    for p in converted:
        if p.suffix == ".png" and ".rgb." in p.name:
            p.unlink(missing_ok=True)

    print(f"  PDF created: {pdf_path} ({pdf_path.stat().st_size // 1024} KB)")


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

def send_email(
    smtp_host: str,
    smtp_port: int,
    sender: str,
    password: str,
    recipient: str,
    pdf_path: Path,
    subject: str,
    body: str,
    use_tls: bool = True,
) -> None:
    """Send an email with the PDF attached."""
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with open(pdf_path, "rb") as f:
        attachment = MIMEApplication(f.read(), _subtype="pdf")
        attachment.add_header(
            "Content-Disposition", "attachment", filename=pdf_path.name
        )
        msg.attach(attachment)

    print(f"  Connecting to {smtp_host}:{smtp_port} ...")
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        if use_tls:
            server.starttls()
            server.ehlo()
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())

    print(f"  Email sent to {recipient}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Screenshot website pages → PDF → email",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", default=str(CONFIG_FILE),
                   help=f"Path to config file (default: {CONFIG_FILE})")
    p.add_argument("--base-url", help="Login/base URL of the website")
    p.add_argument("--pages", nargs="+", help="Relative paths or absolute URLs to screenshot")
    p.add_argument("--email", help="Recipient email address")
    p.add_argument("--sender", help="Sender email address (used for SMTP login)")
    p.add_argument("--smtp-host", help="SMTP server hostname (default: smtp.gmail.com)")
    p.add_argument("--smtp-port", type=int, help="SMTP port (default: 587)")
    p.add_argument("--no-tls", action="store_true", help="Disable STARTTLS (not recommended)")
    p.add_argument("--subject", help="Email subject line")
    p.add_argument("--body", help="Email body text")
    p.add_argument("--output-pdf", help="Output PDF filename")
    p.add_argument("--width", type=int, help="Browser viewport width in pixels")
    p.add_argument("--password", help="SMTP password (will prompt securely if not provided)")
    return p


def main() -> None:
    args = build_parser().parse_args()

    # Load config file; CLI args override config values
    cfg = load_config()
    if args.config != str(CONFIG_FILE):
        # User specified a custom config path
        custom = configparser.ConfigParser()
        custom.read(args.config)
        cfg = {k: v for k, v in (custom["settings"] if "settings" in custom else {}).items()}

    def get(cli_val, key, fallback=None):
        return cli_val if cli_val is not None else cfg.get(key, fallback)

    base_url   = get(args.base_url,    "base_url")
    pages      = get(args.pages,       "pages")
    email      = get(args.email,       "email")
    sender     = get(args.sender,      "sender")
    smtp_host  = get(args.smtp_host,   "smtp_host",  "smtp.gmail.com")
    smtp_port  = get(args.smtp_port,   "smtp_port",  587)
    subject    = get(args.subject,     "subject",    "Website Screenshots")
    body       = get(args.body,        "body",       "Please find the website screenshots attached as a PDF.")
    output_pdf = get(args.output_pdf,  "output_pdf", "screenshots.pdf")
    width      = get(args.width,       "width",      1280)
    password   = get(args.password,    "password")
    no_tls     = args.no_tls or cfg.get("no_tls", "false").lower() == "true"

    # pages from config is already a list; from CLI it's also a list
    if isinstance(pages, str):
        pages = [pages]
    smtp_port = int(smtp_port)
    width = int(width)

    # Validate required fields
    missing = [name for name, val in [("base-url", base_url), ("pages", pages), ("email", email), ("sender", sender)] if not val]
    if missing:
        raise SystemExit(f"Missing required settings: {', '.join(missing)}. Set them in config.ini or pass as CLI args.")

    # Prompt for password if not in config or CLI
    if not password:
        password = getpass.getpass(f"SMTP password for {sender}: ")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        pdf_path = Path(output_pdf).resolve()

        print(f"\n=== Step 1/3: Taking {len(pages)} screenshot(s) ===")
        screenshots = take_screenshots(base_url, pages, tmp_path, width=width)

        print(f"\n=== Step 2/3: Compiling PDF ===")
        images_to_pdf(screenshots, pdf_path)

        # Open the PDF in the system viewer and ask for confirmation
        print(f"\n  Opening PDF for review: {pdf_path}")
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(pdf_path)])
        elif sys.platform == "win32":
            os.startfile(str(pdf_path))
        else:
            subprocess.Popen(["xdg-open", str(pdf_path)])

        confirm = input("\n  --> Review the PDF. Type 'yes' to send the email, anything else to cancel: ").strip().lower()
        if confirm != "yes":
            print("  Email cancelled. PDF saved at:", pdf_path)
            return

        print(f"\n=== Step 3/3: Sending email ===")
        send_email(
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            sender=sender,
            password=password,
            recipient=email,
            pdf_path=pdf_path,
            subject=subject,
            body=body,
            use_tls=not no_tls,
        )

    print("\nDone.")


if __name__ == "__main__":
    main()
