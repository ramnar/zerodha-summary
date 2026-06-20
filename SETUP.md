
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