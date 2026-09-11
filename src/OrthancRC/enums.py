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

from enum import IntEnum

# Using IntEnum for compatibility with WG23

class State(IntEnum):
    IDLE = 0
    INPROGRESS = 1
    COMPLETED = 2
    SUSPENDED = 3
    CANCELED = 4
    EXIT = 5


class Status(IntEnum):
    INFORMATION = 0
    ERROR = 1
    WARNING = 2
    FATALERROR = 3


def asEnum(kind: type[IntEnum], value: object) -> tuple[IntEnum | None, str | None]:
    """value as a member of kind, and what was wrong with it if anything.

    An Application written against the WG23 numbers may pass a bare int, which
    has no .name for a Host to print. A number in range is converted and
    reported; one out of range comes back as None, for the Host to report as
    an error rather than raise inside the Application's call.
    """
    if isinstance(value, kind):
        return value, None
    try:
        member = kind(value)
    except ValueError:
        return None, f"{value!r} is not a {kind.__name__}"
    return member, (f"{kind.__name__} passed as bare {value!r}; "
                    f"use {kind.__name__}.{member.name}")


