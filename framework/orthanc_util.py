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


def _orthancHash(text: str) -> str:
    """Orthanc identifier: SHA-1 of text, formatted as 5 groups of 8 hex chars."""
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()  # 40 lowercase hex chars
    return "-".join(digest[i:i + 8] for i in range(0, 40, 8))


def patientUUID(patientID: str) -> str:
    return _orthancHash(patientID)


def studyUUID(patientID: str, studyUID: str) -> str:
    return _orthancHash(f"{patientID}|{studyUID}")


def seriesUUID(patientID: str, studyUID: str, seriesUID: str) -> str:
    return _orthancHash(f"{patientID}|{studyUID}|{seriesUID}")


def instanceUUID(patientID: str, studyUID: str, seriesUID: str, sopInstanceUID: str) -> str:
    return _orthancHash(f"{patientID}|{studyUID}|{seriesUID}|{sopInstanceUID}")


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
