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

import hashlib
import re
from collections.abc import Collection, Container

from pydicom.dataset import Dataset

from .enums import Status

_ORTHANC_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{8}){4}")


def _orthancHash(text: str) -> str:
    """Orthanc identifier: SHA-1 of text, formatted as 5 groups of 8 hex chars."""
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()  # 40 lowercase hex chars
    return "-".join(digest[i:i + 8] for i in range(0, 40, 8))


def isOrthancID(text: str) -> bool:
    """Whether text has the shape of an Orthanc identifier, as _orthancHash makes."""
    return _ORTHANC_ID.fullmatch(text) is not None


def patientUUID(patientID: str) -> str:
    return _orthancHash(patientID)


def studyUUID(patientID: str, studyUID: str) -> str:
    return _orthancHash(f"{patientID}|{studyUID}")


def seriesUUID(patientID: str, studyUID: str, seriesUID: str) -> str:
    return _orthancHash(f"{patientID}|{studyUID}|{seriesUID}")


def instanceUUID(patientID: str, studyUID: str, seriesUID: str, sopInstanceUID: str) -> str:
    return _orthancHash(f"{patientID}|{studyUID}|{seriesUID}|{sopInstanceUID}")


def asNumber(value: object) -> tuple:
    """An IS tag as a sortable number; anything unusable sorts last.

    These arrive as strings, so comparing them as written would put "10"
    before "2". A missing or malformed number cannot be guessed at, and
    pretending it is 0 would push those values in front of the numbered ones
    instead of after them.
    """
    try:
        return (0, int(str(value).strip()))
    except (TypeError, ValueError):
        return (1, 0)


PATIENT_TAGS = [
    "PatientName",
    "PatientID",
    "PatientBirthDate",
    "PatientSex",
    "OtherPatientIDs",
]

STUDY_TAGS = [
    "StudyDate",
    "StudyTime",
    "StudyID",
    "StudyDescription",
    "AccessionNumber",
    "StudyInstanceUID",
    "RequestedProcedureDescription",
    "InstitutionName",
    "RequestingPhysician",
    "ReferringPhysicianName",
]

SERIES_TAGS = [
    "SeriesDate",
    "SeriesTime",
    "Modality",
    "Manufacturer",
    "StationName",
    "SeriesDescription",
    "BodyPartExamined",
    "SequenceName",
    "ProtocolName",
    "SeriesNumber",
    "CardiacNumberOfImages",
    "ImagesInAcquisition",
    "NumberOfTemporalPositions",
    "NumberOfSlices",
    "NumberOfTimeSlices",
    "SeriesInstanceUID",
    "ImageOrientationPatient",
    "SeriesType",
    "OperatorsName",
    "PerformedProcedureStepDescription",
    "AcquisitionDeviceProcessingDescription",
    "ContrastBolusAgent",
]

INSTANCE_TAGS = [
    "InstanceCreationDate",
    "InstanceCreationTime",
    "AcquisitionNumber",
    "ImageIndex",
    "InstanceNumber",
    "NumberOfFrames",
    "TemporalPositionIdentifier",
    "SOPInstanceUID",
    "ImagePositionPatient",
    "ImageComments",
    "ImageOrientationPatient",
]


# All main DICOM tags, in Patient -> Study -> Series -> Instance order.
# ImageOrientationPatient is a main tag at both the series and instance level,
# so the concatenation is de-duplicated while keeping that order.
MAIN_TAGS = list(
    dict.fromkeys(PATIENT_TAGS + STUDY_TAGS + SERIES_TAGS + INSTANCE_TAGS)
)


def instanceUUIDFor(ds: Dataset) -> str:
    """Orthanc instance UUID derived from the dataset's DICOM identifiers."""
    return instanceUUID(
        str(ds.get("PatientID", "")),
        str(ds.get("StudyInstanceUID", "")),
        str(ds.get("SeriesInstanceUID", "")),
        str(ds.get("SOPInstanceUID", "")),
    )


def outputProblems(
    ds: Dataset,
    instanceUUID: str,
    inputUUIDs: Container[str],
    announced: Container[str],
    patientIDs: Collection[str],
    studyUIDs: Collection[str],
) -> list[tuple[Status, str]]:
    """What is wrong with an output a Host is about to take, as status lines.

    An ERROR among them means the Host refuses the output; a WARNING only
    reports. Shared by every Host so that each checks its output the same way,
    and cheap enough for every output: one SHA-1 and a few lookups.

    `announced` is the output UUIDs already taken this run. `patientIDs` and
    `studyUIDs` are those of the inputs; empty means there are no inputs to
    compare against, and the check is skipped.
    """
    if len(ds) == 0:
        # Nothing else can be said about no data, and "could not encode" would
        # point at the wrong cause.
        return [(Status.ERROR, f"announced output {instanceUUID} but returned no data")]

    problems: list[tuple[Status, str]] = []
    if instanceUUID in announced:
        # Every sink is named after the UUID, so a second output under it
        # overwrites the first, in outputDir and in the staging spill alike.
        problems.append(
            (Status.ERROR, f"output {instanceUUID} was already announced in this run"))
    identity = instanceUUIDFor(ds)
    if identity in inputUUIDs:
        # Orthanc would drop it as AlreadyStored, or overwrite the original.
        problems.append(
            (Status.ERROR, f"output {instanceUUID} has the identity of input "
                           f"{identity}; it needs a new SOPInstanceUID"))
    elif identity != instanceUUID:
        # Not required by the interface, but it is the name every sink uses,
        # and Orthanc will store the output as `identity` whatever it is called.
        problems.append(
            (Status.WARNING, f"output announced as {instanceUUID} has the "
                             f"identity {identity}"))
    patientID = str(ds.get("PatientID", ""))
    if patientIDs and patientID not in patientIDs:
        problems.append(
            (Status.WARNING, f"output {instanceUUID} names patient {patientID!r}, "
                             "who is not among the inputs"))
    studyUID = str(ds.get("StudyInstanceUID", ""))
    if studyUIDs and studyUID not in studyUIDs:
        problems.append(
            (Status.WARNING, f"output {instanceUUID} names study {studyUID!r}, "
                             "which is not among the inputs"))
    return problems


def extractMainTags(ds: Dataset) -> dict[str, object]:
    """Pull the main DICOM tags present in ds into a {keyword: value} dict."""
    tags: dict[str, object] = {}
    for keyword in MAIN_TAGS:
        if keyword not in ds:
            continue
        value = ds.get(keyword)
        if value is None or str(value) == "":
            continue
        tags[keyword] = value
    return tags
