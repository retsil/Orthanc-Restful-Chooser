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

"""An Application that clones whole series, marking the copy's description.

Where the cloneimage example gives every instance a fresh SOPInstanceUID and
leaves it in its original series, this one moves the copies into a series of
their own: each input series gets one new SeriesInstanceUID, shared by every
instance cloned from it, and a SeriesDescription with a suffix appended so the
copy can be told apart from the original in the same study.
"""

from pathlib import Path

from pydicom.dataset import Dataset

from ...base import Application, Host
from ...enums import State, Status
from ...orthanc_util import instanceUUIDFor
# A function, not the CloneInstances class: --module takes the first
# Application subclass a module holds, and an imported one would come first.
from ..cloneimage.application import cloneInstance

_MODULE_DIR = Path(__file__).resolve().parent

DEFAULT_SUFFIX = " (clone)"

# SeriesDescription is an LO, which DICOM caps at 64 characters.
_MAX_DESCRIPTION_LENGTH = 64


def suffixedDescription(description: str, suffix: str) -> str:
    """description with suffix appended, cut to fit an LO.

    The original is what gives way when the two are too long together: the
    suffix is the only thing marking the copy as a copy, so it is kept whole.
    A series with no description at all gets the suffix alone, without the
    leading space that would otherwise separate it from nothing.
    """
    if not description:
        return suffix.strip()[:_MAX_DESCRIPTION_LENGTH]
    room = max(0, _MAX_DESCRIPTION_LENGTH - len(suffix))
    return (description[:room] + suffix)[:_MAX_DESCRIPTION_LENGTH]


class CloneSeries(Application):
    """Clones each input series under a new SeriesInstanceUID and description."""

    __version__ = 1.0
    __icon__ = str(_MODULE_DIR / "cloneseries-icon.png")
    __description__ = ("Clones each input series as a new series, with a "
                       "suffix appended to its series description.")

    def __init__(self, host: Host, suffix: str = DEFAULT_SUFFIX) -> None:
        self._host = host
        self._suffix = suffix
        # {input SeriesInstanceUID: output SeriesInstanceUID}. The new UID is
        # drawn the first time a series is seen and reused for every later
        # instance of it, which is what keeps the clone one series rather than
        # one series per instance. Keyed by the DICOM UID rather than the
        # Orthanc series UUID because the dataset is what carries it, whatever
        # the host.
        self._seriesUIDs: dict[str, str] = {}
        self._outputs: dict[str, Dataset] = {}
        self._cloned = 0
        self._failed = 0
        self._host.notifyStateChanged(State.IDLE)

    @property
    def suffix(self) -> str:
        return self._suffix

    @property
    def seriesUIDs(self) -> dict[str, str]:
        """{input SeriesInstanceUID: output SeriesInstanceUID} so far."""
        return dict(self._seriesUIDs)

    # -- Application interface ---------------------------------------------

    def notifyInputAvailable(self, instanceUUID: str, mainTags: dict[str, object], lastData: bool) -> bool:
        self._host.notifyStateChanged(State.INPROGRESS)

        ds = self._host.getInputData(instanceUUID)
        # Either way the host has already reported why: it could not hand the
        # input over, or could not take the output.
        if len(ds) == 0 or not self._clone(ds, lastData):
            self._failed += 1
        else:
            self._cloned += 1

        if lastData:
            self._host.notifyStatus(
                Status.INFORMATION,
                f"cloned {self._cloned} instance(s) in {len(self._seriesUIDs)} "
                f"series, failed {self._failed}",
            )
            self._host.notifyStateChanged(State.COMPLETED)
        return True

    def getOutputData(self, instanceUUID: str) -> Dataset:
        return self._outputs.get(instanceUUID, Dataset())

    def bringApplicationToFront(self) -> bool:
        # Headless: nothing to raise.
        return False

    # -- internals ---------------------------------------------------------

    def _clone(self, source: Dataset, lastData: bool) -> bool:
        """Clone one instance and hand it over; whether the host took it."""
        # Every instance of the clone keeps its study but moves to the new
        # series, and needs a SOPInstanceUID of its own: the original is still
        # in the archive under the old one.
        ds = cloneInstance(source, self._host.generateUID())
        ds.SeriesInstanceUID = self._outputSeriesUID(str(source.get("SeriesInstanceUID", "")))
        ds.SeriesDescription = suffixedDescription(
            str(source.get("SeriesDescription", "")), self._suffix)

        outputUUID = instanceUUIDFor(ds)
        self._outputs[outputUUID] = ds
        try:
            return self._host.notifyOutputAvailable(outputUUID, lastData)
        finally:
            # The host takes the output from inside that call and never asks
            # again, so there is no reason to hold a whole series in memory.
            del self._outputs[outputUUID]

    def _outputSeriesUID(self, inputSeriesUID: str) -> str:
        """The clone's SeriesInstanceUID for an input series, drawn on first sight."""
        outputSeriesUID = self._seriesUIDs.get(inputSeriesUID)
        if outputSeriesUID is None:
            outputSeriesUID = self._host.generateUID()
            self._seriesUIDs[inputSeriesUID] = outputSeriesUID
            self._host.notifyStatus(
                Status.INFORMATION,
                f"series {inputSeriesUID or '(no SeriesInstanceUID)'} "
                f"is cloned as {outputSeriesUID}",
            )
        return outputSeriesUID
