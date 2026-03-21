# Zerodha Portfolio Summary

Logs into Zerodha, screenshots your portfolio pages, compiles them into a PDF, and emails it to you.

## Setup

**1. Install**

```bash
pip install zerodha-summary
playwright install chromium
```

**2. Configure**

```bash
curl -o config.ini https://raw.githubusercontent.com/ramnar/zerodha-summary/main/config.ini.template
```

Edit `config.ini` with your email addresses and Gmail App Password. See the template for all options.

> `config.ini` is gitignored — never commit it.

**3. Run**

```bash
zerodha-summary
```

The browser opens for you to log in. Once you press **Enter**, it takes screenshots, compiles the PDF, and asks for confirmation before sending the email.

---

## Gmail App Password

Gmail requires an App Password (not your regular password):

1. Go to [Google Account → Security](https://myaccount.google.com/security) → **2-Step Verification** → **App passwords**
2. Create one (e.g. "Zerodha Mailer") and paste it into `config.ini` under `password`

If `password` is omitted from `config.ini`, the program will prompt for it securely at runtime.

---

## Advanced

### CLI overrides

Any `config.ini` setting can be passed as a CLI argument:

```bash
zerodha-summary --receiver-email other@example.com --subject "March Summary" --output-pdf march.pdf
zerodha-summary --config my_other_config.ini
```

### Pages

- Add one URL per line under `pages =` in `config.ini`
- The **Tradebook** page (`/reports/tradebook`) is handled specially — the program automatically iterates all segments (Equity, F&O, Currency, etc.) over the last 3 months and captures every pagination page
- All other pages are screenshotted as-is

---

## Publishing to PyPI

**1. Install build tools**

```bash
pip install build twine
```

**2. Build the distribution**

```bash
python3 -m build
```

This creates `dist/zerodha_summary-<version>-py3-none-any.whl` and `dist/zerodha_summary-<version>.tar.gz`.

**3. Upload to PyPI**

```bash
twine upload dist/*
```

You will be prompted for your PyPI username and password (or use an API token as the password with `__token__` as the username).

To upload to TestPyPI first:

```bash
twine upload --repository testpypi dist/*
```

**4. Bump the version**

Update the `version` field in [pyproject.toml](pyproject.toml) before each release, then rebuild.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Missing dependency` error | Run `pip install .` and `playwright install chromium` |
| Gmail login fails | Use an **App Password**, not your regular Gmail password |
| PDF not opening | Install a PDF viewer (`sudo apt install evince` on Ubuntu) |
