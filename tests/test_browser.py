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

import fnmatch
import sys
import unittest
from pathlib import Path
from unittest import mock

from pydicom.dataset import Dataset

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from OrthancRC.base import Application  # noqa: E402
from OrthancRC import studies  # noqa: E402
from OrthancRC.selection import SEARCH_FIELDS  # noqa: E402
from OrthancRC.curses import browser  # noqa: E402


class RecordingApplication(Application):
    """A do-nothing Application that keeps the host it was handed."""

    def __init__(self, host) -> None:
        self.host = host

    def getOutputData(self, instanceUUID: str) -> Dataset:
        return Dataset()

    def notifyInputAvailable(self, instanceUUID, mainTags, lastData) -> bool:
        return True

    def bringApplicationToFront(self) -> bool:
        return False


class FakeHost:
    """Just enough Host for run_application; records the wiring."""

    def __init__(self, sendResult: bool = True) -> None:
        self.app = None
        self.sendResult = sendResult
        self.sent = False

    def setApplication(self, app) -> None:
        self.app = app

    def sendInputs(self) -> bool:
        self.sent = True
        return self.sendResult


class TestRunApplication(unittest.TestCase):
    def test_wires_application_to_host_and_sends(self):
        host = FakeHost()
        code = browser.run_application(host, RecordingApplication)

        self.assertEqual(code, 0)
        self.assertIsInstance(host.app, RecordingApplication)
        # The Application must receive the host it was registered with.
        self.assertIs(host.app.host, host)
        self.assertTrue(host.sent)

    def test_refused_input_is_a_nonzero_exit(self):
        host = FakeHost(sendResult=False)
        self.assertEqual(browser.run_application(host, RecordingApplication), 1)


def _record(uid: str = "u1", **overrides) -> browser.StudyRecord:
    fields = dict(
        uid=uid,
        study_instance_uid=f"1.2.{uid}",
        patient_id="PID",
        patient_surname="Doe",
        patient_given_name="Jane",
        patient_name_display="Doe Jane",
        dob="19700101",
        study_date="20260101",
        accession="ACC",
        description="desc",
    )
    fields.update(overrides)
    return browser.StudyRecord(**fields)


class HostFromBrowserTest(unittest.TestCase):
    """The picker and the Host built over what it returns, as one call."""

    def _pick(self, selected, records=None, criteria=None):
        records = records if records is not None else [_record("s1"), _record("s2")]
        with (
            mock.patch.object(browser, "fetch_all_studies",
                              return_value=records) as fetch,
            mock.patch.object(browser, "OrthancHost") as hostClass,
            mock.patch.object(browser.curses, "wrapper",
                              return_value=selected) as wrapper,
        ):
            host = browser.host_from_browser("client", criteria, uploadOutputs=False)
        return host, hostClass, fetch, wrapper

    def test_the_host_is_built_over_the_checked_studies_in_row_order(self):
        host, hostClass, fetch, _wrapper = self._pick({"s2", "s1"})

        self.assertIs(host, hostClass.return_value)
        args, kwargs = hostClass.call_args
        self.assertEqual(args, ("client", ["s1", "s2"]))
        # Anything else given goes on to the Host.
        self.assertFalse(kwargs["uploadOutputs"])
        # Without criteria every study on the server is offered.
        self.assertIsNone(fetch.call_args[0][1])

    def test_a_cancelled_picker_builds_no_host(self):
        host, hostClass, _fetch, _wrapper = self._pick(None)

        self.assertIsNone(host)
        hostClass.assert_not_called()

    def test_criteria_reach_the_server_and_narrow_the_rows_again(self):
        records = [_record("s1", patient_id="PID001"),
                   _record("s2", patient_id="OTHER")]
        _host, _hostClass, fetch, wrapper = self._pick(
            {"s1"}, records=records, criteria={"patient_id": "PID001"},
        )

        self.assertEqual(fetch.call_args[0][1], {"patient_id": "PID001"})
        # The find is only a widening, so the rows are filtered locally too.
        offered = wrapper.call_args[0][1]
        self.assertEqual([r.uid for r in offered], ["s1"])


class TestMainWithModule(unittest.TestCase):
    """main() with --module, against a fake Orthanc and a stubbed picker."""

    def _run(self, argv, selected):
        records = [_record("s1"), _record("s2")]
        with (
            mock.patch.object(browser, "Orthanc") as orthanc,
            mock.patch.object(browser, "loadApplicationClass",
                              return_value=RecordingApplication),
            mock.patch.object(browser, "fetch_all_studies", return_value=records),
            mock.patch.object(browser.curses, "wrapper", return_value=selected),
            mock.patch.object(browser, "OrthancHost") as hostClass,
        ):
            host = FakeHost()
            hostClass.return_value = host
            code = browser.main(argv)
        return code, orthanc, hostClass, host

    def test_selection_is_handed_to_the_application(self):
        code, _orthanc, hostClass, host = self._run(
            ["--module", "fake_app"], selected={"s1"},
        )

        self.assertEqual(code, 0)
        # The host is built over the checked studies only, in browser row order.
        args, kwargs = hostClass.call_args
        self.assertEqual(args[1], ["s1"])
        # Uploading results back into Orthanc is the default.
        self.assertTrue(kwargs["uploadOutputs"])
        self.assertIsNone(kwargs["outputDir"])
        self.assertTrue(host.sent)

    def test_row_order_is_kept_not_set_order(self):
        _code, _orthanc, hostClass, _host = self._run(
            ["--module", "fake_app"], selected={"s2", "s1"},
        )

        args, _kwargs = hostClass.call_args
        self.assertEqual(args[1], ["s1", "s2"])

    def test_output_dir_and_no_upload_reach_the_host(self):
        _code, _orthanc, hostClass, _host = self._run(
            ["--module", "fake_app", "--output-dir", "/tmp/out", "--no-upload"],
            selected={"s1", "s2"},
        )

        _args, kwargs = hostClass.call_args
        self.assertEqual(kwargs["outputDir"], Path("/tmp/out"))
        self.assertFalse(kwargs["uploadOutputs"])

    def test_cancelled_selection_never_starts_the_application(self):
        code, _orthanc, hostClass, _host = self._run(
            ["--module", "fake_app"], selected=None,
        )

        self.assertEqual(code, 1)
        hostClass.assert_not_called()

    def test_without_module_no_host_is_built(self):
        code, _orthanc, hostClass, _host = self._run([], selected={"s1"})

        self.assertEqual(code, 0)
        hostClass.assert_not_called()


class TestModuleIsLoadedBeforeSelecting(unittest.TestCase):
    def test_bad_module_fails_before_touching_orthanc(self):
        with (
            mock.patch.object(browser, "Orthanc") as orthanc,
            mock.patch.object(browser, "fetch_all_studies") as fetch,
        ):
            with self.assertRaises(SystemExit):
                browser.main(["--module", "no_such_module_xyz"])

        # A bad --module must not cost the user a search and a selection.
        orthanc.assert_not_called()
        fetch.assert_not_called()


def _server_would_match(query, record) -> bool:
    """Apply an Orthanc /tools/find Query to a StudyRecord the way the server
    would: case insensitive DICOM wildcards, and ranges on StudyDate."""
    values = {
        "PatientID": record.patient_id,
        "PatientName": f"{record.patient_surname}^{record.patient_given_name}",
        "PatientBirthDate": record.dob,
        "AccessionNumber": record.accession,
        "StudyDescription": record.description,
    }
    for tag, pattern in query.items():
        if tag == "StudyDate":
            low, _, high = pattern.partition("-")
            if not record.study_date:
                return False
            if low and record.study_date < low:
                return False
            if high and record.study_date > high:
                return False
            continue
        if not fnmatch.fnmatch(values[tag].lower(), pattern.lower()):
            return False
    return True


class SearchFieldsTest(unittest.TestCase):
    """The one table behind the --search-* options, the criteria and matches()."""

    def test_every_field_reaches_the_criteria_from_its_own_option(self):
        args = browser.parse_args([
            "--search-patient-id", "PID001",
            "--search-patient-surname", "Smith",
            "--search-patient-givenname", "John",
            "--search-dob", "1980-01-01",
            "--search-study-after", "2020-01-01",
            "--search-study-before", "20201231",
            "--search-accession", "ACC42",
            "--search-description", "HEAD",
        ])

        # Dates arrive in either spelling and are stored as DICOM DA.
        self.assertEqual(browser.build_criteria(args), {
            "patient_id": "PID001",
            "patient_surname": "Smith",
            "patient_givenname": "John",
            "dob": "19800101",
            "study_after": "20200101",
            "study_before": "20201231",
            "accession": "ACC42",
            "description": "HEAD",
        })

    def test_an_unused_option_leaves_its_criterion_unset(self):
        criteria = browser.build_criteria(browser.parse_args([]))
        self.assertEqual(set(criteria), {field.key for field in SEARCH_FIELDS})
        self.assertEqual(set(criteria.values()), {None})

    def test_every_field_names_a_record_attribute_that_exists(self):
        record = _record()
        for field in SEARCH_FIELDS:
            self.assertTrue(hasattr(record, field.attr), field.key)


class MatchesTest(unittest.TestCase):
    """What each criterion accepts, once it is set."""

    def _matches(self, record, **criteria) -> bool:
        return studies.matches(record, criteria)

    def test_no_criteria_matches_every_study(self):
        self.assertTrue(studies.matches(_record(), {}))

    def test_text_criteria_are_case_insensitive_substrings(self):
        record = _record()
        self.assertTrue(self._matches(record, patient_id="pi"))
        self.assertTrue(self._matches(record, patient_surname="do"))
        self.assertTrue(self._matches(record, patient_givenname="ANE"))
        self.assertTrue(self._matches(record, accession="cc"))
        self.assertTrue(self._matches(record, description="DES"))
        self.assertFalse(self._matches(record, description="chest"))

    def test_the_two_name_criteria_narrow_different_halves_of_the_name(self):
        record = _record(patient_surname="Doe", patient_given_name="Jane")
        self.assertFalse(self._matches(record, patient_givenname="Doe"))
        self.assertFalse(self._matches(record, patient_surname="Jane"))

    def test_a_date_of_birth_is_matched_exactly(self):
        record = _record(dob="19700101")
        self.assertTrue(self._matches(record, dob="19700101"))
        # Not a prefix and not a substring: a date is the whole value or nothing.
        self.assertFalse(self._matches(record, dob="1970010"))

    def test_the_study_date_bounds_include_their_own_day(self):
        record = _record(study_date="20260101")
        self.assertTrue(self._matches(record, study_after="20260101"))
        self.assertTrue(self._matches(record, study_before="20260101"))
        self.assertFalse(self._matches(record, study_after="20260102"))
        self.assertFalse(self._matches(record, study_before="20251231"))

    def test_a_study_without_a_date_fails_any_bound(self):
        record = _record(study_date="")
        self.assertFalse(self._matches(record, study_after="20200101"))
        self.assertFalse(self._matches(record, study_before="20301231"))
        # With no bound set it is still a hit.
        self.assertTrue(self._matches(record, description="desc"))

    def test_every_criterion_set_must_match(self):
        record = _record()
        both = {"patient_surname": "Doe", "description": "desc"}
        self.assertTrue(studies.matches(record, both))
        self.assertFalse(studies.matches(record, {**both, "description": "chest"}))


class BuildQueryTest(unittest.TestCase):
    """build_query() must narrow the find without ever excluding a hit."""

    def _criteria(self, **overrides):
        base = {field.key: None for field in SEARCH_FIELDS}
        base.update(overrides)
        return base

    def test_no_criteria_queries_everything(self):
        self.assertEqual(studies.build_query(None), {})
        self.assertEqual(studies.build_query(self._criteria()), {})

    def test_substring_criteria_become_wildcards(self):
        query = studies.build_query(self._criteria(
            patient_id="123", accession="ACC", description="HEAD",
        ))
        self.assertEqual(query, {
            "PatientID": "*123*",
            "AccessionNumber": "*ACC*",
            "StudyDescription": "*HEAD*",
        })

    def test_dob_is_matched_exactly(self):
        query = studies.build_query(self._criteria(dob="19800101"))
        self.assertEqual(query["PatientBirthDate"], "19800101")

    def test_date_bounds_become_a_dicom_range(self):
        both = studies.build_query(self._criteria(
            study_after="20200101", study_before="20201231",
        ))
        self.assertEqual(both["StudyDate"], "20200101-20201231")

        after = studies.build_query(self._criteria(study_after="20200101"))
        self.assertEqual(after["StudyDate"], "20200101-")

        before = studies.build_query(self._criteria(study_before="20201231"))
        self.assertEqual(before["StudyDate"], "-20201231")

    def test_name_criteria_share_one_widened_patient_name(self):
        surname = studies.build_query(self._criteria(patient_surname="Smith"))
        self.assertEqual(surname["PatientName"], "*Smith*")

        given = studies.build_query(self._criteria(patient_givenname="John"))
        self.assertEqual(given["PatientName"], "*John*")

        # Only one pattern can be sent for the single PatientName tag; the
        # given name is left to matches().
        both = studies.build_query(self._criteria(
            patient_surname="Smith", patient_givenname="John",
        ))
        self.assertEqual(both["PatientName"], "*Smith*")

    def test_query_never_excludes_a_study_matches_accepts(self):
        """The whole point of the widening: no hit is lost server side."""
        record = studies.StudyRecord(
            uid="s1",
            study_instance_uid="1.2.3",
            patient_id="PID123",
            patient_surname="Smith",
            patient_given_name="John",
            patient_name_display="Smith John",
            dob="19800101",
            study_date="20200601",
            accession="ACC9",
            description="CT HEAD",
        )
        criteria = self._criteria(
            patient_id="id12", patient_surname="smi", patient_givenname="ohn",
            dob="19800101", study_after="20200101", study_before="20201231",
            accession="cc9", description="head",
        )
        self.assertTrue(studies.matches(record, criteria))

        # Replaying the query the way Orthanc would still returns the study.
        self.assertTrue(_server_would_match(studies.build_query(criteria), record))

    def test_query_keeps_a_hit_the_given_name_alone_would_lose(self):
        """PatientName carries the surname pattern, but a study matching on
        the given name only must still survive the server side filter."""
        record = studies.StudyRecord(
            uid="s2", study_instance_uid="1.2.4", patient_id="PID9",
            patient_surname="Jones", patient_given_name="Smith",
            patient_name_display="Jones Smith", dob="", study_date="20200601",
            accession="", description="",
        )
        criteria = self._criteria(patient_givenname="Smith")

        self.assertTrue(studies.matches(record, criteria))
        self.assertTrue(_server_would_match(studies.build_query(criteria), record))


class FetchAllStudiesTest(unittest.TestCase):
    """Paging must survive a server that returns fewer rows than asked for."""

    @staticmethod
    def _entry(uid):
        return {
            "ID": uid,
            "MainDicomTags": {"StudyDescription": "CT"},
            "PatientMainDicomTags": {"PatientName": "Smith^John"},
        }

    def _client(self, pages):
        client = mock.Mock()
        client.post_tools_find.side_effect = pages
        return client

    def test_pages_until_an_empty_page(self):
        # A short page does not mean the end of the data: a server enforcing
        # its own find limit answers with fewer rows than Limit asked for.
        client = self._client([
            [self._entry(f"a{i}") for i in range(10)],
            [self._entry(f"b{i}") for i in range(10)],
            [],
        ])
        records = studies.fetch_all_studies(client)

        self.assertEqual(len(records), 20)
        self.assertEqual(client.post_tools_find.call_count, 3)

    def test_since_advances_by_rows_actually_returned(self):
        client = self._client([
            [self._entry(f"a{i}") for i in range(10)],
            [],
        ])
        studies.fetch_all_studies(client)

        sinces = [c.args[0]["Since"] for c in client.post_tools_find.call_args_list]
        # Advancing by FIND_PAGE_SIZE here would skip studies 10..999.
        self.assertEqual(sinces, [0, 10])

    def test_criteria_are_sent_to_the_server(self):
        client = self._client([[]])
        studies.fetch_all_studies(client, {"patient_id": "123"})

        body = client.post_tools_find.call_args.args[0]
        self.assertEqual(body["Query"], {"PatientID": "*123*"})
        self.assertFalse(body["CaseSensitive"])

    def test_a_server_ignoring_since_does_not_loop_forever(self):
        page = [self._entry("a1")]
        client = mock.Mock()
        client.post_tools_find.return_value = page

        records = studies.fetch_all_studies(client)

        self.assertEqual([r.uid for r in records], ["a1"])


if __name__ == "__main__":
    unittest.main()
