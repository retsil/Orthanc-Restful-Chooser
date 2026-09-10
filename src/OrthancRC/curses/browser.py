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

"""Search studies on an Orthanc server and pick a subset via a curses UI.

The --search-* criteria are sent to Orthanc as a /tools/find query and the
returned main tags are then narrowed locally by the same criteria, so an
archive larger than the server's find limit is still searched in full. The
matching studies are
shown in a curses list where each row can be checked/unchecked; the checked
study UUIDs (Orthanc identifiers) are printed to stdout and, optionally,
saved to a JSON file for use by a later step.

`s` opens a second picker over every study that is checked, listing their
series so that a study can be narrowed to some of them. A study nobody
narrowed means the whole study, exactly as it always did, so a run that never
presses `s` behaves as it did before the key existed.

With --module the selection is also handed straight to an Application: an
OrthancHost is built over the checked studies and the Application subclass
found in that module is loaded, wired to the host and fed every instance of
every selected study. Without it, the browser only selects. --stage adds a
review step to that: the Application's output is held back until a table of
the output series it produced has been confirmed.

Only the terminal front end lives here: the study search, the series listing,
the selection file format, the Host that serves the selected studies and the
staging of its output are all front end agnostic and live in
OrthancRC.studies, OrthancRC.series, OrthancRC.selection and
OrthancRC.orthanc.
"""

import argparse
import curses
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from pyorthanc import Orthanc

from ..base import Application, Host
from ..loader import loadApplicationClass
from ..orthanc.host import OrthancHost
from ..orthanc.staging import (DEFAULT_OUTPUT_CACHE_BYTES, StagedSeries,
                               StagingHost, formatByteSize, parseByteSize)
from ..selection import (add_search_arguments, build_criteria, check_criteria,
                         check_series, check_studies, load_selection,
                         save_selection)
from ..series import SeriesCache, SeriesRecord
from ..studies import StudyRecord, fetch_all_studies, format_date, matches

# A selection as the pickers pass it around: the checked studies, and the
# series wanted from the ones a series picker was opened over. A study absent
# from the second means the whole study.
Picked = Tuple[Set[str], Dict[str, Set[str]]]


STUDY_COLUMNS = (
    ("Patient Name", 22),
    ("Patient ID", 14),
    ("DOB", 10),
    ("Study Date", 10),
    ("Accession", 14),
    ("Description", 24),
)

SERIES_COLUMNS = (
    ("Series", 6),
    ("Modality", 8),
    ("Description", 34),
    ("Instances", 9),
)

STAGE_COLUMNS = (
    ("Series", 6),
    ("Modality", 8),
    ("Description", 34),
    ("Out. inst.", 10),
    ("Size", 10),
)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search and select Orthanc studies via a curses UI.",
    )
    parser.add_argument("--orthanc-url", default="http://localhost:8042")
    parser.add_argument("--orthanc-username", default=None)
    parser.add_argument("--orthanc-password", default=None)

    add_search_arguments(parser)

    parser.add_argument(
        "--save-selection", metavar="PATH", default=None,
        help="After selection, write the checked study UUIDs (and any series "
             "picked with s, and the search criteria used) to this JSON file.",
    )
    parser.add_argument(
        "--restore-selection", metavar="PATH", default=None,
        help="Pre-check the studies and series saved by a previous "
             "--save-selection run. Fails if the saved search criteria don't "
             "match the ones given now, or if a saved study or series has "
             "since gone.",
    )

    parser.add_argument(
        "--module", metavar="NAME", default=None,
        help="Name of the module providing the Application subclass to run on "
             "the selected studies. Without it the browser only selects.",
    )
    parser.add_argument(
        "--output-dir", metavar="DIR", default=None,
        help="Directory to also write the Application's output DICOM files "
             "into (--module only; by default outputs are only uploaded).",
    )
    parser.add_argument(
        "--tmp-dir", metavar="DIR", default=None,
        help="Directory for the host's temporary files (--module only; "
             "default: a fresh system temp dir).",
    )
    parser.add_argument(
        "--no-upload", action="store_true",
        help="Do not upload the Application's output back into Orthanc "
             "(--module only). Pair with --output-dir to keep the results.",
    )
    parser.add_argument(
        "--stage", action="store_true",
        help="Hold the Application's output back until a table of the output "
             "series it produced has been reviewed (--module only). Nothing "
             "is written or uploaded before the table is confirmed.",
    )
    parser.add_argument(
        "--output-cache", metavar="SIZE",
        default=str(DEFAULT_OUTPUT_CACHE_BYTES),
        help="How much staged output to hold in memory before spilling it to "
             "the host's temporary directory (--stage only). A byte count "
             "with an optional K, M or G suffix; default 128M. 0 spills "
             "everything.",
    )

    return parser.parse_args(argv)


# -- screens ----------------------------------------------------------------


def _prepare(stdscr) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_WHITE)


def _row_text(columns: Sequence[Tuple[str, int]],
              cells: Optional[Sequence[object]] = None) -> str:
    """One list row: the column headings when `cells` is None, else those values."""
    if cells is None:
        cells = [name for name, _width in columns]
    return " ".join(f"{str(cell):<{width}.{width}}"
                    for cell, (_name, width) in zip(cells, columns))


def _draw_footer(stdscr, text: str) -> None:
    height, width = stdscr.getmaxyx()
    stdscr.addnstr(height - 1, 0, text, max(width - 1, 0), curses.A_REVERSE)


def _study_cells(rec: StudyRecord) -> List[str]:
    return [
        rec.patient_name_display, rec.patient_id, format_date(rec.dob),
        format_date(rec.study_date), rec.accession, rec.description,
    ]


def run_curses_ui(
    stdscr,
    records: List[StudyRecord],
    preselected: Set[str],
    preselectedSeries: Optional[Dict[str, Set[str]]] = None,
    seriesSource: Optional[SeriesCache] = None,
) -> Optional[Picked]:
    """Pick studies, and optionally narrow them to some of their series.

    Returns the checked study UUIDs and the series wanted from the studies a
    series picker was opened over, or None if the user cancelled. A study
    absent from the second means the whole study, so a run that never presses
    `s` returns an empty mapping and means what it always meant -- and without
    a `seriesSource` to list series from, `s` is not offered at all.
    """
    _prepare(stdscr)

    selected: Set[str] = set(preselected)
    series: Dict[str, Set[str]] = {
        study: set(picked) for study, picked in (preselectedSeries or {}).items()
    }
    cursor = 0
    top = 0
    message = ""

    def narrowed(rec: StudyRecord) -> bool:
        """Whether only some of a study's series are taken.

        A study whose every series is checked is not narrowed: the user
        checked everything, and a third marker for "everything, and pinned to
        what was on the screen" would explain an implementation detail rather
        than a choice they made. A study whose series were restored from a
        file and never listed has no total to compare against, and is shown
        as narrowed rather than costing a request per row to find out.
        """
        picked = series.get(rec.uid)
        if picked is None:
            return False
        held = seriesSource.known(rec.uid) if seriesSource is not None else None
        return held is None or len(picked) != len(held)

    def confirmed() -> Picked:
        # Only the studies still checked, so that un-checking a study a
        # picker was opened over does not leave its series behind.
        return selected, {uid: picked for uid, picked in series.items()
                          if uid in selected}

    def box(rec: StudyRecord) -> str:
        if rec.uid not in selected:
            return "[ ]"
        return "[~]" if narrowed(rec) else "[x]"

    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()

        stdscr.addnstr(0, 0, "    " + _row_text(STUDY_COLUMNS), width, curses.A_BOLD)
        stdscr.addnstr(1, 0, "-" * width, width)

        visible_rows = max(height - 3, 1)
        if cursor < top:
            top = cursor
        elif cursor >= top + visible_rows:
            top = cursor - visible_rows + 1

        for i in range(top, min(len(records), top + visible_rows)):
            rec = records[i]
            line = f"{box(rec)} {_row_text(STUDY_COLUMNS, _study_cells(rec))}"
            attr = curses.color_pair(1) if i == cursor else curses.A_NORMAL
            stdscr.addnstr(2 + (i - top), 0, line, width, attr)

        narrowedCount = sum(1 for rec in records
                            if rec.uid in selected and narrowed(rec))
        footer = (f"{len(selected)} selected / {len(records)} total"
                  + (f", {narrowedCount} narrowed" if narrowedCount else "")
                  + "  |  up/down move  SPACE toggle  a all  n none  "
                    "s series  ENTER confirm  q/ESC cancel")
        _draw_footer(stdscr, message or footer)
        message = ""
        stdscr.refresh()

        if not records:
            key = stdscr.getch()
            if key in (27, ord("q")):
                return None
            continue

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            cursor = max(0, cursor - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            cursor = min(len(records) - 1, cursor + 1)
        elif key == curses.KEY_PPAGE:
            cursor = max(0, cursor - visible_rows)
        elif key == curses.KEY_NPAGE:
            cursor = min(len(records) - 1, cursor + visible_rows)
        elif key == ord(" "):
            uid = records[cursor].uid
            if uid in selected:
                selected.discard(uid)
            else:
                selected.add(uid)
        elif key == ord("a"):
            selected = {r.uid for r in records}
        elif key == ord("n"):
            selected = set()
        elif key == ord("s"):
            message, confirm = _sub_select(
                stdscr, records, selected, series, seriesSource)
            if confirm:
                # ENTER means the same thing on either screen, so a user who
                # narrows the last study is done rather than being handed the
                # study list back to press ENTER on again.
                return confirmed()
        elif key in (curses.KEY_ENTER, 10, 13):
            return confirmed()
        elif key in (27, ord("q")):
            return None


def _sub_select(
    stdscr,
    records: List[StudyRecord],
    selected: Set[str],
    series: Dict[str, Set[str]],
    seriesSource: Optional[SeriesCache],
) -> Tuple[str, bool]:
    """Run the series picker over every checked study.

    Returns a footer message for the study list and whether the user confirmed
    the browse from the picker -- ENTER there means what ENTER means on the
    study list, so the caller returns its selection rather than redrawing.

    Every study the picker covered comes back with an entry in `series`, one
    whose series are all checked included: absent and complete-list are
    different statements, and the difference is when they are evaluated.
    Absent means whatever the study holds when the selection is read, a full
    list means these series, as the study stood when the user looked at it,
    and only the second records what was actually seen.

    A study left with no series at all is un-checked instead -- no series is
    no study -- which is also how the whole picker's empty case is reached:
    un-checking every series of every study un-checks every study, and
    confirming that is the confirmed empty selection the study list already
    has.
    """
    covered = [r.uid for r in records if r.uid in selected]
    if not covered:
        return "s needs some checked studies to pick series from", False
    if seriesSource is None:
        return "series are not available in this browser", False

    def progress(done: int, total: int) -> None:
        # Listing series is a request per study, so a hundred checked studies
        # is a hundred of them and the wait wants saying out loud.
        _draw_footer(stdscr, f"loading series for study {done + 1} of {total}...")
        stdscr.refresh()

    try:
        seriesByStudy = seriesSource.load(covered, progress)
    except Exception as exc:  # noqa: BLE001 - surface any HTTP error in the footer
        return f"could not load series: {exc}", False

    if not any(seriesByStudy.get(studyUUID) for studyUUID in covered):
        # A stale selection rather than an empty sub-selection, and worth
        # saying so instead of opening a picker with no rows in it.
        return "none of the checked studies has any series to pick from", False

    # The same screen, not a nested curses.wrapper(): a second wrapper would
    # end the window this one is still drawing on when it returned.
    picked = run_series_ui(stdscr, records, covered, seriesByStudy, series)
    if picked is None:
        return "series sub-selection discarded", False

    emptied = 0
    for studyUUID, wanted in picked.items():
        if wanted:
            series[studyUUID] = wanted
        else:
            selected.discard(studyUUID)
            series.pop(studyUUID, None)
            emptied += 1

    covered_count = len(picked) - emptied
    return (f"series picked for {covered_count} study/studies"
            + (f"; {emptied} un-checked, having no series left" if emptied else ""),
            True)


def run_series_ui(
    stdscr,
    records: List[StudyRecord],
    covered: List[str],
    seriesByStudy: Dict[str, List[SeriesRecord]],
    current: Dict[str, Set[str]],
) -> Optional[Dict[str, Set[str]]]:
    """Pick series across every covered study, as one list.

    One list holding every series of every checked study rather than a screen
    per study: one cursor runs through the whole thing and `a`/`n` check and
    un-check everything at once, exactly as they do over every study in the
    study list. The grouping by study is presentation only.

    Returns {study UUID: the series checked}, with an entry for every covered
    study -- an empty set meaning the caller should un-check that study -- or
    None if the user discarded the sub-selection. ENTER keeps it and confirms
    the browse, as it does on the study list; q/ESC discards it, abandoning
    the sub-selection rather than the whole browse, which is the one place the
    two pickers differ in meaning.
    """
    _prepare(stdscr)

    byUID = {r.uid: r for r in records}
    # Rows are series, grouped by study for reading, in the DICOM order the
    # host will offer their instances in; studies keep the study list's order.
    rows: List[Tuple[str, object]] = []
    for studyUUID in covered:
        rows.append(("study", byUID[studyUUID]))
        for record in seriesByStudy.get(studyUUID, ()):
            rows.append(("series", record))

    # Everything, for a study with no sub-selection yet -- the whole study is
    # what it currently means -- and otherwise its current sub-selection.
    checked: Set[str] = set()
    for studyUUID in covered:
        wanted = current.get(studyUUID)
        for record in seriesByStudy.get(studyUUID, ()):
            if wanted is None or record.uid in wanted:
                checked.add(record.uid)

    selectable = [i for i, (kind, _row) in enumerate(rows) if kind == "series"]
    if not selectable:
        # Nothing to narrow, so nothing to ask: _sub_select says so before it
        # gets here, and this is only for a caller that did not.
        return None

    cursor = selectable[0]
    top = 0
    total = len(selectable)

    def move(delta: int) -> int:
        """The next selectable row in `delta`'s direction, skipping headings."""
        place = max(0, min(len(selectable) - 1,
                           selectable.index(cursor) + delta))
        return selectable[place]

    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()

        stdscr.addnstr(0, 0, "    " + _row_text(SERIES_COLUMNS), width, curses.A_BOLD)
        stdscr.addnstr(1, 0, "-" * width, width)

        visible_rows = max(height - 3, 1)
        if cursor < top:
            top = cursor
        elif cursor >= top + visible_rows:
            top = cursor - visible_rows + 1

        for i in range(top, min(len(rows), top + visible_rows)):
            kind, row = rows[i]
            if kind == "study":
                cells = _study_cells(row)
                line = f"--- {cells[0]}  {cells[3]}  {row.description}"
                attr = curses.A_BOLD
            else:
                box = "[x]" if row.uid in checked else "[ ]"
                line = f"{box} " + _row_text(SERIES_COLUMNS, [
                    row.series_number, row.modality, row.description,
                    row.instance_count,
                ])
                attr = curses.color_pair(1) if i == cursor else curses.A_NORMAL
            stdscr.addnstr(2 + (i - top), 0, line, width, attr)

        emptied = sum(1 for studyUUID in covered
                      if not any(r.uid in checked
                                 for r in seriesByStudy.get(studyUUID, ())))
        footer = (f"{len(checked)} series selected / {total}"
                  + (f", {emptied} study/studies would be un-checked" if emptied else "")
                  + "  |  up/down move  SPACE toggle  a all  n none  "
                    "ENTER confirm  q/ESC discard changes")
        _draw_footer(stdscr, footer)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            cursor = move(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            cursor = move(1)
        elif key == curses.KEY_PPAGE:
            cursor = move(-visible_rows)
        elif key == curses.KEY_NPAGE:
            cursor = move(visible_rows)
        elif key == ord(" "):
            uid = rows[cursor][1].uid
            if uid in checked:
                checked.discard(uid)
            else:
                checked.add(uid)
        elif key == ord("a"):
            checked = {rows[i][1].uid for i in selectable}
        elif key == ord("n"):
            checked = set()
        elif key in (curses.KEY_ENTER, 10, 13):
            return {
                studyUUID: {r.uid for r in seriesByStudy.get(studyUUID, ())
                            if r.uid in checked}
                for studyUUID in covered
            }
        elif key in (27, ord("q")):
            return None


def run_stage_ui(stdscr, rows: List[StagedSeries]) -> Set[str]:
    """Review the output series a staged run produced; returns the keys to keep.

    Every row starts accepted, so `--stage` is a review step rather than an
    opt-in: confirming the table untouched does exactly what the same run
    without --stage would have done, and the worst a reflexive confirm can do
    is today's behaviour. Rejecting is the action.

    There is no cancel. In the other two pickers q/ESC abandons to somewhere,
    and here there is nowhere: the run is over and the only thing q could mean
    is commit nothing, which sitting beside a default of accept-everything is
    the exact opposite of the ENTER next to it. So the ways out are ENTER and
    an explicit `n`, and the count in the footer says what ENTER will do.
    """
    if not rows:
        return set()
    _prepare(stdscr)

    accepted: Set[str] = {row.key for row in rows}
    cursor = 0
    top = 0
    message = ""

    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()

        stdscr.addnstr(0, 0, "    " + _row_text(STAGE_COLUMNS), width, curses.A_BOLD)
        stdscr.addnstr(1, 0, "-" * width, width)

        visible_rows = max(height - 3, 1)
        if cursor < top:
            top = cursor
        elif cursor >= top + visible_rows:
            top = cursor - visible_rows + 1

        for i in range(top, min(len(rows), top + visible_rows)):
            row = rows[i]
            box = "[y]" if row.key in accepted else "[n]"
            line = f"{box} " + _row_text(STAGE_COLUMNS, [
                row.seriesNumber, row.modality,
                row.description or row.seriesInstanceUID or row.key,
                row.instanceCount, formatByteSize(row.byteCount),
            ])
            attr = curses.color_pair(1) if i == cursor else curses.A_NORMAL
            stdscr.addnstr(2 + (i - top), 0, line, width, attr)

        footer = (f"{len(accepted)} series accepted / {len(rows)}  |  "
                  "up/down move  SPACE toggle  a all  n none  ENTER commit")
        _draw_footer(stdscr, message or footer)
        message = ""
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            cursor = max(0, cursor - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            cursor = min(len(rows) - 1, cursor + 1)
        elif key == curses.KEY_PPAGE:
            cursor = max(0, cursor - visible_rows)
        elif key == curses.KEY_NPAGE:
            cursor = min(len(rows) - 1, cursor + visible_rows)
        elif key == ord(" "):
            uid = rows[cursor].key
            if uid in accepted:
                accepted.discard(uid)
            else:
                accepted.add(uid)
        elif key == ord("a"):
            accepted = {row.key for row in rows}
        elif key == ord("n"):
            accepted = set()
        elif key in (curses.KEY_ENTER, 10, 13):
            return accepted
        elif key in (27, ord("q")):
            message = ("the run is over, so there is nothing to cancel: "
                       "ENTER commits, n rejects all")


# -- the browser as a call, and as a command line ---------------------------


def host_from_browser(
    client: Orthanc,
    criteria: Optional[Dict[str, Optional[str]]] = None,
    preselected: Optional[Set[str]] = None,
    preselectedSeries: Optional[Dict[str, Set[str]]] = None,
    **kwargs,
) -> Optional[OrthancHost]:
    """Pick studies in the curses UI and build a Host over them.

    Returns None if the user cancelled the picker. `criteria` has the shape
    build_criteria() returns; None offers every study on the server. Any other
    keyword goes to OrthancHost.

    This is the whole browser as a single call, for a caller that wants a
    ready-made Host rather than main()'s command line.
    """
    records = fetch_all_studies(client, criteria)
    if criteria is not None:
        records = [r for r in records if matches(r, criteria)]

    picked = curses.wrapper(
        run_curses_ui, records, preselected or set(), preselectedSeries or {},
        SeriesCache(client),
    )
    if picked is None:
        return None
    selected, series = picked
    # Keep the browser's row order rather than the set's arbitrary one.
    return OrthancHost(client, [r.uid for r in records if r.uid in selected],
                       seriesUUIDs=series, **kwargs)


def run_application(host: Host, application_class: type[Application]) -> int:
    """Wire an Application to a Host and push the whole selection through it.

    Returns a process exit code: 0 once every instance has been accepted, 1 if
    the Application refused one (its way of cancelling) or the host had
    nothing to send.
    """
    app = application_class(host)
    host.setApplication(app)
    return 0 if host.sendInputs() else 1


def run_staged_application(
    host: OrthancHost,
    application_class: type[Application],
    outputCacheBytes: int,
) -> int:
    """Run an Application with its output held back for review.

    The run itself is unchanged -- the Application's getOutputData() is still
    called from inside its own notifyOutputAvailable() -- and only what
    becomes of the output waits. A run that produced no output at all skips
    the table rather than showing an empty one.
    """
    staging = StagingHost(host, outputCacheBytes=outputCacheBytes)
    code = run_application(staging, application_class)

    rows = staging.stagedOutputs
    if not rows:
        print("No output was staged, so there is nothing to review.",
              file=sys.stderr)
        staging.discardStagedOutputs()
        return code

    accepted = curses.wrapper(run_stage_ui, rows)
    return code if staging.commitStagedOutputs(accepted) else 1


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    try:
        criteria = build_criteria(args)
        outputCacheBytes = parseByteSize(args.output_cache)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.stage and args.no_upload and not args.output_dir:
        print("warning: --stage with --no-upload and no --output-dir has "
              "nowhere to commit output to, so the review decides nothing.",
              file=sys.stderr)

    # Load the Application before any searching or selecting, so a bad
    # --module fails immediately instead of after the user has picked studies.
    application_class = loadApplicationClass(args.module) if args.module else None

    restored = None
    if args.restore_selection:
        try:
            restored = load_selection(args.restore_selection)
            # The criteria are checked before anything is fetched: a selection
            # made under a different search cannot be reconciled with this one
            # whatever the archive holds.
            check_criteria(restored, criteria)
        except OSError as exc:
            print(f"error: could not read --restore-selection file: {exc}",
                  file=sys.stderr)
            return 1
        except ValueError as exc:  # json.JSONDecodeError included
            print(f"error: {exc}", file=sys.stderr)
            return 1

    client = Orthanc(
        url=args.orthanc_url,
        username=args.orthanc_username,
        password=args.orthanc_password,
    )

    try:
        all_studies = fetch_all_studies(client, criteria)
    except Exception as exc:  # noqa: BLE001 - surface any connection/HTTP error plainly
        print(f"error: could not load studies from Orthanc: {exc}", file=sys.stderr)
        return 1

    records = [r for r in all_studies if matches(r, criteria)]

    # Shared with the series picker, so a study whose series were fetched to
    # validate a restore is not fetched again when s is pressed.
    cache = SeriesCache(client)
    restoredSeries: Dict[str, Set[str]] = {}
    if restored is not None:
        try:
            check_studies(restored, {r.uid for r in records})
            if restored.series_uids:
                available = {
                    studyUUID: {s.uid for s in cache.get(studyUUID)}
                    for studyUUID in sorted(restored.series_uids)
                }
                check_series(restored, available)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001 - surface any connection/HTTP error plainly
            print(f"error: could not load series from Orthanc: {exc}", file=sys.stderr)
            return 1
        restoredSeries = {study: set(wanted)
                          for study, wanted in restored.series_uids.items()}

    if not records:
        print("No studies matched the given search criteria.")
        return 0

    picked = curses.wrapper(
        run_curses_ui, records,
        restored.study_uids if restored is not None else set(),
        restoredSeries,
        cache,
    )

    if picked is None:
        print("Selection cancelled.")
        return 1

    selected, series = picked

    if args.save_selection:
        save_selection(args.save_selection, criteria, selected, series)
        saved = f"Saved {len(selected)} study UUID(s)"
        if series:
            saved += (f" and {sum(len(s) for s in series.values())} series "
                      f"UUID(s) across {len(series)} of them")
        print(f"{saved} to {args.save_selection}", file=sys.stderr)

    if application_class is None:
        return 0

    # Built over the selection directly rather than through
    # host_from_browser(), which would re-fetch the studies and show the
    # picker a second time. Keep the browser's row order, not the set's.
    host = OrthancHost(
        client,
        [r.uid for r in records if r.uid in selected],
        seriesUUIDs=series,
        outputDir=Path(args.output_dir) if args.output_dir else None,
        tmpDir=Path(args.tmp_dir) if args.tmp_dir else None,
        uploadOutputs=not args.no_upload,
    )
    if args.stage:
        return run_staged_application(host, application_class, outputCacheBytes)
    return run_application(host, application_class)


if __name__ == "__main__":
    sys.exit(main())
