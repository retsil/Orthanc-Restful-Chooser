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

"""A percentage and a bar for a run over a known number of items."""

import sys
from typing import Optional, TextIO

_BAR_WIDTH = 30
# Rewriting one line only reads well on a terminal; a redirected run gets a
# whole line whenever the percentage passes another multiple of this.
_LOG_STEP = 10


class ProgressBar:
    """Renders `[####------]  40% (2/5)` once per completed item.

    On a terminal the line is rewritten in place, so the run keeps a single
    progress line; `clear()` takes the cursor off it again, for whoever wants
    to print something else in between. Anywhere else (a pipe, a log file)
    only every _LOG_STEP percent is reported, on a line of its own.

    A total of zero or less means nothing is known to progress through, and
    the bar stays silent rather than showing a percentage it cannot compute.
    """

    def __init__(
        self,
        total: int,
        stream: Optional[TextIO] = None,
        isTTY: Optional[bool] = None,
    ) -> None:
        self._total = total
        # Resolved at write time, so that a caller redirecting sys.stderr
        # after construction is still written to.
        self._stream = stream
        self._isTTY = isTTY
        self._done = 0
        # Which _LOG_STEP-wide bucket was last written on a non-terminal; 0
        # so that the first report is 10%, not the first item finished.
        self._loggedBucket = 0
        self._onBarLine = False

    @property
    def total(self) -> int:
        return self._total

    @property
    def percent(self) -> int:
        """How much of the run is done, 0-100; 100 when there is nothing to do."""
        if self._total <= 0:
            return 100
        return min(100, self._done * 100 // self._total)

    def advance(self, count: int = 1) -> None:
        """Count items as finished and redraw."""
        self._done += count
        self._render()

    def finish(self) -> None:
        """Complete the bar at 100% and leave the cursor on a fresh line."""
        self._done = self._total
        self._render(final=True)

    def clear(self) -> None:
        """Leave the bar's line, so other output does not land on top of it."""
        if self._onBarLine:
            self._write("\r\x1b[K")
            self._onBarLine = False

    # -- internals ---------------------------------------------------------

    def _render(self, final: bool = False) -> None:
        if self._total <= 0:
            return

        percent = self.percent
        filled = _BAR_WIDTH * percent // 100
        line = (f"[{'#' * filled}{'-' * (_BAR_WIDTH - filled)}] "
                f"{percent:3d}% ({min(self._done, self._total)}/{self._total})")

        if self._isTerminal():
            # \r\x1b[K: back to the start of the line and wipe what was there,
            # so a shorter line cannot leave the tail of a longer one behind.
            self._write("\r\x1b[K" + line + ("\n" if final else ""))
            self._onBarLine = not final
            return

        bucket = percent // _LOG_STEP
        if bucket > self._loggedBucket:
            self._loggedBucket = bucket
            self._write(line + "\n")

    def _isTerminal(self) -> bool:
        if self._isTTY is not None:
            return self._isTTY
        stream = self._resolveStream()
        return bool(getattr(stream, "isatty", lambda: False)())

    def _resolveStream(self) -> TextIO:
        return self._stream if self._stream is not None else sys.stderr

    def _write(self, text: str) -> None:
        stream = self._resolveStream()
        stream.write(text)
        stream.flush()
