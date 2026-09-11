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

"""An Application that saves the series matching a filter to a local folder.

The host offers every instance of the selected studies; this Application only
looks at the main tags that come with the offer, so an instance belonging to a
series that does not match the filter is never downloaded at all.
"""

from pathlib import Path
from typing import Optional

import pydicom
from pydicom.dataset import Dataset

from ...base import Application, Host
from ...enums import State, Status
from .progress import ProgressBar, Rate

_MODULE_DIR = Path(__file__).resolve().parent


def countInputs(host: Host) -> int:
    """How many instances the host is going to offer, or 0 if it cannot say.

    The WG23 Host interface carries no count -- an Application is only told
    which input is the last one -- so this asks the Orthanc host for the
    selection it is about to send, and settles for no percentage when the host
    keeps it to itself.
    """
    uuids = getattr(host, "instanceUUIDs", None)
    if uuids is None:
        return 0
    try:
        return len(uuids)
    except TypeError:
        return 0


class DownloadSeries(Application):
    """Downloads the series whose main tags match a UID/modality/description."""

    __version__ = 1.0
    __icon__ = str(_MODULE_DIR / "download-icon.png")
    __description__ = ("Downloads every series matching a series instance UID, "
                       "a modality and a series description into a target "
                       "folder.")

    def __init__(
        self,
        host: Host,
        matchSeriesDescription: str | None = None,
        matchModality: str | None = None,
        matchSeriesInstanceUID: str | None = None,
        targetFolder: Path | str | None = None,
        showProgress: bool = True,
    ) -> None:
        self._host = host
        # A series instance UID and a modality have to match exactly (CT is not
        # CTA); a description is a substring, as in --search-description. A UID
        # is compared as written: DICOM restricts it to digits and dots, so
        # there is no case to fold, only surrounding whitespace to strip.
        self._matchSeriesDescription = matchSeriesDescription
        self._matchModality = matchModality
        self._matchSeriesInstanceUID = matchSeriesInstanceUID
        # Without a target the host's temporary directory is the only place
        # this Application can be sure it may write into.
        self._targetFolder = (
            Path(targetFolder) if targetFolder is not None
            else self._host.getTmpDir() / "download"
        )
        # The rate is kept whether or not there is a bar to draw it on, so
        # that the closing summary can report one even with --no-progress.
        self._rate = Rate()
        # What the host said it would offer, and every input it did, so that
        # the closing summary can be checked against both.
        self._expected = countInputs(host)
        self._offered = 0
        # Progress counts every instance offered, matching or not: it tracks
        # the way through the selection, not the number of files written; the
        # rate beside it counts only the bytes that actually arrived.
        self._progress: Optional[ProgressBar] = (
            ProgressBar(self._expected, rate=self._rate) if showProgress
            else None
        )
        self._downloaded = 0
        self._skipped = 0
        self._failed = 0
        self._seriesFolders: dict[str, Path] = {}
        self._host.notifyStateChanged(State.IDLE)

    # -- what was written --------------------------------------------------

    @property
    def targetFolder(self) -> Path:
        return self._targetFolder

    @property
    def progress(self) -> Optional[ProgressBar]:
        """The bar reporting how far through the selection this run is."""
        return self._progress

    @property
    def rate(self) -> Rate:
        """The rate at which downloaded bytes have been arriving."""
        return self._rate

    @property
    def seriesFolders(self) -> dict[str, Path]:
        """{SeriesInstanceUID: folder} for every series downloaded so far."""
        return dict(self._seriesFolders)

    # -- filtering ---------------------------------------------------------

    def matches(self, mainTags: dict[str, object]) -> bool:
        """Whether an instance's main tags satisfy every match criterion.

        A criterion left unset matches everything, so an Application with
        none set downloads the whole selection.
        """
        if self._matchSeriesInstanceUID:
            seriesUID = str(mainTags.get("SeriesInstanceUID", ""))
            if seriesUID.strip() != self._matchSeriesInstanceUID.strip():
                return False
        if self._matchModality:
            modality = str(mainTags.get("Modality", ""))
            if modality.strip().upper() != self._matchModality.strip().upper():
                return False
        if self._matchSeriesDescription:
            description = str(mainTags.get("SeriesDescription", ""))
            if self._matchSeriesDescription.lower() not in description.lower():
                return False
        return True

    # -- Application interface ---------------------------------------------

    def notifyInputAvailable(self, instanceUUID: str, mainTags: dict[str, object], lastData: bool) -> bool:
        self._host.notifyStateChanged(State.INPROGRESS)
        self._offered += 1

        if self._progress is not None:
            # The status lines the download reports must not be written over
            # the bar, so step off its line first and redraw afterwards.
            self._progress.clear()

        if self.matches(mainTags):
            self._download(instanceUUID, mainTags)
        else:
            self._skipped += 1

        if self._progress is not None:
            self._progress.advance()
            if lastData:
                self._progress.finish()

        if lastData:
            accounted = self._downloaded + self._skipped + self._failed
            if accounted != self._offered or self._expected not in (0, self._offered):
                # Each input should land in exactly one of the three counts;
                # a host that cannot give a total reports 0 and is not held to it.
                self._host.notifyStatus(
                    Status.WARNING,
                    f"the counts do not add up: {accounted} accounted for, "
                    f"{self._offered} offered, {self._expected or 'unknown'} expected",
                )
            rate = self._rate.format()
            self._host.notifyStatus(
                Status.INFORMATION,
                f"downloaded {self._downloaded} instance(s) in "
                f"{len(self._seriesFolders)} series into {self._targetFolder}, "
                f"skipped {self._skipped}, failed {self._failed}"
                + (f", at {rate}" if rate else ""),
            )
            self._host.notifyStateChanged(State.COMPLETED)
        return True

    def getOutputData(self, instanceUUID: str) -> Dataset:
        # A download produces files, not native objects: nothing goes back to
        # the host, so this is never called with anything meaningful.
        return Dataset()

    def bringApplicationToFront(self) -> bool:
        # Headless: nothing to raise.
        return False

    # -- internals ---------------------------------------------------------

    def _download(self, instanceUUID: str, mainTags: dict[str, object]) -> None:
        ds = self._host.getInputData(instanceUUID)
        if len(ds) == 0:
            # The host has already reported why it could not hand the data over.
            self._failed += 1
            return

        folder = self._seriesFolder(instanceUUID, mainTags)
        folder.mkdir(parents=True, exist_ok=True)
        # The Orthanc instance UUID is the fallback name: unlike SOPInstanceUID
        # it is always known here, and it is unique per instance.
        sopInstanceUID = str(mainTags.get("SOPInstanceUID", "")) or instanceUUID
        path = folder / f"{sopInstanceUID}.dcm"
        if path.exists():
            # Two instances sharing a SOPInstanceUID, or a rerun into the same
            # target: either way, writing would lose a file without a word.
            self._host.notifyStatus(Status.ERROR, f"{path} already exists, not overwritten")
            self._failed += 1
            return
        try:
            pydicom.dcmwrite(str(path), ds, enforce_file_format=True)
        except Exception as exc:  # noqa: BLE001 - surface any write error plainly
            self._host.notifyStatus(Status.ERROR, f"could not write {path}: {exc}")
            self._failed += 1
            return

        self._downloaded += 1
        # What landed on disk is what came over the wire, and the file is the
        # one thing that can be measured without holding the bytes twice.
        try:
            self._rate.add(path.stat().st_size)
        except OSError:
            # A written file that cannot be stat'ed only costs the rate some
            # accuracy; it is not worth failing a download that succeeded.
            pass
        self._host.notifyStatus(Status.INFORMATION, f"wrote {path}")

    def _seriesFolder(self, instanceUUID: str, mainTags: dict[str, object]) -> Path:
        """The folder holding one instance's series, assigning it on first sight.

        The instances of a series all land directly in the target folder. A
        second matching series cannot share it, so it gets the next free
        sibling: <target>-1, then <target>-2, and so on, in the order the
        series are first seen.
        """
        seriesUID = str(mainTags.get("SeriesInstanceUID", "")) or instanceUUID
        folder = self._seriesFolders.get(seriesUID)
        if folder is None:
            index = len(self._seriesFolders)
            folder = (self._targetFolder if index == 0
                      else Path(f"{self._targetFolder}-{index}"))
            if folder.is_dir() and any(folder.iterdir()):
                # An earlier run's series would be mixed in with this one's.
                self._host.notifyStatus(
                    Status.WARNING, f"{folder} is not empty; series "
                                    f"{seriesUID} is being added to what is there")
            self._seriesFolders[seriesUID] = folder
        return folder
