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

"""Reference Application that downloads matching series to a folder.

Only the Application is exported here; its command line lives in .cli and
imports pyorthanc, so that this module stays loadable with --module by a host
that has already made the connection.
"""

from .application import DownloadSeries
from .progress import ProgressBar

__all__ = [
    "DownloadSeries",
    "ProgressBar",
]
