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
import inspect

from .base import Application


def loadApplicationClass(moduleName: str) -> type[Application]:
    """Import moduleName and return the Application subclass it defines.

    Exactly one concrete subclass has to be found, or `__all__` has to narrow
    it to one: taking the first in the module would follow import order, and a
    module that imports an Application to build on would load that one.
    """
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
    # Abstract ones are dropped, Application itself among them; a class bound
    # to two names is still one candidate.
    candidates = list(dict.fromkeys(
        obj for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, Application)
        and not inspect.isabstract(obj)
    ))
    exported = getattr(module, "__all__", None)
    if len(candidates) > 1 and exported is not None:
        # Narrowed only if that leaves something; otherwise the ambiguity
        # below is the truer report than "defines no Application subclass".
        candidates = [obj for obj in candidates if obj.__name__ in exported] or candidates
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        names = ", ".join(sorted(obj.__name__ for obj in candidates))
        raise SystemExit(
            f"module {moduleName!r} holds more than one Application subclass "
            f"({names}); name one in its __all__")
    raise SystemExit(f"module {moduleName!r} defines no Application subclass")
