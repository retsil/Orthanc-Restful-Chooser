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

"""Study records loaded from an Orthanc server, and local filtering of them.

Front end agnostic: a study browser calls `fetch_all_studies()` with the
criteria built by `selection.build_criteria()` and then narrows the result
locally with `matches()`.

The criteria are pushed down into the /tools/find query rather than being
applied only in `matches()`. Orthanc servers cap how many results a find can
return (`LimitFindResults`), so fetching every study and filtering afterwards
silently searches an arbitrary truncated prefix of the archive: on an archive
larger than the cap, a study that matches perfectly well is simply never
handed to `matches()`. `build_query()` is deliberately a *widening* of the
criteria -- it never excludes a study `matches()` would accept -- so
`matches()` still has the final say and the two cannot disagree.
"""

import dataclasses
from typing import Dict, List, Optional

from pyorthanc import Orthanc

from .selection import SEARCH_FIELDS

FIND_PAGE_SIZE = 1000


@dataclasses.dataclass
class StudyRecord:
    uid: str  # Orthanc identifier (UUID) for the study
    study_instance_uid: str
    patient_id: str
    patient_surname: str
    patient_given_name: str
    patient_name_display: str
    dob: str  # DICOM DA, YYYYMMDD
    study_date: str  # DICOM DA, YYYYMMDD
    accession: str
    description: str


def split_patient_name(raw: str) -> (str, str):
    """Split a DICOM PN value ('Surname^Given^Middle^Prefix^Suffix') in two."""
    alphabetic_group = raw.split("=")[0]
    components = alphabetic_group.split("^")
    surname = components[0] if len(components) > 0 else ""
    given = components[1] if len(components) > 1 else ""
    return surname, given


def build_query(criteria: Optional[Dict[str, Optional[str]]]) -> Dict[str, str]:
    """Translate search criteria into an Orthanc /tools/find Query.

    The result is a *widening* of `criteria`: every study `matches()` would
    accept is matched by this query too, so the server can discard the bulk of
    the archive while `matches()` remains the authority on what is a hit.

    Values use DICOM wildcard matching, which Orthanc applies case
    insensitively for these tags once "CaseSensitive" is false -- the same
    comparison `matches()` makes for a substring criterion.
    """
    if not criteria:
        return {}

    query: Dict[str, str] = {}

    if criteria.get("patient_id"):
        query["PatientID"] = f"*{criteria['patient_id']}*"

    # PatientName is one tag but two criteria, and the server can only be given
    # one pattern for it. Constraining it on the surname alone is the widening
    # that stays correct whichever of the two is set: "*x*" matches x anywhere
    # in "Surname^Given^...", so it cannot exclude a study whose given name
    # matches. matches() then splits the components properly.
    name_term = criteria.get("patient_surname") or criteria.get("patient_givenname")
    if name_term:
        query["PatientName"] = f"*{name_term}*"

    if criteria.get("dob"):
        query["PatientBirthDate"] = criteria["dob"]

    if criteria.get("accession"):
        query["AccessionNumber"] = f"*{criteria['accession']}*"

    if criteria.get("description"):
        query["StudyDescription"] = f"*{criteria['description']}*"

    # DICOM range matching. matches() compares dates as strings and rejects
    # studies with no StudyDate whenever a bound is set; an open-ended range
    # does both on the server.
    after, before = criteria.get("study_after"), criteria.get("study_before")
    if after or before:
        query["StudyDate"] = f"{after or ''}-{before or ''}"

    return query


def fetch_all_studies(
    client: Orthanc,
    criteria: Optional[Dict[str, Optional[str]]] = None,
) -> List[StudyRecord]:
    """Load the main tags of every study matching `criteria`, in pages.

    Uses the low-level /tools/find endpoint directly (via post_tools_find)
    instead of pyorthanc's find_studies()/Study objects: those only carry
    the Orthanc ID and would each lazily re-fetch their own main tags with a
    separate request the first time an attribute is read.

    Passing no criteria asks for the whole archive, which a server with a
    configured find limit will truncate -- see the module docstring.
    """
    records: List[StudyRecord] = []
    seen: set = set()
    since = 0
    while True:
        page = client.post_tools_find({
            "Level": "Study",
            "Query": build_query(criteria),
            "CaseSensitive": False,
            "Expand": True,
            "Limit": FIND_PAGE_SIZE,
            "Since": since,
        })
        if not page:
            break

        # A server that truncates a find to its own limit returns a short page
        # while more studies remain, so paging stops on an empty page rather
        # than a short one, and advances by what actually arrived rather than
        # by FIND_PAGE_SIZE -- advancing by the page size we asked for would
        # skip every study the server withheld.
        new_on_page = 0
        for entry in page:
            uid = entry["ID"]
            if uid in seen:
                continue
            seen.add(uid)
            new_on_page += 1
            main_tags = entry.get("MainDicomTags", {})
            patient_tags = entry.get("PatientMainDicomTags", {})
            surname, given = split_patient_name(patient_tags.get("PatientName", ""))
            records.append(StudyRecord(
                uid=uid,
                study_instance_uid=main_tags.get("StudyInstanceUID", ""),
                patient_id=patient_tags.get("PatientID", ""),
                patient_surname=surname,
                patient_given_name=given,
                patient_name_display=patient_tags.get("PatientName", "").replace("^", " ").strip(),
                dob=patient_tags.get("PatientBirthDate", ""),
                study_date=main_tags.get("StudyDate", ""),
                accession=main_tags.get("AccessionNumber", ""),
                description=main_tags.get("StudyDescription", ""),
            ))

        # A server that ignores "Since" hands back the same page forever.
        if new_on_page == 0:
            break
        since += len(page)

    return records


def matches(record: StudyRecord, criteria: Dict[str, Optional[str]]) -> bool:
    """Whether a study satisfies every criterion that is set.

    Which criteria exist, what each one narrows and how it compares all come
    from selection.SEARCH_FIELDS; a criterion left unset matches every study.
    """
    for field in SEARCH_FIELDS:
        wanted = criteria.get(field.key)
        if wanted and not field.accepts(getattr(record, field.attr), wanted):
            return False
    return True


def format_date(value: str) -> str:
    if len(value) == 8:
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"
    return value
