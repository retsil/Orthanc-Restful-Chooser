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

import copy
from pathlib import Path

from pydicom.dataset import Dataset

from ...base import Host, Application
from ...orthanc_util import instanceUUIDFor
from ...enums import (
    State,
    Status,
)

_MODULE_DIR = Path(__file__).resolve().parent


def cloneInstance(source: Dataset, sopInstanceUID: str) -> Dataset:
    """A deep copy of source under a new SOP Instance UID.

    The file meta carries the same UID and must be kept in step with it; a
    dataset built in memory rather than read from a file has no file meta.
    """
    ds = copy.deepcopy(source)
    ds.SOPInstanceUID = sopInstanceUID
    if getattr(ds, "file_meta", None) is not None:
        ds.file_meta.MediaStorageSOPInstanceUID = sopInstanceUID
    return ds


class CloneInstances(Application):
    """A minimal Application: clones each input instance under a fresh UID."""
    __version__=1.0
    __icon__=str(_MODULE_DIR / "cloneimage-icon.png")
    __description__="A minimal Application: clones each input instance under a fresh UID."

    def __init__(self, host: Host) -> None:
        self._host = host
        self._outputs: dict[str, Dataset] = {}
        self._refused = 0
        self._host.notifyStateChanged(State.IDLE)

    def getOutputData(self, instanceUUID: str) -> Dataset:
        return self._outputs.get(instanceUUID, Dataset())

    def notifyInputAvailable(self, instanceUUID: str, mainTags: dict[str, object], lastData: bool) -> bool:
        self._host.notifyStateChanged(State.INPROGRESS)
        self._host.notifyStatus(Status.INFORMATION, f"received input {instanceUUID}")

        # Trivial "processing": copy the input dataset and give it a fresh
        # SOP Instance UID obtained from the host.
        ds = cloneInstance(self._host.getInputData(instanceUUID), self._host.generateUID())
        outputUUID = instanceUUIDFor(ds)
        self._outputs[outputUUID] = ds
        try:
            # False is the host saying it could not take the output: the
            # reason is in its own status, but it should not look like success.
            if not self._host.notifyOutputAvailable(outputUUID, lastData):
                self._refused += 1
        finally:
            # The host takes the output from inside that call and never asks
            # again, so there is no reason to hold every clone in memory.
            del self._outputs[outputUUID]

        if lastData:
            if self._refused:
                self._host.notifyStatus(
                    Status.WARNING, f"the host did not take {self._refused} output(s)")
            self._host.notifyStateChanged(State.COMPLETED)
        return True

    def bringApplicationToFront(self) -> bool:
        # Headless: nothing to raise.
        return False
