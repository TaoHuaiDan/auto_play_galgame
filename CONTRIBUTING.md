# Contributing

Thanks for helping improve Galgame MCP.

## Development setup

Use Python 3.11 or newer. On Windows, install the development dependencies and
the native OCR backend with:

```powershell
py -3 -m pip install -e ".[dev,windows-ocr]"
```

The text parser and session store are useful on any platform that can run the
Python package. Desktop capture, window capture, keyboard/mouse input, and
Windows OCR are Windows-specific backends.

## Checks before a pull request

```powershell
py -3 -m compileall -q src
py -3 -m unittest discover -s tests -v
py -3 -m build
```

Do not commit game installations, save files, screenshots, OCR dumps, or
`.galgame_sessions` data. The repository ignores the generated session and
build directories by default.

When changing the MCP protocol surface, update the tool tests and README
examples together. Keep raw screenshots and OCR text local unless a user
explicitly asks for them to be returned.
