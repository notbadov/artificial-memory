# Third-party software in the client runtime

The source code in this repository has no third-party dependencies: the launcher uses only
Node.js itself, and the Python code uses the standard library and `cryptography`.

The **client runtime** is a different thing. It is the archive the launcher and the desktop
application download (about 30 MB): a standalone Python with three packages installed into it
and four files of this repository in `bootstrap/`. It is built by putting together published
binaries, unchanged; `adapters/client/runtime/sources.json` and
`adapters/client/runtime/requirements.lock` pin each of them by SHA-256. The licence texts
named below are inside every runtime archive, at the paths given.

Checked on 2026-10-13 against the runtime `20261011-28bfcfeb8bda-py3.12.14` (all three
platforms), by reading the archives.

## What the build adds

| Component | Version | Licence | Licence text in the archive |
|---|---|---|---|
| CPython, as built by [python-build-standalone](https://github.com/astral-sh/python-build-standalone) (release 20260929, `install_only_stripped`) | 3.12.14 | Python Software Foundation License Version 2, with the earlier BeOpen, CNRI and CWI licences and the Zero-Clause BSD licence for documentation code | macOS: `python/lib/python3.12/LICENSE.txt`; Windows: `python/LICENSE.txt` |
| [cryptography](https://github.com/pyca/cryptography) | 46.0.7 | Apache-2.0 OR BSD-3-Clause | `…/site-packages/cryptography-46.0.7.dist-info/licenses/` (`LICENSE`, `LICENSE.APACHE`, `LICENSE.BSD`) |
| [cffi](https://github.com/python-cffi/cffi) | 2.1.1 | MIT-0 (MIT No Attribution) | `…/site-packages/cffi-2.1.1.dist-info/licenses/LICENSE` |
| [pycparser](https://github.com/eliben/pycparser) | 3.0 | BSD-3-Clause | `…/site-packages/pycparser-3.0.dist-info/licenses/LICENSE` |

`…/site-packages` is `python/lib/python3.12/site-packages` on macOS and
`python/Lib/site-packages` on Windows.

## What comes inside those binaries

Found in the archives; the runtime's own code calls none of it directly.

| Component | Where | Licence |
|---|---|---|
| OpenSSL 3.5.6 | linked into the `cryptography` wheel (`cryptography/hazmat/bindings/_rust.*`) | Apache-2.0 |
| OpenSSL 3.5.9 | linked into CPython (macOS: `libpython3.12.dylib`; Windows: `libcrypto-3-x64.dll`, `libssl-3-x64.dll`) | Apache-2.0 |
| pip 26.2.1, which the Python build carries; the runtime never runs it | `…/site-packages/pip` | MIT; the licences of the libraries pip vendors are in `pip-26.2.1.dist-info/licenses/` |
| Tcl/Tk | Windows: `python/tcl`, `tcl86t.dll`, `tk86t.dll`; macOS: `libtcl9.0.dylib`, `libtcl9tk9.0.dylib` and their extensions | Tcl/Tk licence (BSD-style); on Windows the text is `python/tcl/tk8.6/license.terms` |
| Microsoft Visual C++ runtime | Windows: `python/vcruntime140.dll`, `python/vcruntime140_1.dll` | Microsoft's redistributable terms |
| SQLite, bzip2 1.0.8, Expat 2.8.5, libmpdec, libffi, zlib, XZ | linked into CPython or shipped as DLLs next to it | see below |
| libedit | linked into CPython on macOS | see below |

python-build-standalone states that it builds CPython against libedit and without the GDBM
module in order to keep GPL-licensed code (readline, GDBM) out of its distributions.

On Windows, `python/LICENSE.txt` carries a section "Additional Conditions for this Windows
binary build" with the terms of the libraries that build includes. The macOS archives of the
`install_only` kind carry CPython's licence only; the licences of the libraries linked into
them are published by python-build-standalone with its full distributions
(https://gregoryszorc.com/docs/python-build-standalone/main/running.html, "Licensing").

## How each licence was established

- **CPython, cryptography, cffi, pycparser, pip, Tcl/Tk on Windows**: from the licence files
  and the package metadata (`License-Expression`) inside the runtime archives themselves.
  For cryptography the metadata in the archive names only the licence files; the expression
  `Apache-2.0 OR BSD-3-Clause` is from its page on PyPI and agrees with those files.
- **The versions of OpenSSL, bzip2, Expat and SQLite's presence**: from version strings in the
  binaries. **OpenSSL's licence** (Apache-2.0 for the 3.x series): from `LICENSE.txt` of the
  OpenSSL repository.
- **python-build-standalone's own build code** is under the Mozilla Public License 2.0 (its
  repository's `LICENSE`); the runtime contains its output, not that code.
- **Not established here**: the exact licence texts for SQLite, bzip2, Expat, libmpdec, libffi,
  zlib, XZ, libedit and Tcl/Tk on macOS as they apply to these builds, and Microsoft's terms
  for the Visual C++ runtime files. They are listed so that nothing in the archive goes
  unnamed; for their terms see each project and python-build-standalone's documentation.
