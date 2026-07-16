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


