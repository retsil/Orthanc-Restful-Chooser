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

"""Command line for the series clone, run on the command-line host.

It takes the command-line host's own options for inputs and outputs, and adds
--suffix, which --module has no way to pass on.
"""

import argparse

from ...cmdline import addHostArguments, runApplication
from .application import DEFAULT_SUFFIX, CloneSeries


def parseArgs(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone each input series as a new series, with a suffix "
                    "appended to its series description.",
    )
    addHostArguments(parser)
    parser.add_argument(
        "--suffix",
        metavar="TEXT",
        default=DEFAULT_SUFFIX,
        help="text appended to each cloned series' SeriesDescription "
             "(default: %(default)r). Include any separating space yourself, "
             "and write --suffix=TEXT for a suffix that starts with '-'.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parseArgs(argv)
    runApplication(args, lambda host: CloneSeries(host, suffix=args.suffix))


if __name__ == "__main__":
    main()
