# Third-party notices

Galgame MCP does not vendor third-party source code. Its runtime dependencies
are installed separately by the Python package manager and retain their own
licenses.

- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) — MIT
  License.
- [PyWinRT](https://github.com/pywinrt/pywinrt), used through the modular
  `winrt-Windows.*` packages for Windows OCR — MIT License.
- [Pillow](https://python-pillow.org/), an optional local crop/scale dependency
  for the second focused OCR pass. It is installed separately and is not
  vendored.
- Tesseract, when installed separately by a user, is an optional system OCR
  executable and is not distributed by this project.

Transitive dependencies installed by `mcp` are not copied into this
repository. Their license and notice files remain part of the environment in
which they are installed.
