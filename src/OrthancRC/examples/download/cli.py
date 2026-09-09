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

"""Command line for the download Application.

Unlike the browser, this runs no picker: the studies come from a selection
file the browser wrote earlier with --save-selection. It reuses OrthancHost to
serve them, so the only thing this example adds is its own Application.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from pyorthanc import Orthanc

from ...orthanc.host import OrthancHost
from .application import DownloadSeries


def parseArgs(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download the series matching a series instance UID, a "
                    "modality and a series description from a saved study "
                    "selection into a folder.",
    )
    parser.add_argument("--orthanc-url", default="http://localhost:8042")
    parser.add_argument("--orthanc-username", default=None)
    parser.add_argument("--orthanc-password", default=None)

    parser.add_argument(
        "--from-selection-file", metavar="PATH", required=True,
        help="JSON file of study UUIDs written by the browser's "
             "--save-selection.",
    )
    parser.add_argument(
        "--match-series-description", metavar="TEXT", default=None,
        help="Only download series whose SeriesDescription contains TEXT "
             "(case-insensitive). Unset matches every description.",
    )
    parser.add_argument(
        "--match-modality", metavar="MODALITY", default=None,
        help="Only download series of this Modality, e.g. CT "
             "(case-insensitive, exact). Unset matches every modality.",
    )
    parser.add_argument(
        "--match-series-instance-uid", metavar="UID", default=None,
        help="Only download the series with this SeriesInstanceUID (exact). "
             "Unset matches every series.",
    )
    parser.add_argument(
        "--no-progress", action="store_true",
        help="Do not report download progress. Progress goes to standard "
             "error: a bar rewritten in place on a terminal, a line every "
             "10%% anywhere else.",
    )
    parser.add_argument(
        "--target-folder", metavar="DIR", required=True,
        help="Directory to download the matching series into. A second "
             "matching series goes to DIR-1, a third to DIR-2, and so on.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parseArgs(argv)

    client = Orthanc(
        url=args.orthanc_url,
        username=args.orthanc_username,
        password=args.orthanc_password,
    )

    try:
        # This Application writes files itself and produces no native objects,
        # so the host has nothing to upload back into Orthanc.
        host = OrthancHost.fromSelectionFile(
            client, args.from_selection_file, uploadOutputs=False,
        )
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: could not read --from-selection-file: {exc}", file=sys.stderr)
        return 1

    app = DownloadSeries(
        host,
        matchSeriesDescription=args.match_series_description,
        matchModality=args.match_modality,
        matchSeriesInstanceUID=args.match_series_instance_uid,
        targetFolder=Path(args.target_folder),
        showProgress=not args.no_progress,
    )
    host.setApplication(app)
    return 0 if host.sendInputs() else 1


if __name__ == "__main__":
    sys.exit(main())
