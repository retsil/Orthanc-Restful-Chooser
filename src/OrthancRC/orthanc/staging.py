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

"""A Host that holds an Application's output back until someone accepts it.

StagingHost wraps another Host and intercepts exactly one call. Inputs, tmp
dir, status and everything else are forwarded untouched, so an Application
cannot tell the difference: its getOutputData() is called once, synchronously,
from inside its own notifyOutputAvailable(), exactly where it is called
without staging. What changes is only what the Host does next -- nothing, for
the moment, beyond writing the bytes down.

That the Application sees no change is the whole design, not a nicety. There
is no releaseData() in this interface, so nothing would tell an Application
when it may free an output it was asked to keep; a Host that deferred
getOutputData() until after the run would be asking for data every reasonable
Application has already dropped. So the Host takes the output while the call
is in its hands and takes responsibility for it afterwards: held in memory up
to `outputCacheBytes`, and spilled to a file in the wrapped Host's tmp dir
past that, since output that arrives faster than a person can review it must
go somewhere and a Python process is not it.

The review itself is not here. A front end asks for `stagedOutputs`, a table
of output series with an instance count each, puts whatever screen it likes in
front of a user, and calls `commitStagedOutputs()` with the ones to keep --
which is when, and only when, anything is written or uploaded. The terminal
front end's version of that screen lives in OrthancRC.curses; a Qt one would
put the same rows in a dialog, and neither needs a line of this file.
"""

import dataclasses
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from pydicom.dataset import Dataset

from ..base import Application, Host
from ..enums import State, Status
from .host import OrthancHost

# What an unconfigured StagingHost will hold in memory before it starts
# spilling. A 512x512 16-bit CT slice is about 512 KB encoded, so this is
# roughly 250 of them -- two or three typical series, which covers the runs a
# user is likely to sit and review. Anything with a large single frame,
# mammography or whole-slide, spills almost at once, and that is the right
# outcome: those are exactly the outputs that should not be accumulating in a
# Python process.
DEFAULT_OUTPUT_CACHE_BYTES = 128 * 1024 * 1024

_SIZE_SUFFIXES = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}


def parseByteSize(text: str) -> int:
    """A byte count with an optional K/M/G suffix, as bytes.

    Command line sugar for the one option here whose natural unit is not
    one-of-a-thing: "128M" is what anyone will actually want to type.
    """
    match = re.fullmatch(r"\s*(\d+)\s*([KMG]?)B?\s*", text.upper())
    if not match:
        raise ValueError(
            f"Invalid size {text!r}, expected a byte count with an optional "
            "K, M or G suffix, e.g. 128M")
    return int(match.group(1)) * _SIZE_SUFFIXES[match.group(2)]


def formatByteSize(count: int) -> str:
    """A byte count as `4.2 MiB`, the inverse of what parseByteSize takes.

    Binary units, the ones DICOM sizes are naturally in, and the same ones the
    download example reports its rate in.
    """
    for suffix, scale in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024)):
        if count >= scale:
            return f"{count / scale:.1f} {suffix}"
    return f"{count} B"


@dataclasses.dataclass(frozen=True)
class StagedSeries:
    """One row of the table a user reviews: an output series, and how much.

    `instanceCount` counts *output* instances, which is not the size of the
    input series that produced them -- an Application may emit one output per
    input, or fewer, or more -- and it counts instances spilled to disk as
    readily as ones still held in memory, since the runs where the table
    matters most are exactly the ones that spilled.

    `key` identifies the row when it is handed back to commitStagedOutputs()
    and means nothing else; treat it as opaque.
    """

    key: str
    seriesInstanceUID: str
    seriesNumber: str
    modality: str
    description: str
    instanceCount: int
    byteCount: int


@dataclasses.dataclass
class _StagedInstance:
    """One output instance, already encoded, waiting to be accepted."""

    instanceUUID: str
    seriesKey: str
    size: int
    data: Optional[bytes] = None  # held in memory, unless spilled
    path: Optional[Path] = None  # where it was spilled to, unless held

    def read(self) -> bytes:
        return self.data if self.data is not None else self.path.read_bytes()

    def drop(self) -> None:
        self.data = None
        if self.path is not None:
            self.path.unlink(missing_ok=True)
            self.path = None


class StagingHost(Host):
    """Wraps a Host, holding its output back for review.

    Forwards to the host it wraps rather than inheriting from it, so there is
    exactly one tmp dir, one message list and one prefetch pool between the
    two. Inheriting would give the staging half its own of each, and the
    symptom would be status messages going missing rather than anything that
    looked like a bug in staging.
    """

    def __init__(
        self,
        host: OrthancHost,
        outputCacheBytes: int = DEFAULT_OUTPUT_CACHE_BYTES,
    ) -> None:
        self._host = host
        # Zero is meaningful and spills everything, which is the only way to
        # exercise the spill path without producing 128 MB of output.
        self._outputCacheBytes = max(0, outputCacheBytes)
        self._app: Optional[Application] = None
        self._staged: List[_StagedInstance] = []
        # Rows in the order their first instance arrived, which is the order
        # the run produced them in.
        self._series: Dict[str, StagedSeries] = {}
        self._heldBytes = 0
        self._spilledBytes = 0
        self._spillDir: Optional[Path] = None

    # -- what a front end reviews -----------------------------------------

    @property
    def stagedOutputs(self) -> List[StagedSeries]:
        """The output series produced so far, in the order they were produced."""
        return list(self._series.values())

    @property
    def heldBytes(self) -> int:
        """Staged output currently in memory."""
        return self._heldBytes

    @property
    def spilledBytes(self) -> int:
        """Staged output currently on disk, because the budget was reached."""
        return self._spilledBytes

    def commitStagedOutputs(self, accepted: Iterable[str]) -> bool:
        """Write and upload the accepted series; drop everything either way.

        `accepted` holds the `key` of each StagedSeries to keep. A rejected
        series is never written or uploaded, and an empty `accepted` is a
        confirmed empty commit rather than an error -- the same rule as
        confirming an empty study selection in a browser.

        Returns whether every accepted instance was stored, the way
        sendInputs() returns whether every input was accepted.
        """
        wanted: Set[str] = set(accepted)
        unknown = sorted(wanted - set(self._series))
        if unknown:
            self.notifyStatus(
                Status.ERROR, f"no staged output series {unknown}")

        self.notifyStatus(
            Status.INFORMATION,
            f"staged {len(self._staged)} output instance(s) in "
            f"{len(self._series)} series: "
            f"{self._heldBytes} byte(s) held in memory, "
            f"{self._spilledBytes} spilled to disk")

        keeping = wanted & set(self._series)
        stored = 0
        ok = True
        try:
            for staged in self._staged:
                if staged.seriesKey not in keeping:
                    continue
                if not self._host.storeOutput(staged.instanceUUID, staged.read()):
                    ok = False
                    continue
                stored += 1
        finally:
            # Whether a series was kept or dropped, and whether storing it
            # worked, the review is over and must cost nothing once it is: the
            # spill files hold patient data and the buffers hold the rest.
            self.discardStagedOutputs()

        if keeping:
            self.notifyStatus(
                Status.INFORMATION,
                f"committed {stored} output instance(s) in {len(keeping)} series")
        else:
            # Not an error: rejecting everything is a decision the table is
            # there to let a user make, like confirming an empty selection.
            self.notifyStatus(
                Status.INFORMATION,
                "no output series accepted; nothing was written or uploaded")
        return ok

    def discardStagedOutputs(self) -> None:
        """Forget every staged output, deleting whatever was spilled.

        Safe to call twice, and worth calling on any path that ends a run
        without a review, so that no patient data is left in the tmp dir.
        """
        for staged in self._staged:
            staged.drop()
        self._staged = []
        self._series = {}
        self._heldBytes = 0
        self._spilledBytes = 0
        if self._spillDir is not None:
            # Only the directory this made, and only once its files are gone;
            # the tmp dir it sits in belongs to the wrapped host.
            self._spillDir.rmdir()
            self._spillDir = None

    # -- the one call that is intercepted ---------------------------------

    def notifyOutputAvailable(self, instanceUUID: str, lastData: bool) -> bool:
        if self._app is None:
            self.notifyStatus(Status.ERROR, "no application registered")
            return False

        # Taken and encoded here, inside the Application's own call, so that
        # an unencodable dataset is still reported by the call that produced
        # it and nothing is ever asked of the Application afterwards.
        ds = self._app.getOutputData(instanceUUID)
        data = self._host.encodeOutput(ds, instanceUUID)
        if data is None:
            return False

        staged = _StagedInstance(
            instanceUUID=instanceUUID,
            seriesKey=self._rowFor(instanceUUID, ds, len(data)),
            size=len(data),
        )
        if self._heldBytes + len(data) <= self._outputCacheBytes:
            staged.data = data
            self._heldBytes += len(data)
        else:
            staged.path = self._spill(instanceUUID, data)
            self._spilledBytes += len(data)
        self._staged.append(staged)
        return True

    # -- everything else is the wrapped host ------------------------------

    def setApplication(self, app: Application) -> None:
        self._app = app
        # Forwarded as well, so the wrapped host can drive the same
        # Application through sendInputs() below.
        self._host.setApplication(app)

    def sendInputs(self) -> bool:
        return self._host.sendInputs()

    def close(self) -> None:
        # Only the prefetch pool: staged output outlives the run by design,
        # and is dropped by commitStagedOutputs() or discardStagedOutputs().
        self._host.close()

    @property
    def messages(self) -> List[tuple]:
        return self._host.messages

    @property
    def instanceUUIDs(self) -> List[str]:
        return self._host.instanceUUIDs

    def getMainTags(self, instanceUUID: str) -> Dict[str, object]:
        return self._host.getMainTags(instanceUUID)

    def getAvailableScreen(self) -> tuple[int, int, int, int]:
        return self._host.getAvailableScreen()

    def getTmpDir(self) -> Path:
        return self._host.getTmpDir()

    def generateUID(self) -> str:
        return self._host.generateUID()

    def getInputData(self, instanceUUID: str) -> Dataset:
        return self._host.getInputData(instanceUUID)

    def notifyStateChanged(self, value: State) -> None:
        self._host.notifyStateChanged(value)

    def notifyStatus(self, value: Status, text: str) -> None:
        self._host.notifyStatus(value, text)

    # -- internals ---------------------------------------------------------

    def _rowFor(self, instanceUUID: str, ds: Dataset, size: int) -> str:
        """Find or start the table row this output instance belongs to.

        Grouped by SeriesInstanceUID, which is the one place in this design
        where a DICOM UID rather than an Orthanc identifier is the right key:
        an output instance has no Orthanc identity until it is uploaded, and
        the point of the table is to decide whether it ever will be.

        Read off the output itself, so the row describes what will actually be
        stored rather than what the Application said about it.
        """
        seriesUID = str(ds.get("SeriesInstanceUID", "") or "")
        # An output the Application gave no series gets a row to itself rather
        # than being dropped or lumped in with another: a row a user can
        # reject is fine, output vanishing from the table is not.
        key = seriesUID or f"<no SeriesInstanceUID: {instanceUUID}>"

        row = self._series.get(key)
        if row is None:
            self._series[key] = StagedSeries(
                key=key,
                seriesInstanceUID=seriesUID,
                seriesNumber=str(ds.get("SeriesNumber", "") or ""),
                modality=str(ds.get("Modality", "") or ""),
                description=str(ds.get("SeriesDescription", "") or ""),
                instanceCount=1,
                byteCount=size,
            )
        else:
            self._series[key] = dataclasses.replace(
                row,
                instanceCount=row.instanceCount + 1,
                byteCount=row.byteCount + size,
            )
        return key

    def _spill(self, instanceUUID: str, data: bytes) -> Path:
        """Put one output instance in the wrapped host's tmp dir.

        Its own subdirectory of that, so staged output is never confused with
        whatever the Application is using the tmp dir for.
        """
        if self._spillDir is None:
            self._spillDir = self._host.getTmpDir() / "staged-output"
            self._spillDir.mkdir(parents=True, exist_ok=True)
        path = self._spillDir / f"{instanceUUID}.dcm"
        path.write_bytes(data)
        return path
