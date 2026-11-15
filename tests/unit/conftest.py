"""Shared test plumbing.

Every service ships a package literally named `app`. That is fine inside a
container holding exactly one service, and a problem inside a pytest session
holding all of them: the first `import app.identity` binds `app` for the whole
process, and the next service's identically-named import silently hands back the
wrong package. Both ingest-api and vehicle-sim now have an `app/identity.py`, so
this is not hypothetical.

`import_service` loads a module with one service's directory at the front of
sys.path and the `app` module cache cleared, then puts both back. Order of test
files therefore does not matter, and a third service can join without anything
here changing.
"""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _app_modules() -> dict:
    return {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}


def import_service(service: str, module: str):
    """Import `module` as resolved from `services/<service>/`."""
    saved_path = list(sys.path)
    saved_modules = _app_modules()
    for name in saved_modules:
        del sys.modules[name]

    sys.path.insert(0, str(ROOT / "services" / service))
    try:
        return importlib.import_module(module)
    finally:
        sys.path[:] = saved_path
        for name in _app_modules():
            del sys.modules[name]
        sys.modules.update(saved_modules)
