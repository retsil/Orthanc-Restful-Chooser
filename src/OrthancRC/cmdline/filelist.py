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

import argparse
import tempfile
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset
from pydicom.uid import generate_uid

from ..base import Host, Application
from ..enums import (
    State,
    Status,
)
from ..loader import loadApplicationClass
from ..orthanc_util import (
    extractMainTags,
    instanceUUIDFor,
)


class CmdLineHost(Host):
    """A minimal command-line Host: no GUI, reports state/status to stdout.

    Input DICOM files are read up front and indexed by Orthanc instance UUID.
    Output datasets are pulled from the Application and written to outputDir.
    """

    def __init__(self, inputFiles: list[Path], outputDir: Path, tmpDir: Path | None = None) -> None:
        if tmpDir is not None:
            self._tmpDir = tmpDir
            self._tmpDir.mkdir(parents=True, exist_ok=True)
        else:
            self._tmpDir = Path(tempfile.mkdtemp(prefix="cmdline-"))
        self._outputDir = outputDir
        self._outputDir.mkdir(parents=True, exist_ok=True)
        self._app: Application | None = None
        self._inputs: dict[str, Dataset] = {}
        for path in inputFiles:
            ds = pydicom.dcmread(str(path))
            self._inputs[instanceUUIDFor(ds)] = ds

    def setApplication(self, app: Application) -> None:
        self._app = app

    @property
    def instanceUIDs(self) -> list[str]:
        return list(self._inputs.keys())

    def getAvailableScreen(self) -> tuple[int, int, int, int]:
        # Headless: report a nominal virtual screen.
        return (0, 0, 800, 600)

    def getTmpDir(self) -> Path:
        return self._tmpDir

    def generateUID(self) -> str:
        return str(generate_uid())

    def getInputData(self, instanceUUID: str) -> Dataset:
        return self._inputs.get(instanceUUID, Dataset())

    def notifyOutputAvailable(self, instanceUUID: str, mainTags: dict[str, object], lastData: bool) -> bool:
        if self._app is None:
            self.notifyStatus(Status.ERROR, "no application registered")
            return False
        ds = self._app.getOutputData(instanceUUID)
        outPath = self._outputDir / f"{instanceUUID}.dcm"
        try:
            ds.save_as(str(outPath), enforce_file_format=True)
        except Exception as exc:  # noqa: BLE001 - an unwritable dataset is the app's bug
            self.notifyStatus(Status.ERROR, f"could not write output {instanceUUID}: {exc}")
            return False
        self.notifyStatus(Status.INFORMATION, f"wrote {outPath}")
        return True

    def notifyStateChanged(self, value: State) -> None:
        print(f"[state] {value.name}")

    def notifyStatus(self, value: Status, text: str) -> None:
        print(f"[{value.name}] {text}")


def collectInputFiles(args: argparse.Namespace) -> list[Path]:
    """Gather input DICOM paths from --input args and/or an --inputlist file."""
    files: list[Path] = []
    for item in args.input or []:
        files.append(Path(item))
    if args.inputlist:
        listPath = Path(args.inputlist)
        for line in listPath.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                files.append(Path(line))
    return files


def parseArgs(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Command-line DICOM host.")
    parser.add_argument(
        "--module",
        required=True,
        metavar="NAME",
        help="name of the module providing the Application subclass to run",
    )
    parser.add_argument(
        "--input",
        action="append",
        metavar="FILE",
        help="input DICOM file (repeatable)",
    )
    parser.add_argument(
        "--inputlist",
        metavar="FILE",
        help="text file containing one input DICOM filename per line",
    )
    parser.add_argument(
        "--outputdir",
        metavar="DIR",
        default=".",
        help="directory to write output DICOM files into (default: current dir)",
    )
    parser.add_argument(
        "--tmpdir",
        metavar="DIR",
        default=None,
        help="directory for temporary files (default: a fresh system temp dir)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parseArgs(argv)
    inputFiles = collectInputFiles(args)
    if not inputFiles:
        raise SystemExit("no input files: pass --input FILE and/or --inputlist FILE")

    tmpDir = Path(args.tmpdir) if args.tmpdir else None
    host = CmdLineHost(inputFiles, Path(args.outputdir), tmpDir)

    appClass = loadApplicationClass(args.module)
    app = appClass(host)
    host.setApplication(app)

    uids = host.instanceUIDs
    for i, uid in enumerate(uids):
        inputTags = extractMainTags(host.getInputData(uid))
        # A False return is how the Application cancels processing; stop
        # feeding it inputs and exit non-zero, as OrthancHost.sendInputs does.
        if not app.notifyInputAvailable(uid, inputTags, lastData=(i == len(uids) - 1)):
            host.notifyStatus(Status.WARNING, f"application refused input {uid}, stopping")
            raise SystemExit(1)


if __name__ == "__main__":
    main()
