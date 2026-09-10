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

"""The series of a study, loaded from an Orthanc server.

Front end agnostic, exactly as `studies` is: a browser that lets a user narrow
a study to some of its series calls `fetch_series()` for the studies that are
checked and shows what comes back. The rows carry Orthanc series identifiers,
which is what a selection file and `OrthancHost` both speak.

Series come back in the DICOM order `OrthancHost` offers their instances in --
SeriesNumber, then SeriesInstanceUID as a tie-breaker -- so a picker's rows and
the run that follows agree about what order a study is in.

Listing a study's series is one request per study, which is a great many of
them for a large selection, so `SeriesCache` keeps what it has already
fetched for as long as a front end holds it.
"""

import dataclasses
from typing import Callable, Dict, List, Optional

from pyorthanc import Orthanc

from .orthanc_util import asNumber


@dataclasses.dataclass
class SeriesRecord:
    uid: str  # Orthanc identifier (UUID) for the series
    study_uid: str  # Orthanc identifier of the study holding it
    series_instance_uid: str
    series_number: str
    modality: str
    description: str
    instance_count: int


def series_order(record: SeriesRecord) -> tuple:
    """Sort key putting series in the order their instances will be offered.

    The same key as `OrthancHost._instanceOrder` uses for the series half of
    its ordering, so the two cannot drift: number first, and the UID only as a
    tie-breaker so that two series sharing a number still come out in the same
    order on every run.
    """
    return (asNumber(record.series_number), record.series_instance_uid)


def fetch_series(client: Orthanc, study_uuid: str) -> List[SeriesRecord]:
    """Load the main tags of every series of one study, in DICOM order.

    One request, asking Orthanc to expand the listing rather than hand back
    bare identifiers -- and a second per series only for a server that hands
    them back anyway.
    """
    entries = client.get_studies_id_series(study_uuid, params={"expand": ""})
    records = [
        _record(client.get_series_id(entry) if isinstance(entry, str) else entry,
                study_uuid)
        for entry in entries
    ]
    records.sort(key=series_order)
    return records


def _record(entry: dict, study_uuid: str) -> SeriesRecord:
    tags = entry.get("MainDicomTags", {})
    return SeriesRecord(
        uid=entry["ID"],
        study_uid=study_uuid,
        series_instance_uid=str(tags.get("SeriesInstanceUID", "")),
        series_number=str(tags.get("SeriesNumber", "")),
        modality=str(tags.get("Modality", "")),
        description=str(tags.get("SeriesDescription", "")),
        # Orthanc lists a series' children in the same record, so the count is
        # already here and costs no request of its own.
        instance_count=len(entry.get("Instances", []) or []),
    )


class SeriesCache:
    """The series of the studies asked for so far, kept for the session.

    A front end that opens a series picker over a hundred checked studies pays
    a hundred requests for it; re-opening the picker, or opening it again after
    a study was un-checked and checked back, should pay none. Held by whatever
    is browsing rather than being module state, so nothing outlives it.

    It holds the client, unlike `fetch_series`, because it is what a picker is
    handed in place of one: a screen should be able to ask for the series of a
    study without also being the thing that knows how to talk to Orthanc.
    """

    def __init__(self, client: Orthanc) -> None:
        self._client = client
        self._byStudy: Dict[str, List[SeriesRecord]] = {}

    def get(self, study_uuid: str) -> List[SeriesRecord]:
        """The series of one study, fetching them the first time only."""
        if study_uuid not in self._byStudy:
            self._byStudy[study_uuid] = fetch_series(self._client, study_uuid)
        return list(self._byStudy[study_uuid])

    def known(self, study_uuid: str) -> Optional[List[SeriesRecord]]:
        """What is already held for a study, or None -- never a request.

        For a caller that can say something more useful when it happens to
        know a study's series but must not go and fetch them to find out: a
        list drawing one row per study cannot pay a request per row.
        """
        held = self._byStudy.get(study_uuid)
        return list(held) if held is not None else None

    def load(
        self,
        study_uuids: List[str],
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[str, List[SeriesRecord]]:
        """The series of several studies, in the order the studies were given.

        `progress(done, total)` is called before each study that still has to
        be fetched, so a front end can say what it is waiting for; a run served
        entirely from the cache calls it not at all.
        """
        loaded: Dict[str, List[SeriesRecord]] = {}
        for i, study_uuid in enumerate(study_uuids):
            if progress is not None and study_uuid not in self._byStudy:
                progress(i, len(study_uuids))
            loaded[study_uuid] = self.get(study_uuid)
        return loaded
