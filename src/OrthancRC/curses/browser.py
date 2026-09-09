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

With --module the selection is also handed straight to an Application: an
OrthancHost is built over the checked studies and the Application subclass
found in that module is loaded, wired to the host and fed every instance of
every selected study. Without it, the browser only selects.

Only the terminal front end lives here: the study search, the selection file
format and the Host that serves the selected studies are front end agnostic
and live in OrthancRC.studies, OrthancRC.selection and OrthancRC.orthanc.
"""

import argparse
import curses
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

from pyorthanc import Orthanc

from ..base import Application, Host
from ..loader import loadApplicationClass
from ..orthanc.host import OrthancHost
from ..selection import (add_search_arguments, build_criteria, load_selection,
                         save_selection)
from ..studies import StudyRecord, fetch_all_studies, format_date, matches


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
        help="After selection, write the checked study UUIDs (and the search "
             "criteria used) to this JSON file.",
    )
    parser.add_argument(
        "--restore-selection", metavar="PATH", default=None,
        help="Pre-check the studies saved by a previous --save-selection run. "
             "Fails if the saved search criteria don't match the ones given now.",
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

    return parser.parse_args(argv)


def run_curses_ui(stdscr, records: List[StudyRecord], preselected: Set[str]) -> Optional[Set[str]]:
    curses.curs_set(0)
    stdscr.keypad(True)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_WHITE)

    selected: Set[str] = set(preselected)
    cursor = 0
    top = 0

    columns = [
        ("Patient Name", "patient_name_display", 22),
        ("Patient ID", "patient_id", 14),
        ("DOB", "dob", 10),
        ("Study Date", "study_date", 10),
        ("Accession", "accession", 14),
        ("Description", "description", 24),
    ]

    def row_text(rec: Optional[StudyRecord]) -> str:
        if rec is None:
            cells = [name for name, _, width in columns]
        else:
            cells = [
                (format_date(rec.dob) if attr == "dob" else
                 format_date(rec.study_date) if attr == "study_date" else
                 getattr(rec, attr))
                for _, attr, width in columns
            ]
        parts = [f"{cell:<{width}.{width}}" for cell, (_, _, width) in zip(cells, columns)]
        return " ".join(parts)

    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()

        stdscr.addnstr(0, 0, "    " + row_text(None), width, curses.A_BOLD)
        stdscr.addnstr(1, 0, "-" * width, width)

        visible_rows = max(height - 3, 1)
        if cursor < top:
            top = cursor
        elif cursor >= top + visible_rows:
            top = cursor - visible_rows + 1

        for i in range(top, min(len(records), top + visible_rows)):
            rec = records[i]
            box = "[x]" if rec.uid in selected else "[ ]"
            line = f"{box} {row_text(rec)}"
            attr = curses.color_pair(1) if i == cursor else curses.A_NORMAL
            stdscr.addnstr(2 + (i - top), 0, line, width, attr)

        footer = (f"{len(selected)} selected / {len(records)} total  |  "
                  "up/down move  SPACE toggle  a all  n none  ENTER confirm  q/ESC cancel")
        stdscr.addnstr(height - 1, 0, footer, max(width - 1, 0), curses.A_REVERSE)
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
        elif key in (curses.KEY_ENTER, 10, 13):
            return selected
        elif key in (27, ord("q")):
            return None


def host_from_browser(
    client: Orthanc,
    criteria: Optional[Dict[str, Optional[str]]] = None,
    preselected: Optional[Set[str]] = None,
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

    selected = curses.wrapper(run_curses_ui, records, preselected or set())
    if selected is None:
        return None
    # Keep the browser's row order rather than the set's arbitrary one.
    return OrthancHost(client, [r.uid for r in records if r.uid in selected], **kwargs)


def run_application(host: Host, application_class: type[Application]) -> int:
    """Wire an Application to a Host and push the whole selection through it.

    Returns a process exit code: 0 once every instance has been accepted, 1 if
    the Application refused one (its way of cancelling) or the host had
    nothing to send.
    """
    app = application_class(host)
    host.setApplication(app)
    return 0 if host.sendInputs() else 1


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    try:
        criteria = build_criteria(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Load the Application before any searching or selecting, so a bad
    # --module fails immediately instead of after the user has picked studies.
    application_class = loadApplicationClass(args.module) if args.module else None

    restored_uids: Set[str] = set()
    if args.restore_selection:
        try:
            saved_criteria, restored_uids = load_selection(args.restore_selection)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: could not read --restore-selection file: {exc}", file=sys.stderr)
            return 1

        if saved_criteria != criteria:
            print(
                "error: saved selection's search criteria do not match the "
                "criteria given on the command line\n"
                f"  saved:   {saved_criteria}\n"
                f"  current: {criteria}",
                file=sys.stderr,
            )
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

    if restored_uids:
        matched_uids = {r.uid for r in records}
        missing = restored_uids - matched_uids
        if missing:
            print(
                "error: restored selection contains studies no longer matching "
                f"the search criteria: {sorted(missing)}",
                file=sys.stderr,
            )
            return 1

    if not records:
        print("No studies matched the given search criteria.")
        return 0

    selected = curses.wrapper(run_curses_ui, records, restored_uids)

    if selected is None:
        print("Selection cancelled.")
        return 1

    print(json.dumps(sorted(selected), indent=2))

    if args.save_selection:
        save_selection(args.save_selection, criteria, selected)
        print(f"Saved {len(selected)} study UUID(s) to {args.save_selection}", file=sys.stderr)

    if application_class is None:
        return 0

    # Built over the selection directly rather than through
    # host_from_browser(), which would re-fetch the studies and show the
    # picker a second time. Keep the browser's row order, not the set's.
    host = OrthancHost(
        client,
        [r.uid for r in records if r.uid in selected],
        outputDir=Path(args.output_dir) if args.output_dir else None,
        tmpDir=Path(args.tmp_dir) if args.tmp_dir else None,
        uploadOutputs=not args.no_upload,
    )
    return run_application(host, application_class)


if __name__ == "__main__":
    sys.exit(main())
