# Zerodha Portfolio Summary Mailer

Automates the process of logging into Zerodha, taking screenshots of key portfolio pages, compiling them into a PDF, and emailing the PDF to a recipient.

---

## What it does

1. Opens a visible browser and navigates to the Zerodha login page
2. Waits for you to log in manually (supports OTP / 2FA)
3. Screenshots each configured page
4. For the **Tradebook** page, iterates through every segment (Equity, F&O, Currency, etc.) with a 3-month date range, and captures all pagination pages per segment
5. Compiles all screenshots into a single PDF
6. Opens the PDF for your review
7. Sends the PDF by email only after you confirm

---

## Requirements

- Python 3.10+
- A Gmail account with an **App Password** (see setup below)

### Install dependencies

```bash
pip install playwright pillow img2pdf
playwright install chromium
```

---

## Configuration

The program reads settings from `config.ini`. A template is provided — copy it and fill in your details:

```bash
cp config.ini.template config.ini
```

Then edit `config.ini`:

```ini
[settings]
base_url    = https://kite.zerodha.com/          # Login page URL
pages       = https://console.zerodha.com/reports/tradebook
              https://console.zerodha.com/account/demat
              https://console.zerodha.com/portfolio/corporate-action-order-window
              https://coin.zerodha.com/dashboard

email       = recipient@example.com              # Who receives the PDF
sender      = yourname@gmail.com                 # Gmail address used to send
password    = your-gmail-app-password            # Gmail App Password (see below)
smtp_host   = smtp.gmail.com
smtp_port   = 587
no_tls      = false
subject     = Zerodha Portfolio Summary
body        = Please find the Zerodha screenshots attached as a PDF.
output_pdf  = zerodha_summary.pdf
width       = 1280                               # Browser viewport width in pixels
```

> **Note:** `config.ini` is excluded from git (via `.gitignore`) to protect your credentials. Never commit it.

### Gmail App Password setup

Gmail requires an App Password when 2-Step Verification is enabled:

1. Go to [Google Account → Security](https://myaccount.google.com/security)
2. Under **How you sign in to Google**, click **2-Step Verification**
3. Scroll to the bottom and click **App passwords**
4. Create a new app password (e.g. name it "Zerodha Mailer")
5. Copy the 16-character password (spaces are optional) into `config.ini` under `password`

### Pages configuration

- Each URL goes on its own line, indented under `pages =`
- The **Tradebook** page (`/reports/tradebook`) is handled specially: the program automatically iterates all segments and date ranges
- All other pages are screenshotted as-is (full page)

---

## Running the program

```bash
python3 screenshot_to_pdf_mailer.py
```

### What happens step by step

| Step | What you see |
|------|-------------|
| 1 | Browser opens to `base_url` (Zerodha login) |
| 2 | Log in with your credentials + OTP |
| 3 | Press **Enter** in the terminal to confirm login |
| 4 | Program navigates to each page and takes screenshots |
| 5 | For Tradebook: selects each segment, sets Dec 1 – today date range, captures all pages |
| 6 | PDF is compiled and opened in your PDF viewer |
| 7 | Type **`yes`** in the terminal to send the email, or anything else to cancel |

### Overriding config from the command line

Any setting in `config.ini` can be overridden with a CLI argument:

```bash
python3 screenshot_to_pdf_mailer.py \
    --email other@example.com \
    --subject "March 2026 Summary" \
    --output-pdf march_2026.pdf
```

Use `--config` to point to a different config file:

```bash
python3 screenshot_to_pdf_mailer.py --config my_other_config.ini
```

---

## File structure

```
zerodha-summary/
├── screenshot_to_pdf_mailer.py   # Main program
├── config.ini.template           # Template — safe to commit
├── config.ini                    # Your local config — DO NOT commit
├── .gitignore                    # Excludes config.ini, *.pdf, *.png
└── README.md                     # This file
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Missing dependency` error | Run `pip install playwright pillow img2pdf` and `playwright install chromium` |
| Gmail login fails | Make sure you are using an **App Password**, not your regular Gmail password |
| Tradebook segment not found | Check the `[DOM] selects` line printed in the terminal — the segment name must match exactly |
| Pagination not detected | Check the `[pagination]` line printed in the terminal for the actual button classes |
| PDF not opening | Install a PDF viewer (`sudo apt install evince` on Ubuntu) |
