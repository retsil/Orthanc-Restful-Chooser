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

"""Search criteria and the selection file shared by every study browser.

SEARCH_FIELDS is the one place the set of search criteria is spelled out: the
--search-* options a front end offers, the criteria dict a selection file
carries and the local filter in `studies.matches()` are all driven from it, so
a new criterion is added by adding a row to it.

The selection file format is

    {"criteria": {...},
     "selection_level": "study" | "series",
     "study_uids": [...],
     "series_uids": {"<study UUID>": [...]}}

of which only `criteria` and `study_uids` are ever required: a file written
before series sub-selection existed carries neither of the other two, and
means every series of every study it names, which is what it always meant.
`selection_level` is the file saying what it is, so no reader has to guess
from whether `series_uids` happens to be there, and the two must agree.

It is what a browser writes with --save-selection and what OrthancHost reads
back with fromSelectionFile(), so every front end must go through
save_selection() and load_selection() rather than writing the JSON by hand.
Both sides of that are strict: the file is the message of the simplest IPC
there is, and a reader that quietly repaired a contradictory one would hand
its caller a different set of instances than the sender described.
"""

import argparse
import dataclasses
import json
from typing import Callable, Dict, Optional, Set, Tuple

DATE_HELP = "YYYYMMDD or YYYY-MM-DD"


def normalize_date(value: Optional[str]) -> Optional[str]:
    """Turn 'YYYY-MM-DD' or 'YYYYMMDD' into DICOM DA ('YYYYMMDD')."""
    if value is None:
        return None
    digits = value.replace("-", "")
    if len(digits) != 8 or not digits.isdigit():
        raise ValueError(f"Invalid date {value!r}, expected YYYYMMDD or YYYY-MM-DD")
    return digits


def contains(value: str, wanted: str) -> bool:
    return wanted.lower() in value.lower()


def equals(value: str, wanted: str) -> bool:
    return value == wanted


def not_before(value: str, wanted: str) -> bool:
    # A study with no date is not known to be within a bounded range, so a
    # bound that is set rejects it rather than letting it through.
    return bool(value) and value >= wanted


def not_after(value: str, wanted: str) -> bool:
    return bool(value) and value <= wanted


@dataclasses.dataclass(frozen=True)
class SearchField:
    """One search criterion: what it is called, what it narrows, and how."""

    key: str  # its name in the criteria dict, and so in a selection file
    attr: str  # the studies.StudyRecord attribute it is compared against
    accepts: Callable[[str, str], bool]  # (record value, criterion) -> keep it?
    is_date: bool = False  # normalised to a DICOM DA before use

    @property
    def option(self) -> str:
        """The command line option carrying this criterion."""
        return "--search-" + self.key.replace("_", "-")

    @property
    def dest(self) -> str:
        """The argparse attribute `option` is parsed into."""
        return "search_" + self.key


# In the order the options are offered on a command line. Two criteria may
# narrow the same record attribute, as the study date bounds do.
SEARCH_FIELDS: Tuple[SearchField, ...] = (
    SearchField("patient_id", "patient_id", contains),
    SearchField("patient_surname", "patient_surname", contains),
    SearchField("patient_givenname", "patient_given_name", contains),
    SearchField("dob", "dob", equals, is_date=True),
    SearchField("study_after", "study_date", not_before, is_date=True),
    SearchField("study_before", "study_date", not_after, is_date=True),
    SearchField("accession", "accession", contains),
    SearchField("description", "description", contains),
)


def add_search_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the --search-* options every study browser shares."""
    for field in SEARCH_FIELDS:
        parser.add_argument(
            field.option, default=None,
            help=DATE_HELP if field.is_date else None,
        )


def build_criteria(args: argparse.Namespace) -> Dict[str, Optional[str]]:
    """Collect the --search-* options parsed into `args` into a criteria dict."""
    return {
        field.key: (normalize_date(getattr(args, field.dest)) if field.is_date
                    else getattr(args, field.dest))
        for field in SEARCH_FIELDS
    }


STUDY_LEVEL = "study"
SERIES_LEVEL = "series"
SELECTION_LEVELS = (STUDY_LEVEL, SERIES_LEVEL)


@dataclasses.dataclass(frozen=True)
class Selection:
    """What a selection file says: some studies, and maybe some of their series.

    `series_uids` holds Orthanc series identifiers, as `study_uids` holds
    Orthanc study identifiers -- not SeriesInstanceUIDs. An Orthanc identifier
    is a SHA-1 of the DICOM identity (see orthanc_util), so it is the same on
    every server holding that series and a file of them is portable to any
    Orthanc with the same studies, which is the only place it means anything.

    A study missing from `series_uids` means the whole study, whatever it
    holds when the file is read. A study present with a full list means those
    series, as the study stood when the user looked at it. Only the second
    records what was actually seen, so a browser that opened its series picker
    over a study writes the list out even when every series is checked.
    """

    criteria: Dict[str, Optional[str]]
    selectionLevel: str
    study_uids: Set[str]
    series_uids: Dict[str, Set[str]]

    @classmethod
    def of(
        cls,
        criteria: Dict[str, Optional[str]],
        uids: Set[str],
        series: Optional[Dict[str, Set[str]]] = None,
    ) -> "Selection":
        """Build a Selection, taking its level from whether series were picked."""
        series = {study: set(picked) for study, picked in (series or {}).items()}
        return cls(
            criteria=dict(criteria),
            selectionLevel=SERIES_LEVEL if series else STUDY_LEVEL,
            study_uids=set(uids),
            series_uids=series,
        )

    def seriesFor(self, studyUUID: str) -> Optional[Set[str]]:
        """The series wanted from one study, or None for all of them.

        The one place the "absent means the whole study" rule is applied, so
        that no caller has to remember it.
        """
        picked = self.series_uids.get(studyUUID)
        return set(picked) if picked is not None else None

    def asJSONData(self) -> Dict[str, object]:
        """The file's JSON, with every list sorted so a save is diffable."""
        data: Dict[str, object] = {
            "criteria": self.criteria,
            "selection_level": self.selectionLevel,
            "study_uids": sorted(self.study_uids),
        }
        # Omitted rather than written empty at study level, so a file with no
        # sub-selection stays exactly the file this format has always written.
        if self.series_uids:
            data["series_uids"] = {
                study: sorted(picked)
                for study, picked in sorted(self.series_uids.items())
            }
        return data


def save_selection(
    path: str,
    criteria,
    uids: Optional[Set[str]] = None,
    series: Optional[Dict[str, Set[str]]] = None,
) -> None:
    """Write a selection file, from a Selection or from its parts.

    save_selection(path, selection) and save_selection(path, criteria, uids)
    are both accepted; the second grows an optional {study UUID: series UUIDs}
    third argument for a sub-selection.
    """
    selection = criteria if isinstance(criteria, Selection) else Selection.of(
        criteria, uids or set(), series)
    with open(path, "w") as f:
        json.dump(selection.asJSONData(), f, indent=2)


def load_selection(path: str) -> Selection:
    """Read a selection file. Raises ValueError if it does not describe one."""
    with open(path) as f:
        return load_selection_json(f.read())


def load_selection_json(text: str) -> Selection:
    """Parse and check a selection payload, wherever it arrived from.

    Split from load_selection() because the file is not the only way this
    payload travels between two front ends, and because every format check
    belongs here rather than being repeated by each reader.

    Raises ValueError -- which json.JSONDecodeError already is -- naming what
    is wrong. Nothing is repaired or ignored: an unrecognised level, a level
    disagreeing with the keys beside it, an empty series list or a series list
    for a study that is not selected are each a file that does not mean what
    it says, and guessing which half to believe is the thing this format
    carries `selection_level` to avoid.
    """
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("selection is not a JSON object")

    criteria = data.get("criteria", {})
    if not isinstance(criteria, dict):
        raise ValueError("selection 'criteria' is not an object")

    studyUIDs = _uidList(data.get("study_uids", []), "study_uids")

    rawSeries = data.get("series_uids", {})
    if not isinstance(rawSeries, dict):
        raise ValueError("selection 'series_uids' is not an object")
    seriesUIDs: Dict[str, Set[str]] = {}
    for study, picked in rawSeries.items():
        wanted = _uidList(picked, f"series_uids[{study!r}]")
        if not wanted:
            # Not the same as the study being absent, which means all of them:
            # only a bug in a writer can produce it, so it is never guessed at.
            raise ValueError(
                f"selection lists no series at all for study {study!r}; "
                "leave the study out of 'series_uids' to select all of it")
        seriesUIDs[study] = wanted

    unknown = sorted(set(seriesUIDs) - studyUIDs)
    if unknown:
        raise ValueError(
            "selection lists series for studies that are not selected: "
            f"{unknown}")

    # An empty 'series_uids' names nothing, so it contradicts nothing and is
    # read as the key being absent; only a populated one asserts a level.
    level = data.get("selection_level", SERIES_LEVEL if seriesUIDs else STUDY_LEVEL)
    if level not in SELECTION_LEVELS:
        raise ValueError(
            f"selection has unknown 'selection_level' {level!r}, "
            f"expected one of {list(SELECTION_LEVELS)}")
    if level == SERIES_LEVEL and not seriesUIDs:
        raise ValueError(
            "selection is at series level but names no series in 'series_uids'")
    if level == STUDY_LEVEL and seriesUIDs:
        raise ValueError(
            "selection is at study level but 'series_uids' names series; "
            "the two disagree about what the file selects")

    return Selection(
        criteria=criteria,
        selectionLevel=level,
        study_uids=studyUIDs,
        series_uids=seriesUIDs,
    )


# -- restoring a selection --------------------------------------------------
#
# A restore is strict in every sense, and these three are why. --restore is
# not a bookmark or a user convenience: it is the simplest IPC there is, one
# process handing a selection to another with the file as the message. A
# silently degraded selection is a corrupted message and the sender has no way
# to find out the receiver dropped half of it, so each of these reports and
# refuses rather than carrying on with what is still there. They live here
# rather than in a front end because the second front end would otherwise
# copy them, and because the far end of an IPC channel cannot answer a dialog:
# the failure has to stay a message and an exit code.


def check_criteria(selection: Selection, criteria: Dict[str, Optional[str]]) -> None:
    """Raise unless the selection was made under the criteria in force now."""
    if selection.criteria != criteria:
        raise ValueError(
            "saved selection's search criteria do not match the criteria "
            "given on the command line\n"
            f"  saved:   {selection.criteria}\n"
            f"  current: {criteria}")


def check_studies(selection: Selection, matched: Set[str]) -> None:
    """Raise if a restored study is no longer among those `matched` by a search."""
    missing = sorted(selection.study_uids - set(matched))
    if missing:
        raise ValueError(
            "restored selection contains studies no longer matching the "
            f"search criteria: {missing}")


def check_series(selection: Selection, available: Dict[str, Set[str]]) -> None:
    """Raise if a restored series is no longer in the study that held it.

    `available` is {study UUID: the series UUIDs it holds now}, for at least
    every study the selection names series for.

    This bites more often than it looks like it should, and that is intended:
    a study whose series were all checked keeps its explicit full list, so any
    study a picker was opened over fails here if the archive has since lost
    one of its series, where before only a narrowed study could. The file
    describes an archive that has changed, and the message names the study as
    well as the series so the sender can tell which end went stale.
    """
    gone = {
        study: sorted(wanted - set(available.get(study, ())))
        for study, wanted in selection.series_uids.items()
    }
    gone = {study: missing for study, missing in gone.items() if missing}
    if gone:
        detail = "\n".join(
            f"  study {study}: {missing}" for study, missing in sorted(gone.items()))
        raise ValueError(
            "restored selection contains series their studies no longer "
            f"hold:\n{detail}")


def _uidList(value: object, what: str) -> Set[str]:
    """One of the file's UUID lists, as a set."""
    if not isinstance(value, list) or not all(isinstance(uid, str) for uid in value):
        raise ValueError(f"selection '{what}' is not a list of strings")
    return set(value)
