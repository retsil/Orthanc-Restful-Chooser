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

"""A WG23 Host backed by an Orthanc server, fed by the curses study browser.

The studies to work on come from `browser`: either interactively, through the
same curses selection UI (`fromBrowser`), or from a selection previously
written with the browser's --save-selection (`fromSelectionFile`).

Selected studies are expanded into their instances; each instance keeps its
Orthanc identifier as its native object UUID and its main tags are collected
from the patient, study, series and instance levels. Instance data is pulled
from Orthanc only when the Application asks for it, and output datasets are
uploaded back into Orthanc (and/or written to disk).
"""

import curses
import io
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

import pydicom
from pydicom.dataset import Dataset
from pydicom.uid import generate_uid
from pyorthanc import Orthanc

from ..base import Application, Host
from ..enums import State, Status
from . import browser


class OrthancHost(Host):
    """Host serving DICOM instances of browser-selected studies from Orthanc."""

    def __init__(
        self,
        client: Orthanc,
        studyUUIDs: Iterable[str],
        outputDir: Optional[Path] = None,
        tmpDir: Optional[Path] = None,
        uploadOutputs: bool = True,
    ) -> None:
        self._client = client
        self._studyUUIDs = list(studyUUIDs)
        self._outputDir = outputDir
        if self._outputDir is not None:
            self._outputDir.mkdir(parents=True, exist_ok=True)
        if tmpDir is not None:
            self._tmpDir = tmpDir
            self._tmpDir.mkdir(parents=True, exist_ok=True)
        else:
            self._tmpDir = Path(tempfile.mkdtemp(prefix="orthanc-host-"))
        self._uploadOutputs = uploadOutputs
        self._app: Optional[Application] = None
        # Orthanc instance UUID -> main tags, in study selection order. Filled
        # in lazily by _loadInstances() so constructing a Host is cheap.
        self._instances: Optional[Dict[str, dict]] = None
        # Every (Status, text) reported so far, for a UI to display.
        self.messages: List[tuple] = []

    # -- construction from a browser selection ---------------------------

    @classmethod
    def fromSelectionFile(cls, client: Orthanc, path: str, **kwargs) -> "OrthancHost":
        """Build a Host from a browser --save-selection JSON file."""
        _criteria, studyUUIDs = browser.load_selection(path)
        return cls(client, sorted(studyUUIDs), **kwargs)

    @classmethod
    def fromBrowser(
        cls,
        client: Orthanc,
        criteria: Optional[Dict[str, Optional[str]]] = None,
        preselected: Optional[Set[str]] = None,
        **kwargs,
    ) -> Optional["OrthancHost"]:
        """Run the browser's curses picker; None if the user cancelled it.

        `criteria` has the shape returned by browser.build_criteria(); when it
        is None every study on the server is offered.
        """
        records = browser.fetch_all_studies(client)
        if criteria is not None:
            records = [r for r in records if browser.matches(r, criteria)]

        selected = curses.wrapper(browser.run_curses_ui, records, preselected or set())
        if selected is None:
            return None
        # Keep the browser's row order rather than the set's arbitrary one.
        return cls(client, [r.uid for r in records if r.uid in selected], **kwargs)

    # -- selected instances ----------------------------------------------

    @property
    def studyUUIDs(self) -> List[str]:
        """Orthanc identifiers of the studies selected in the browser."""
        return list(self._studyUUIDs)

    @property
    def instanceUUIDs(self) -> List[str]:
        """Orthanc identifiers of every instance in the selected studies."""
        return list(self._loadInstances().keys())

    def getMainTags(self, instanceUUID: str) -> Dict[str, object]:
        """Patient, study, series and instance main tags for one instance."""
        return dict(self._loadInstances().get(instanceUUID, {}))

    def setApplication(self, app: Application) -> None:
        self._app = app

    def sendInputs(self) -> bool:
        """Offer every selected instance to the Application, in order.

        Stops early if the Application refuses an input, as it may do to
        cancel processing; returns whether all inputs were accepted.
        """
        if self._app is None:
            self.notifyStatus(Status.ERROR, "no application registered")
            return False

        uuids = self.instanceUUIDs
        if not uuids:
            self.notifyStatus(Status.WARNING, "selection contains no instances")
            return False

        for i, uuid in enumerate(uuids):
            lastData = i == len(uuids) - 1
            if not self._app.notifyInputAvailable(uuid, self.getMainTags(uuid), lastData):
                self.notifyStatus(Status.WARNING, f"application refused input {uuid}")
                return False
        return True

    # -- Host interface ---------------------------------------------------

    def getAvailableScreen(self) -> tuple[int, int, int, int]:
        # Terminal-hosted: report a nominal virtual screen.
        return (0, 0, 800, 600)

    def getTmpDir(self) -> Path:
        return self._tmpDir

    def generateUID(self) -> str:
        return str(generate_uid())

    def getInputData(self, instanceUUID: str) -> Dataset:
        if instanceUUID not in self._loadInstances():
            self.notifyStatus(Status.ERROR, f"{instanceUUID} is not a selected instance")
            return Dataset()
        try:
            data = self._client.get_instances_id_file(instanceUUID)
        except Exception as exc:  # noqa: BLE001 - surface any HTTP error plainly
            self.notifyStatus(Status.ERROR, f"could not download {instanceUUID}: {exc}")
            return Dataset()
        return pydicom.dcmread(io.BytesIO(data))

    def notifyOutputAvailable(self, instanceUUID: str, mainTags: dict[str, object], lastData: bool) -> bool:
        if self._app is None:
            self.notifyStatus(Status.ERROR, "no application registered")
            return False

        ds = self._app.getOutputData(instanceUUID)
        buffer = io.BytesIO()
        try:
            pydicom.dcmwrite(buffer, ds, enforce_file_format=True)
        except Exception as exc:  # noqa: BLE001 - an unwritable dataset is the app's bug
            self.notifyStatus(Status.ERROR, f"could not encode output {instanceUUID}: {exc}")
            return False

        if self._outputDir is not None:
            outPath = self._outputDir / f"{instanceUUID}.dcm"
            outPath.write_bytes(buffer.getvalue())
            self.notifyStatus(Status.INFORMATION, f"wrote {outPath}")

        if self._uploadOutputs:
            try:
                stored = self._client.post_instances(buffer.getvalue())
            except Exception as exc:  # noqa: BLE001 - surface any HTTP error plainly
                self.notifyStatus(Status.ERROR, f"could not upload output {instanceUUID}: {exc}")
                return False
            storedUUID = stored.get("ID", "") if isinstance(stored, dict) else ""
            self.notifyStatus(Status.INFORMATION, f"uploaded output as {storedUUID or 'unknown UUID'}")

        return True

    def notifyStateChanged(self, value: State) -> None:
        print(f"[state] {value.name}", file=sys.stderr)

    def notifyStatus(self, value: Status, text: str) -> None:
        self.messages.append((value, text))
        print(f"[{value.name}] {text}", file=sys.stderr)

    # -- internals ---------------------------------------------------------

    def _loadInstances(self) -> Dict[str, dict]:
        """Expand the selected studies into {instance UUID: main tags}."""
        if self._instances is not None:
            return self._instances

        instances: Dict[str, dict] = {}
        for studyUUID in self._studyUUIDs:
            try:
                study = self._client.get_studies_id(studyUUID)
                seriesEntries = self._expand(
                    self._client.get_studies_id_series(studyUUID, params={"expand": ""}),
                    self._client.get_series_id,
                )
                instanceEntries = self._expand(
                    self._client.get_studies_id_instances(studyUUID, params={"expand": ""}),
                    self._client.get_instances_id,
                )
            except Exception as exc:  # noqa: BLE001 - surface any HTTP error plainly
                self.notifyStatus(Status.ERROR, f"could not read study {studyUUID}: {exc}")
                continue

            studyTags = {
                **study.get("PatientMainDicomTags", {}),
                **study.get("MainDicomTags", {}),
            }
            seriesTags = {
                entry["ID"]: entry.get("MainDicomTags", {})
                for entry in seriesEntries
            }
            for entry in instanceEntries:
                instances[entry["ID"]] = {
                    **studyTags,
                    **seriesTags.get(entry.get("ParentSeries", ""), {}),
                    **entry.get("MainDicomTags", {}),
                }

        self._instances = instances
        return instances

    @staticmethod
    def _expand(entries, fetch) -> List[dict]:
        """Normalise a child listing that may hold bare UUIDs into full records."""
        return [fetch(entry) if isinstance(entry, str) else entry for entry in entries]
