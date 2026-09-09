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

The selection file format is `{"criteria": {...}, "study_uids": [...]}`; it is
what a browser writes with --save-selection and what OrthancHost reads back
with fromSelectionFile(), so every front end must go through save_selection()
and load_selection() rather than writing the JSON by hand.
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


def save_selection(path: str, criteria: Dict[str, Optional[str]], uids: Set[str]) -> None:
    with open(path, "w") as f:
        json.dump({"criteria": criteria, "study_uids": sorted(uids)}, f, indent=2)


def load_selection(path: str) -> (Dict[str, Optional[str]], Set[str]):
    with open(path) as f:
        data = json.load(f)
    return data.get("criteria", {}), set(data.get("study_uids", []))
