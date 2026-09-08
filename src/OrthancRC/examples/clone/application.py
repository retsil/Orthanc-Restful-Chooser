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
from ...orthanc_util import (
    extractMainTags,
    instanceUUIDFor,
)
from ...enums import (
    State,
    Status,
)

_MODULE_DIR = Path(__file__).resolve().parent


class CloneInstances(Application):
    """A minimal Application: clones each input instance under a fresh UID."""
    __version__=1.0
    __icon__=str(_MODULE_DIR / "clone-icon.png")
    __description__="A minimal Application: clones each input instance under a fresh UID."

    def __init__(self, host: Host) -> None:
        self._host = host
        self._outputs: dict[str, Dataset] = {}
        self._host.notifyStateChanged(State.IDLE)

    def getOutputData(self, instanceUUID: str) -> Dataset:
        return self._outputs.get(instanceUUID, Dataset())

    def notifyInputAvailable(self, instanceUUID: str, mainTags: dict[str, object], lastData: bool) -> bool:
        self._host.notifyStateChanged(State.INPROGRESS)
        self._host.notifyStatus(Status.INFORMATION, f"received input {instanceUUID}")

        # Trivial "processing": copy the input dataset and give it a fresh
        # SOP Instance UID obtained from the host.
        ds = copy.deepcopy(self._host.getInputData(instanceUUID))
        newSopUID = self._host.generateUID()
        ds.SOPInstanceUID = newSopUID
        if getattr(ds, "file_meta", None) is not None:
            ds.file_meta.MediaStorageSOPInstanceUID = newSopUID
        outputUUID = instanceUUIDFor(ds)
        self._outputs[outputUUID] = ds
        outputTags = extractMainTags(ds)
        self._host.notifyOutputAvailable(outputUUID, outputTags, lastData)

        if lastData:
            self._host.notifyStateChanged(State.COMPLETED)
        return True

    def bringApplicationToFront(self) -> bool:
        # Headless: nothing to raise.
        return False
