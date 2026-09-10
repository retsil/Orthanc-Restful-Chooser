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

"""The Host that serves DICOM instances out of an Orthanc server.

Front end agnostic: no UI lives here. Whatever picks the studies -- the curses
browser, a saved selection file, a command line -- builds an OrthancHost over
them and hands it the Application to feed.
"""

from .host import OrthancHost
from .staging import (DEFAULT_OUTPUT_CACHE_BYTES, StagedSeries, StagingHost,
                      formatByteSize, parseByteSize)

__all__ = [
    "DEFAULT_OUTPUT_CACHE_BYTES",
    "OrthancHost",
    "StagedSeries",
    "StagingHost",
    "formatByteSize",
    "parseByteSize",
]
