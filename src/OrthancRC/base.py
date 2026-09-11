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

import sys
from abc import ABC, abstractmethod
from pathlib import Path

from pydicom.dataset import Dataset

from .enums import State, Status, asEnum


class Host(ABC):

    @abstractmethod
    def getAvailableScreen(self) -> tuple[int, int, int, int]:
        ...

    @abstractmethod
    def getTmpDir(self) -> Path:
        ...

    @abstractmethod
    def generateUID(self) -> str:
        ...

    @abstractmethod
    def getInputData(self, instanceUUID: str) -> Dataset:
        ...

    @abstractmethod
    def notifyOutputAvailable(self, instanceUUID: str, lastData: bool) -> bool:
        """Announce an output, which the Host takes with getOutputData().

        Unlike notifyInputAvailable() there are no main tags: the Host asks for
        the dataset from inside this call, so anything it wants to know about
        the output it reads there, and an Application is not made to summarise
        what it is about to hand over whole.
        """
        ...

    @abstractmethod
    def notifyStateChanged(self, value: State) -> None:
        ...

    @abstractmethod
    def notifyStatus(self, value: Status, text: str) -> None:
        ...


class ReportingHost(Host):
    """A Host that keeps every status it is sent and prints a line for each.

    Shared by the Hosts that report for themselves rather than forwarding, so
    that how a bare int or a wrong enum is reported is decided once. They
    differ only in where the lines go, which is _printLine(); `messages` is
    theirs to create, first thing, so that their own set-up can report.
    """

    messages: list[tuple[Status, str]]

    def _printLine(self, line: str) -> None:
        print(line, file=sys.stderr)

    def notifyStateChanged(self, value: State) -> None:
        state, problem = asEnum(State, value)
        if problem is not None:
            self.notifyStatus(Status.WARNING if state is not None else Status.ERROR, problem)
        if state is not None:
            self._printLine(f"[state] {state.name}")

    def notifyStatus(self, value: Status, text: str) -> None:
        status, problem = asEnum(Status, value)
        # The message is kept whatever it was sent as: an unknown level is
        # recorded as an error, so it can neither vanish nor pass as benign.
        # Not `status or ERROR`: INFORMATION is 0.
        level = status if status is not None else Status.ERROR
        self.messages.append((level, text))
        self._printLine(f"[{level.name}] {text}")
        if problem is not None:
            self.notifyStatus(Status.WARNING if status is not None else Status.ERROR, problem)


class Application(ABC):

    @abstractmethod
    def __init__(self, host: Host):
        ...

    @abstractmethod
    def getOutputData(self, instanceUUID: str) -> Dataset:
        """The output the Application announced with notifyOutputAvailable().

        The Host calls this from inside that notification and never after it
        returns, so an Application is free to build its output on demand and
        drop it as soon as the call is over. This interface has no
        releaseData() to say otherwise -- PS3.19 gets that answer from the
        transition to IDLE, and there is no such transition here -- so a Host
        that wants to hold output back keeps it itself rather than asking
        again later.
        """
        ...

    @abstractmethod
    def notifyInputAvailable(self, instanceUUID: str, mainTags: dict[str, object], lastData: bool) -> bool:
        ...

    @abstractmethod
    def bringApplicationToFront(self) -> bool:
        ...
