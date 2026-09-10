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

from abc import ABC, abstractmethod
from pathlib import Path

from pydicom.dataset import Dataset

from .enums import State, Status


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
    def notifyOutputAvailable(self, instanceUUID: str, mainTags: dict[str, object], lastData: bool) -> bool:
        ...

    @abstractmethod
    def notifyStateChanged(self, value: State) -> None:
        ...

    @abstractmethod
    def notifyStatus(self, value: Status, text: str) -> None:
        ...


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
