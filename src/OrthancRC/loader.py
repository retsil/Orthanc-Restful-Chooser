# Copyright (C) 2026 retsil <https://github.com/retsil/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Loading an Application implementation named on a command line."""

import importlib

from .base import Application


def loadApplicationClass(moduleName: str) -> type[Application]:
    """Import moduleName and return the Application subclass it defines."""
    try:
        module = importlib.import_module(moduleName)
    except ImportError as exc:
        # Not on the path, or one of its own imports is missing.
        raise SystemExit(f"cannot import module {moduleName!r}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - the module itself is broken
        # Found and executed, but it raised: a syntax error, or a failure in
        # its import-time code. Show the type, as the message alone rarely
        # says what went wrong.
        raise SystemExit(
            f"module {moduleName!r} failed to import: {type(exc).__name__}: {exc}"
        ) from exc
    for obj in vars(module).values():
        if isinstance(obj, type) and issubclass(obj, Application) and obj is not Application:
            return obj
    raise SystemExit(f"module {moduleName!r} defines no Application subclass")
