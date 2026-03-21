#!/usr/bin/env python3
"""
zerodha-summary

Takes screenshots of specified pages on a website, compiles them into a PDF,
and emails the PDF to a given address.

Usage:
    zerodha-summary \
        --base-url https://zerodha.com/ \
        --pages https://zerodha.com/about/ https://zerodha.com/products/ \
        --receiver-email recipient@example.com \
        --sender-email yourname@gmail.com \
        --smtp-host smtp.gmail.com \
        --smtp-port 587 \
        --subject "Zerodha Screenshots" \
        --output-pdf zerodha_screenshots.pdf

    # For Gmail, use an App Password (Google Account → Security → App Passwords)
    # The program will securely prompt for the SMTP password if --password is not provided.

Dependencies:
    pip install zerodha-summary
    playwright install chromium
"""

# --- Standard library imports ---
import argparse        # Parses command-line arguments (--base-url, --receiver-email, etc.)
import configparser    # Reads config.ini so you don't have to pass args every time
import os              # Used to open the PDF on Windows (os.startfile)
import re              # Used to sanitize segment names into safe filenames
import subprocess      # Used to open the PDF in the system viewer (Linux/macOS)
import sys             # Used to detect the OS platform
import smtplib         # Connects to the SMTP server and sends the email
import tempfile        # Creates a temporary folder for PNG files (auto-cleaned up)
import getpass         # Prompts for SMTP password without showing it on screen
from datetime import date as _date
from email.mime.application import MIMEApplication  # Attaches the PDF file to the email
from email.mime.multipart import MIMEMultipart      # Builds a multipart email (text + attachment)
from email.mime.text import MIMEText                # Adds the plain-text body to the email
from pathlib import Path
from urllib.parse import urljoin, urlparse          # Resolves relative page paths to full URLs

# Default location for the config file (current working directory)
CONFIG_FILE = Path.cwd() / "config.ini"


def load_config() -> dict:
    """Read config.ini and return a flat dict of settings.

    Multi-line values (like 'pages') are returned as a list.
    Single-line values are returned as a plain string.
    Returns an empty dict if config.ini does not exist.
    """
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


# --- Third-party dependency checks ---
# These give a clear error message instead of a confusing ImportError if a
# package is missing.

try:
    from playwright.sync_api import sync_playwright  # Browser automation
except ImportError:
    raise SystemExit("Missing dependency: pip install playwright && playwright install chromium")

try:
    from PIL import Image  # Image processing (mode conversion before PDF creation)
except ImportError:
    raise SystemExit("Missing dependency: pip install pillow")

try:
    import img2pdf  # Lossless PNG → PDF conversion
except ImportError:
    raise SystemExit("Missing dependency: pip install img2pdf")


# ---------------------------------------------------------------------------
# Tradebook helpers
# ---------------------------------------------------------------------------

# All segment types available on the Zerodha tradebook page.
# The code skips any segment that doesn't appear in the page's <select> dropdown.
TRADEBOOK_SEGMENTS = [
    "Equity",
    "Futures & Options",
    "Currency",
    "Commodity",
    "Mutual funds",
    "Equity (external trades)",
    "MF (external trades)",
]


def _next_tradebook_page(page) -> bool:
    """Click the next-page button in tradebook pagination.

    Zerodha uses Bootstrap-style pagination where the active page marker is on
    the <li> element, not the <button>/<a> inside it. This function handles
    that pattern and falls back to button-level detection and arrow buttons.

    Returns True if navigated to next page, False if already on last page.
    """
    # page.evaluate() runs JavaScript directly in the browser. This is more
    # reliable than Playwright locators for Vue.js apps because it fires click
    # events the same way a real user would, triggering Vue's event handlers.
    result = page.evaluate("""() => {
        // Primary strategy: Zerodha uses Bootstrap pagination where <li class="active">
        // marks the current page. Walk forward through siblings to find the next
        // enabled page item and click its link.
        const activeLi = document.querySelector('li.active, li.page-item.active');
        if (activeLi) {
            let next = activeLi.nextElementSibling;
            while (next) {
                if (!next.classList.contains('disabled')) {
                    const btn = next.querySelector('button, a');
                    if (btn) { btn.click(); return true; }
                }
                next = next.nextElementSibling;
            }
            return false;
        }

        // Fallback: active class directly on button (some other pagination styles)
        const allBtns = Array.from(document.querySelectorAll('button'));
        const activeIdx = allBtns.findIndex(b => b.classList.contains('active'));
        if (activeIdx >= 0 && activeIdx + 1 < allBtns.length) {
            const next = allBtns[activeIdx + 1];
            if (!next.disabled) { next.click(); return true; }
        }

        // Fallback: look for a "next" arrow button (>, ›, », Next)
        for (const arrow of ['>', '\u203a', '\u00bb', 'Next']) {
            const btn = allBtns.find(b => b.innerText.trim() === arrow && !b.disabled);
            if (btn) { btn.click(); return true; }
        }

        return false;
    }""")
    return result


def _screenshot_tradebook(page, output_dir: Path, base_index: int) -> list[Path]:
    """Screenshot all pages of every tradebook segment for the last 3 months.

    For each segment (Equity, F&O, etc.):
      1. Select the segment from the dropdown
      2. Set the date range to the last 3 months
      3. Submit the report
      4. Screenshot every pagination page

    Args:
        page:        Playwright page object (already on the tradebook URL)
        output_dir:  Directory to save PNG files into
        base_index:  Starting number for PNG filenames (to keep global ordering)

    Returns:
        List of PNG file paths in the order they were captured.
    """
    today = _date.today()

    # Calculate the start of the 3-month window (first day of the month, 3 months ago)
    m, y = today.month - 3, today.year
    if m <= 0:  # Handle year rollover (e.g. Jan - 3 = October of previous year)
        m += 12
        y -= 1
    from_date = _date(y, m, 1)
    screenshots: list[Path] = []

    # Inspect all <select> elements on the page to find the segment dropdown.
    # We identify it by checking which one contains "Equity" in its options.
    selects = page.evaluate("""() =>
        Array.from(document.querySelectorAll('select')).map(s => ({
            options: Array.from(s.options).map(o => o.text)
        }))
    """)
    seg_select_idx = next(
        (i for i, s in enumerate(selects) if any("Equity" in o for o in s["options"])),
        None,
    )
    if seg_select_idx is None:
        raise RuntimeError("Could not find segment <select> on tradebook page.")

    available_options = selects[seg_select_idx]["options"]

    for seg_idx, segment in enumerate(TRADEBOOK_SEGMENTS):
        # Skip segments not offered by the account (e.g. Currency if not traded)
        if segment not in available_options:
            print(f"    Skipping '{segment}' — not in page options")
            continue

        print(f"    [{seg_idx+1}/{len(TRADEBOOK_SEGMENTS)}] Segment: {segment} ...")

        # 1. Select the segment via the native <select> dropdown
        page.locator("select").nth(seg_select_idx).select_option(label=segment)
        page.wait_for_timeout(400)

        # 2. Fill the date-range picker (mx-input is the vue-date-picker input class)
        #    Format: "YYYY-MM-DD ~ YYYY-MM-DD"
        date_range = f"{from_date} ~ {today}"
        inp = page.locator("input.mx-input").first
        inp.click()
        page.wait_for_timeout(300)
        inp.fill(date_range)
        inp.press("Enter")
        page.wait_for_timeout(500)

        # 3. Click the submit button (blue arrow) to load the report data
        page.locator('button.btn-blue').click()
        page.wait_for_timeout(1500)
        try:
            # Wait for all network activity to settle (data fully loaded)
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            # Some pages keep a long-poll connection open and never hit networkidle;
            # fall back to a fixed delay to let the table render
            page.wait_for_timeout(3_000)

        # 4. Screenshot every pagination page for this segment
        # Convert segment name to a safe filename part (e.g. "Futures & Options" → "futures_options")
        slug   = re.sub(r"[^a-z0-9]+", "_", segment.lower()).strip("_")
        pg_num = 1
        while True:
            # Scroll to the bottom first to trigger any lazy-loaded content,
            # then scroll back to top so the full page is visible in the screenshot
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(400)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(300)

            # Zero-pad filenames so they sort correctly in the filesystem
            # Example: 002_tradebook_equity_p01.png
            png_path = output_dir / f"{base_index + seg_idx:03d}_tradebook_{slug}_p{pg_num:02d}.png"
            page.screenshot(path=str(png_path), full_page=True)
            screenshots.append(png_path)
            print(f"      Page {pg_num} captured.")

            # Try to advance to the next pagination page; stop when on the last page
            if not _next_tradebook_page(page):
                break
            page.wait_for_timeout(1_500)  # Wait for the new page data to render
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
    """Launch a visible browser, wait for manual login, then screenshot each page.

    The browser is shown (headless=False) so the user can log in manually.
    After the user presses ENTER, the script navigates to each page and
    captures a full-page PNG screenshot.

    Args:
        base_url:   The login/home URL to open first
        pages:      List of URLs or relative paths to screenshot
        output_dir: Folder to save PNG files
        width:      Viewport width in pixels (height is set to 900)

    Returns:
        Ordered list of PNG file paths.
    """
    screenshots: list[Path] = []

    with sync_playwright() as pw:
        # Launch a visible Chromium browser window so the user can log in
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": width, "height": 900},
            device_scale_factor=2,  # 2x pixel density for sharper/retina-quality screenshots
        )
        page = context.new_page()

        # Open the base URL and pause for manual login.
        # Automation libraries cannot handle 2FA or CAPTCHA, so the user logs in themselves.
        print(f"\n  Opening login page: {base_url}")
        page.goto(base_url, wait_until="load", timeout=30_000)
        print("  --> Please log in in the browser window.")
        print("  --> Press ENTER here once you are fully logged in...")
        input()
        print("  Login confirmed. Proceeding with screenshots.\n")

        for i, path_or_url in enumerate(pages):
            # Support both absolute URLs ("https://...") and relative paths ("/reports/...")
            if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
                url = path_or_url
            else:
                url = urljoin(base_url.rstrip("/") + "/", path_or_url.lstrip("/"))

            print(f"  [{i+1}/{len(pages)}] Navigating to {url} ...")
            try:
                # networkidle waits until there are no pending network requests for 500ms
                page.goto(url, wait_until="networkidle", timeout=30_000)
            except Exception:
                # SPAs (Single Page Apps like React/Vue) may never reach networkidle
                # because they keep WebSocket connections open; fall back to "load" + delay
                page.goto(url, wait_until="load", timeout=30_000)
                page.wait_for_timeout(3_000)

            if "reports/tradebook" in url:
                # Tradebook needs special handling: select segment, set dates, paginate
                shots = _screenshot_tradebook(page, output_dir, len(screenshots))
                screenshots.extend(shots)
            else:
                # For regular pages: scroll down to load lazy content, then back up
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
    """Convert a URL to a filesystem-safe filename slug.

    Example: "https://console.zerodha.com/portfolio/holdings"
             → "console.zerodha.com_portfolio_holdings"
    """
    parsed = urlparse(url)
    slug = (parsed.netloc + parsed.path).replace("/", "_").strip("_") or "home"
    return slug[:80]  # Keep filenames short to avoid OS path length limits


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------

def images_to_pdf(image_paths: list[Path], pdf_path: Path) -> None:
    """Combine a list of PNG screenshots into a single PDF (one image per page).

    img2pdf preserves the original pixel data without re-encoding (lossless),
    but it only accepts RGB images. Screenshots with transparency (RGBA) or
    palette mode (P) are converted to RGB first via Pillow.
    """
    converted: list[Path] = []
    for img_path in image_paths:
        with Image.open(img_path) as im:
            if im.mode in ("RGBA", "P"):
                # Save a temporary RGB copy alongside the original
                rgb_path = img_path.with_suffix(".rgb.png")
                im.convert("RGB").save(rgb_path)
                converted.append(rgb_path)
            else:
                converted.append(img_path)

    # Write all images into a single PDF file
    with open(pdf_path, "wb") as f:
        f.write(img2pdf.convert([str(p) for p in converted]))

    # Remove the temporary RGB copies created above
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
    sender_email: str,
    password: str,
    recipient: str,
    pdf_path: Path,
    subject: str,
    body: str,
    use_tls: bool = True,
) -> None:
    """Send an email with the PDF attached via SMTP.

    Builds a multipart MIME message (plain text body + PDF attachment) and
    delivers it using STARTTLS encryption (recommended for port 587).

    For Gmail, use an App Password from Google Account → Security → App Passwords
    instead of your regular account password.
    """
    # Build the email message structure
    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))  # Plain text body

    # Attach the PDF as a binary file
    with open(pdf_path, "rb") as f:
        attachment = MIMEApplication(f.read(), _subtype="pdf")
        attachment.add_header(
            "Content-Disposition", "attachment", filename=pdf_path.name
        )
        msg.attach(attachment)

    print(f"  Connecting to {smtp_host}:{smtp_port} ...")
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()  # Identify ourselves to the server
        if use_tls:
            server.starttls()  # Upgrade to encrypted connection
            server.ehlo()      # Re-identify after encryption handshake
        server.login(sender_email, password)
        server.sendmail(sender_email, recipient, msg.as_string())

    print(f"  Email sent to {recipient}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Define all command-line arguments the program accepts."""
    p = argparse.ArgumentParser(
        description="Screenshot website pages → PDF → email",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", default=str(CONFIG_FILE),
                   help=f"Path to config file (default: {CONFIG_FILE})")
    p.add_argument("--base-url", help="Login/base URL of the website")
    p.add_argument("--pages", nargs="+", help="Relative paths or absolute URLs to screenshot")
    p.add_argument("--receiver-email", help="Recipient email address")
    p.add_argument("--sender-email", help="Sender email address (used for SMTP login)")
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
    """Entry point: parse config + CLI args, then run the 3-step workflow."""
    args = build_parser().parse_args()

    # Load config file; CLI args take priority over config file values
    cfg = load_config()
    if args.config != str(CONFIG_FILE):
        # User pointed to a custom config file path
        custom = configparser.ConfigParser()
        custom.read(args.config)
        cfg = {k: v for k, v in (custom["settings"] if "settings" in custom else {}).items()}

    # Helper: return the CLI value if provided, otherwise fall back to config, then default
    def get(cli_val, key, fallback=None):
        return cli_val if cli_val is not None else cfg.get(key, fallback)

    base_url       = get(args.base_url,        "base_url")
    pages          = get(args.pages,           "pages")
    receiver_email = get(args.receiver_email,  "receiver_email")
    sender_email   = get(args.sender_email,    "sender_email")
    smtp_host      = get(args.smtp_host,       "smtp_host",  "smtp.gmail.com")
    smtp_port      = get(args.smtp_port,       "smtp_port",  587)
    subject        = get(args.subject,         "subject",    "Website Screenshots")
    body           = get(args.body,            "body",       "Please find the website screenshots attached as a PDF.")
    output_pdf     = get(args.output_pdf,      "output_pdf", "screenshots.pdf")
    width          = get(args.width,           "width",      1280)
    password       = get(args.password,        "password")
    no_tls         = args.no_tls or cfg.get("no_tls", "false").lower() == "true"

    # Normalize types: pages is always a list, port and width are always ints
    if isinstance(pages, str):
        pages = [pages]
    smtp_port = int(smtp_port)
    width = int(width)

    # Validate that all required fields are present before starting the browser
    missing = [name for name, val in [("base-url", base_url), ("pages", pages), ("receiver-email", receiver_email), ("sender-email", sender_email)] if not val]
    if missing:
        raise SystemExit(f"Missing required settings: {', '.join(missing)}. Set them in config.ini or pass as CLI args.")

    # Prompt for SMTP password securely if it wasn't supplied via config or CLI
    if not password:
        password = getpass.getpass(f"SMTP password for {sender_email}: ")

    # Use a temporary directory for PNG files so they are automatically cleaned up
    # after the script finishes (even if it crashes)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        pdf_path = Path(output_pdf).resolve()

        print(f"\n=== Step 1/3: Taking {len(pages)} screenshot(s) ===")
        screenshots = take_screenshots(base_url, pages, tmp_path, width=width)

        print(f"\n=== Step 2/3: Compiling PDF ===")
        images_to_pdf(screenshots, pdf_path)

        # Open the PDF so the user can review it before it is sent
        print(f"\n  Opening PDF for review: {pdf_path}")
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(pdf_path)])
        elif sys.platform == "win32":
            os.startfile(str(pdf_path))
        else:
            subprocess.Popen(["xdg-open", str(pdf_path)])

        # Require explicit confirmation before sending — avoids accidental emails
        confirm = input("\n  --> Review the PDF. Type 'yes' to send the email, anything else to cancel: ").strip().lower()
        if confirm != "yes":
            print("  Email cancelled. PDF saved at:", pdf_path)
            return

        print(f"\n=== Step 3/3: Sending email ===")
        send_email(
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            sender_email=sender_email,
            password=password,
            recipient=receiver_email,
            pdf_path=pdf_path,
            subject=subject,
            body=body,
            use_tls=not no_tls,
        )
        pdf_path.unlink(missing_ok=True)
        print(f"  Cleaned up: {pdf_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()
