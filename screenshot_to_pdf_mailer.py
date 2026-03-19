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
import os
import smtplib
import tempfile
import getpass
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin, urlparse

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
# Screenshot helpers
# ---------------------------------------------------------------------------

def take_screenshots(base_url: str, pages: list[str], output_dir: Path, width: int = 1280) -> list[Path]:
    """Launch a headless browser and screenshot each page. Returns ordered list of PNG paths."""
    screenshots: list[Path] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": width, "height": 900},
            device_scale_factor=2,          # retina-quality output
        )
        page = context.new_page()

        for i, path_or_url in enumerate(pages):
            # Support both absolute URLs and relative paths
            if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
                url = path_or_url
            else:
                url = urljoin(base_url.rstrip("/") + "/", path_or_url.lstrip("/"))

            print(f"  [{i+1}/{len(pages)}] Screenshotting {url} ...")
            try:
                page.goto(url, wait_until="networkidle", timeout=30_000)
            except Exception:
                # Fallback for SPAs that never reach networkidle (e.g. React apps)
                page.goto(url, wait_until="load", timeout=30_000)
                page.wait_for_timeout(3_000)   # allow JS to render

            # Scroll to bottom to trigger lazy-load, then back to top
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(500)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(300)

            slug = _url_to_slug(url)
            png_path = output_dir / f"{i:03d}_{slug}.png"
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
    p.add_argument("--base-url", required=True,
                   help="Base URL of the website, e.g. https://example.com")
    p.add_argument("--pages", nargs="+", required=True,
                   help="Relative paths or absolute URLs to screenshot, e.g. / /about /pricing")
    p.add_argument("--email", required=True,
                   help="Recipient email address")
    p.add_argument("--sender", required=True,
                   help="Sender email address (used for SMTP login)")
    p.add_argument("--smtp-host", default="smtp.gmail.com",
                   help="SMTP server hostname (default: smtp.gmail.com)")
    p.add_argument("--smtp-port", type=int, default=587,
                   help="SMTP port (default: 587 for STARTTLS)")
    p.add_argument("--no-tls", action="store_true",
                   help="Disable STARTTLS (not recommended)")
    p.add_argument("--subject", default="Website Screenshots",
                   help="Email subject line")
    p.add_argument("--body", default="Please find the website screenshots attached as a PDF.",
                   help="Email body text")
    p.add_argument("--output-pdf", default="screenshots.pdf",
                   help="Output PDF filename (default: screenshots.pdf)")
    p.add_argument("--width", type=int, default=1280,
                   help="Browser viewport width in pixels (default: 1280)")
    p.add_argument("--password", default=None,
                   help="SMTP password (will prompt securely if not provided)")
    return p


def main() -> None:
    args = build_parser().parse_args()

    # Prompt for password if not supplied via flag (avoid leaking in shell history)
    password = args.password or getpass.getpass(f"SMTP password for {args.sender}: ")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        pdf_path = Path(args.output_pdf).resolve()

        print(f"\n=== Step 1/3: Taking {len(args.pages)} screenshot(s) ===")
        screenshots = take_screenshots(args.base_url, args.pages, tmp_path, width=args.width)

        print(f"\n=== Step 2/3: Compiling PDF ===")
        images_to_pdf(screenshots, pdf_path)

        print(f"\n=== Step 3/3: Sending email ===")
        send_email(
            smtp_host=args.smtp_host,
            smtp_port=args.smtp_port,
            sender=args.sender,
            password=password,
            recipient=args.email,
            pdf_path=pdf_path,
            subject=args.subject,
            body=args.body,
            use_tls=not args.no_tls,
        )

    print("\nDone.")


if __name__ == "__main__":
    main()
