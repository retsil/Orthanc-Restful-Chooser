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

"""A WG23 Host backed by an Orthanc server.

The studies to work on are chosen elsewhere: a front end builds a Host over
the Orthanc identifiers it picked, or over a selection written earlier with a
browser's --save-selection (`fromSelectionFile`). Nothing here knows how they
were picked, so no user interface is involved.

Selected studies are expanded into their instances; each instance keeps its
Orthanc identifier as its native object UUID and its main tags are collected
from the patient, study, series and instance levels. They are offered in DICOM
order -- selection order across studies, then SeriesNumber, then
InstanceNumber -- because Orthanc lists the children of a study in no
documented order at all. Instance data is pulled from Orthanc only when the
Application asks for it -- and asking for one instance starts the download of
the next few on a small thread pool, so the request for those is usually
already in flight by the time it asks. Output datasets are uploaded back into
Orthanc (and/or written to disk).
"""

import io
import sys
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pydicom
from pydicom.dataset import Dataset
from pydicom.uid import generate_uid
from pyorthanc import Orthanc

from ..base import Application, Host
from ..enums import State, Status
from ..selection import load_selection


class OrthancHost(Host):
    """Host serving the DICOM instances of a set of Orthanc studies."""

    def __init__(
        self,
        client: Orthanc,
        studyUUIDs: Iterable[str],
        outputDir: Optional[Path] = None,
        tmpDir: Optional[Path] = None,
        uploadOutputs: bool = True,
        prefetchDepth: int = 2,
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
        # How many instances are downloaded ahead of the Application. Zero
        # waits for each download at the moment it is asked for, which is what
        # an Application sees anyway; this only decides how much work has
        # already been done by then, and how many datasets may be held at once.
        self._prefetchDepth = max(0, prefetchDepth)
        self._pool: Optional[ThreadPoolExecutor] = None
        # Downloads running ahead, {instance UUID: future}. Only ever read or
        # written from the thread the Application runs on.
        self._pending: Dict[str, Future] = {}
        self._app: Optional[Application] = None
        # Orthanc instance UUID -> main tags, in study selection order. Filled
        # in lazily by _loadInstances() so constructing a Host is cheap.
        self._instances: Optional[Dict[str, dict]] = None
        # The same instances as a list and as {UUID: place in it}, so that a
        # claim can find what comes after it without walking the dict.
        self._order: List[str] = []
        self._positions: Dict[str, int] = {}
        # Every (Status, text) reported so far, for a UI to display.
        self.messages: List[tuple] = []

    # -- construction from a saved selection ------------------------------

    @classmethod
    def fromSelectionFile(cls, client: Orthanc, path: str, **kwargs) -> "OrthancHost":
        """Build a Host from a browser --save-selection JSON file."""
        _criteria, studyUUIDs = load_selection(path)
        return cls(client, sorted(studyUUIDs), **kwargs)

    # -- selected instances ----------------------------------------------

    @property
    def studyUUIDs(self) -> List[str]:
        """Orthanc identifiers of the studies this Host serves."""
        return list(self._studyUUIDs)

    @property
    def instanceUUIDs(self) -> List[str]:
        """Orthanc identifiers of every instance in the selected studies.

        Also what an Application counts to show progress: the Host interface
        itself carries no total.
        """
        return list(self._loadInstances().keys())

    def getMainTags(self, instanceUUID: str) -> Dict[str, object]:
        """Patient, study, series and instance main tags for one instance."""
        return dict(self._loadInstances().get(instanceUUID, {}))

    def setApplication(self, app: Application) -> None:
        self._app = app

    def sendInputs(self) -> bool:
        """Offer every selected instance to the Application, in order.

        Whenever the Application takes an instance's data the next few start
        downloading, so its following getInputData() usually only has to wait
        for a request that is already in flight; see _prefetch().

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

        try:
            for i, uuid in enumerate(uuids):
                lastData = i == len(uuids) - 1
                accepted = self._app.notifyInputAvailable(
                    uuid, self.getMainTags(uuid), lastData)
                # Data the Application did not take is dropped as soon as it
                # has moved on, so the window is all that is ever held.
                self._discard(uuid)
                if not accepted:
                    self.notifyStatus(Status.WARNING, f"application refused input {uuid}")
                    return False
            return True
        finally:
            self.close()

    def close(self) -> None:
        """Stop prefetching: cancel what has not started, wait for what has.

        sendInputs() does this itself when it returns. It is only worth calling
        directly after an Application has gone on asking for data once the run
        was over, which leaves a pool running behind it.
        """
        for running in self._pending.values():
            running.cancel()
        self._pending.clear()
        if self._pool is not None:
            self._pool.shutdown(cancel_futures=True)
            self._pool = None

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

        # This one was wanted, so the ones after it are worth starting -- and
        # started before waiting here, so this download overlaps with them.
        self._prefetch(self._nextInstances(instanceUUID))
        running = self._pending.pop(instanceUUID, None)
        try:
            return running.result() if running is not None else self._download(instanceUUID)
        except Exception as exc:  # noqa: BLE001 - surface any HTTP error plainly
            # Reported here rather than where it was raised: a download that
            # failed ahead of time is only the Application's problem once it
            # asks for the data, and one it never asks for is nobody's.
            self.notifyStatus(Status.ERROR, f"could not download {instanceUUID}: {exc}")
            return Dataset()

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

    def _download(self, instanceUUID: str) -> Dataset:
        """Fetch and parse one instance; runs on a prefetch thread.

        Raises instead of reporting, so that whether an error is worth a status
        message is decided by getInputData() on the thread that asked for the
        data. It touches no Host state, which leaves the client -- an
        httpx.Client, and thread safe -- as the only thing shared with the
        thread driving sendInputs().
        """
        return pydicom.dcmread(io.BytesIO(self._client.get_instances_id_file(instanceUUID)))

    def _nextInstances(self, instanceUUID: str) -> List[str]:
        """The `prefetchDepth` instances offered after this one."""
        self._loadInstances()
        position = self._positions.get(instanceUUID)
        if position is None:
            return []
        return self._order[position + 1:position + 1 + self._prefetchDepth]

    def _prefetch(self, uuids: List[str]) -> None:
        """Start downloading instances the Application has not asked for yet.

        Only ever driven by a claim, never by an input merely being offered.
        An Application that filters on main tags -- as the download example
        does -- never asks for the data of most instances, and fetching those
        anyway would pull the whole selection over just to throw it away.
        Guessing from what it did take keeps that to `prefetchDepth` instances
        per run it skips, because instances arrive grouped by series rather
        than interleaved.

        Being driven by the claim rather than by sendInputs' position also
        serves an Application that reads every main tag first and only asks for
        the data afterwards, from the last input or after the run: the window
        follows it through whatever order it works in.
        """
        if not uuids or not self._prefetchDepth:
            return
        if self._pool is None:
            # Never more workers than the window, and never enough of them to
            # make a server's life hard; a deeper window then simply queues.
            self._pool = ThreadPoolExecutor(
                max_workers=min(self._prefetchDepth, 4),
                thread_name_prefix="orthanc-prefetch",
            )
        for uuid in uuids:
            if uuid not in self._pending:
                self._pending[uuid] = self._pool.submit(self._download, uuid)

    def _discard(self, instanceUUID: str) -> None:
        """Drop a download the Application turned out not to want."""
        running = self._pending.pop(instanceUUID, None)
        if running is not None:
            running.cancel()

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
            # Orthanc lists the children of a study in no documented order, so
            # they are put into DICOM order here: series by number, then the
            # instances within each series. Selected studies keep the order
            # they were selected in, which the loop above already gives.
            ordered = sorted(instanceEntries,
                             key=lambda entry: self._instanceOrder(entry, seriesTags))
            for entry in ordered:
                instances[entry["ID"]] = {
                    **studyTags,
                    **seriesTags.get(entry.get("ParentSeries", ""), {}),
                    **entry.get("MainDicomTags", {}),
                }

        self._instances = instances
        self._order = list(instances)
        self._positions = {uuid: i for i, uuid in enumerate(self._order)}
        return instances

    @staticmethod
    def _expand(entries, fetch) -> List[dict]:
        """Normalise a child listing that may hold bare UUIDs into full records."""
        return [fetch(entry) if isinstance(entry, str) else entry for entry in entries]

    @staticmethod
    def _instanceOrder(entry: dict, seriesTags: Dict[str, dict]) -> tuple:
        """Sort key placing an instance in its series, and in order within it.

        The UIDs are only tie-breakers, so that two series sharing a number --
        or two instances sharing one, as a localizer and its reconstruction
        can -- still come out in the same order on every run.
        """
        series = seriesTags.get(entry.get("ParentSeries", ""), {})
        tags = entry.get("MainDicomTags", {})
        return (
            OrthancHost._asNumber(series.get("SeriesNumber")),
            str(series.get("SeriesInstanceUID", "")),
            OrthancHost._asNumber(tags.get("InstanceNumber")),
            str(tags.get("SOPInstanceUID", "")),
        )

    @staticmethod
    def _asNumber(value: object) -> tuple:
        """An IS tag as a sortable number; anything unusable sorts last.

        These arrive as strings, so comparing them as written would put "10"
        before "2". A missing or malformed number cannot be guessed at, and
        pretending it is 0 would push those instances in front of the numbered
        ones instead of after them.
        """
        try:
            return (0, int(str(value).strip()))
        except (TypeError, ValueError):
            return (1, 0)
