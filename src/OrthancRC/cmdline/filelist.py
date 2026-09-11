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
from collections.abc import Callable
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset
from pydicom.uid import generate_uid

from ..base import Host, Application
from ..enums import (
    State,
    Status,
    asEnum,
)
from ..loader import loadApplicationClass
from ..orthanc_util import (
    extractMainTags,
    instanceUUIDFor,
    outputProblems,
)

# What instanceUUIDFor() hashes; an input missing any of them shares its
# identity with every other input missing the same ones.
_IDENTITY_TAGS = ("PatientID", "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID")


class CmdLineHost(Host):
    """A minimal command-line Host: no GUI, reports state/status to stdout.

    Input DICOM files are read up front and indexed by Orthanc instance UUID.
    Output datasets are pulled from the Application and written to outputDir.
    """

    def __init__(self, inputFiles: list[Path], outputDir: Path, tmpDir: Path | None = None) -> None:
        # Every (Status, text) reported, so a front end can tell whether
        # anything went wrong; the inputs below already report to it.
        self.messages: list[tuple[Status, str]] = []
        if tmpDir is not None:
            self._tmpDir = tmpDir
            self._tmpDir.mkdir(parents=True, exist_ok=True)
        else:
            self._tmpDir = Path(tempfile.mkdtemp(prefix="cmdline-"))
        self._outputDir = outputDir
        self._outputDir.mkdir(parents=True, exist_ok=True)
        self._app: Application | None = None
        self._inputs: dict[str, Dataset] = {}
        # Output UUIDs taken this run; each names a file in outputDir.
        self._announced: set[str] = set()
        paths: dict[str, Path] = {}
        for path in inputFiles:
            ds = pydicom.dcmread(str(path))
            missing = [tag for tag in _IDENTITY_TAGS if not str(ds.get(tag, ""))]
            if missing:
                self.notifyStatus(Status.WARNING, f"{path} has no {', '.join(missing)}")
            uuid = instanceUUIDFor(ds)
            if uuid in paths:
                # Keyed by identity, so the Application would silently see one
                # input fewer than it was given.
                self.notifyStatus(
                    Status.WARNING, f"{path} has the same identity as {paths[uuid]}, "
                                    "and replaces it")
            paths[uuid] = path
            self._inputs[uuid] = ds
        self._patientIDs = {str(ds.get("PatientID", "")) for ds in self._inputs.values()}
        self._studyUIDs = {str(ds.get("StudyInstanceUID", "")) for ds in self._inputs.values()}

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
        if instanceUUID not in self._inputs:
            # Reported as OrthancHost reports it, rather than handed over empty.
            self.notifyStatus(Status.ERROR, f"{instanceUUID} is not a selected instance")
            return Dataset()
        return self._inputs[instanceUUID]

    def notifyOutputAvailable(self, instanceUUID: str, lastData: bool) -> bool:
        if self._app is None:
            self.notifyStatus(Status.ERROR, "no application registered")
            return False
        ds = self._app.getOutputData(instanceUUID)
        problems = outputProblems(ds, instanceUUID, self._inputs, self._announced,
                                  self._patientIDs, self._studyUIDs)
        for status, text in problems:
            self.notifyStatus(status, text)
        if any(status is Status.ERROR for status, _text in problems):
            return False
        outPath = self._outputDir / f"{instanceUUID}.dcm"
        if outPath.exists():
            # Repeats within a run are refused above; this is a file left by
            # an earlier run into the same directory.
            self.notifyStatus(Status.WARNING, f"overwriting {outPath}")
        try:
            ds.save_as(str(outPath), enforce_file_format=True)
        except Exception as exc:  # noqa: BLE001 - an unwritable dataset is the app's bug
            self.notifyStatus(Status.ERROR, f"could not write output {instanceUUID}: {exc}")
            return False
        self._announced.add(instanceUUID)
        self.notifyStatus(Status.INFORMATION, f"wrote {outPath}")
        return True

    def notifyStateChanged(self, value: State) -> None:
        state, problem = asEnum(State, value)
        if problem is not None:
            self.notifyStatus(Status.WARNING if state is not None else Status.ERROR, problem)
        if state is not None:
            print(f"[state] {state.name}")

    def notifyStatus(self, value: Status, text: str) -> None:
        status, problem = asEnum(Status, value)
        # Not `status or ERROR`: INFORMATION is 0.
        level = status if status is not None else Status.ERROR
        self.messages.append((level, text))
        print(f"[{level.name}] {text}")
        if problem is not None:
            self.notifyStatus(Status.WARNING if status is not None else Status.ERROR, problem)


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


def addHostArguments(parser: argparse.ArgumentParser) -> None:
    """Add the options describing the inputs, outputs and temp dir to parser.

    Shared with Applications that bring their own command line, so that they
    take their inputs exactly as this host does and add only their own options.
    """
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


def parseArgs(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Command-line DICOM host.")
    parser.add_argument(
        "--module",
        required=True,
        metavar="NAME",
        help="name of the module providing the Application subclass to run",
    )
    addHostArguments(parser)
    return parser.parse_args(argv)


def runApplication(args: argparse.Namespace,
                   appFactory: Callable[[Host], Application]) -> None:
    """Build a CmdLineHost from args and feed its inputs to appFactory(host).

    args is anything parsed by a parser that addHostArguments() filled in.
    The factory is what lets an Application with options of its own be run:
    --module can only call the class with the Host, and nothing else.
    """
    inputFiles = collectInputFiles(args)
    if not inputFiles:
        raise SystemExit("no input files: pass --input FILE and/or --inputlist FILE")

    tmpDir = Path(args.tmpdir) if args.tmpdir else None
    host = CmdLineHost(inputFiles, Path(args.outputdir), tmpDir)

    app = appFactory(host)
    host.setApplication(app)

    uids = host.instanceUIDs
    for i, uid in enumerate(uids):
        inputTags = extractMainTags(host.getInputData(uid))
        # A False return is how the Application cancels processing; stop
        # feeding it inputs and exit non-zero, as OrthancHost.sendInputs does.
        if not app.notifyInputAvailable(uid, inputTags, lastData=(i == len(uids) - 1)):
            host.notifyStatus(Status.WARNING, f"application refused input {uid}, stopping")
            raise SystemExit(1)

    # Every input accepted is not the same as the run having worked: an output
    # that could not be written was reported, and must not exit 0.
    if any(status in (Status.ERROR, Status.FATALERROR) for status, _text in host.messages):
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    args = parseArgs(argv)
    runApplication(args, loadApplicationClass(args.module))


if __name__ == "__main__":
    main()
