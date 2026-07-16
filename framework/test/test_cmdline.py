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
import tempfile
import unittest
from pathlib import Path

import pydicom

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cmdline import (  # noqa: E402
    CmdLineHost,
    CmdLineApplication,
    loadApplicationClass,
    extractMainTags,
    instanceUUIDFor,
)

DATA_FILE = Path(__file__).resolve().parent / "CT_small.dcm"


class TestCmdLineOutput(unittest.TestCase):
    def test_output_gets_new_instance_uid(self):
        inputDs = pydicom.dcmread(str(DATA_FILE))
        inputUID = str(inputDs.SOPInstanceUID)
        inputHandle = instanceUUIDFor(inputDs)

        with tempfile.TemporaryDirectory() as outputDir:
            outputPath = Path(outputDir)

            host = CmdLineHost([DATA_FILE], outputPath)
            app = CmdLineApplication(host)
            host.setApplication(app)

            # Instances are keyed by their Orthanc instance UUID, not SOPInstanceUID.
            uids = host.instanceUIDs
            self.assertEqual(uids, [inputHandle])
            for i, uid in enumerate(uids):
                app.notifyInputAvailable(uid, {}, lastData=(i == len(uids) - 1))

            outputFiles = list(outputPath.glob("*.dcm"))
            self.assertEqual(len(outputFiles), 1)
            outFile = outputFiles[0]

            outDs = pydicom.dcmread(str(outFile))
            outputUID = str(outDs.SOPInstanceUID)

            # The output must carry a fresh SOP Instance UID.
            self.assertNotEqual(outputUID, inputUID)
            # File-meta UID must stay consistent with the dataset UID.
            self.assertEqual(str(outDs.file_meta.MediaStorageSOPInstanceUID), outputUID)

            # Clean up the written file.
            outFile.unlink()
            self.assertFalse(outFile.exists())


class TestExtractMainTags(unittest.TestCase):
    def test_patient_name_extracted(self):
        ds = pydicom.dcmread(str(DATA_FILE))
        tags = extractMainTags(ds)

        self.assertIn("PatientName", tags)
        print(f"PatientName: {tags['PatientName']}")
        # The extracted value must match the dataset's PatientName.
        self.assertEqual(tags["PatientName"], ds.PatientName)


class TestLoadApplicationClass(unittest.TestCase):
    def test_loads_subclass_from_module(self):
        cls = loadApplicationClass("cmdline")
        self.assertIs(cls, CmdLineApplication)

    def test_module_without_application_raises(self):
        # "os" imports fine but defines no Application subclass.
        with self.assertRaises(SystemExit):
            loadApplicationClass("os")

    def test_unimportable_module_raises(self):
        with self.assertRaises(SystemExit):
            loadApplicationClass("no_such_module_xyz")


if __name__ == "__main__":
    unittest.main()
