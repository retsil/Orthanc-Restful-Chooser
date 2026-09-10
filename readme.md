
## Python Application Hosting

### Introduction

DICOM Part 19 describes the separation between host and application. This is described in https://dicom.nema.org/Dicom/2011/11_19pu.pdf. The standard defines Application and Host communication through SOAP calls, but that is overkill for most implementationd because everything is python.

### Python modules

This implementation expects that the end user starts the python host process. Python applications are loaded at runtime into the virtual environment of the Python host process. There will need to be configuration options which defines modules to be dynamically loaded at runtime. Each application module will need to have the static fields `__version__` `__icon__` and `__description__`

The `__init__` method of application module should have a single argument containing a clone of the host interface object.

### DICOM objects

Native objects in WG23 were designed in the standard to be any object, but for our purposes they are DICOM objects. Native objects have descriptors, conveniently the are identified by UUIDs so Orthanc UUIDs are a perfect fit. Object descriptions also have a SOP class, but I recommend using Orthanc main tags.

### Transferring data

In WG23, transferring data is a two step process which is performed at the instance level. The reason for this is because for large data sets, getting data cannot happen on the UI thread. Also, it permits the use of a data selector before transferring data or loading objects into memory.

I've simplified it to:
1. Host calls Application.notifyInputAvailable(UUID, mainTags)
2. Application calls Host.getInputData(UUID) returning a pydicom object

When returning data to the Host
1. Application calls Host.notifyOutputAvailable(UUID, mainTags)
2. Host calls Application.getOutputData(UUID) returning a pydicom object

mainTags for instances are defined in Orthanc. I would recommend including all main tags from Patient, Study, Series and Instance levels.
### Application state

Every application has a state and status. When the state changes it notifies the host via the Host.notifyStateChanged method. Sometimes an message is passed prior to state change. This can be done through the Host.notifyStatus(value, text) method.

- State Values can be IDLE, INPROGRESS, COMPLETED, SUSPENDED, CANCELED, EXIT
- Status Values can be INFORMATION, ERROR, WARNING, FATALERROR

### Windowing system

Some additional helper functions are defined in the XIP implementation of WG23. I think the following are a good idea

- Application.bringApplicationToFront()
- Host.getAvailableScreen()
- Host.getTmpDir();
- Host.generateUID();

### Example reference implementation.

Command line 
```
cd src
python3.13 -m OrthancRC.cmdline --input ../tests/CT_small.dcm --module OrthancRC.examples.clone
```

### Orthanc study browser

Search an Orthanc server and pick studies in a curses list. Without `--module`
it only prints (and optionally saves) the checked study UUIDs:

```
python3.13 -m OrthancRC.curses --orthanc-url http://localhost:8042 \
    --search-patient-surname doe --save-selection selection.json
```

With `--module` the selection is handed straight to an Application: an
`OrthancHost` is built over the checked studies and the Application subclass
found in that module is loaded, wired to the host, and fed every instance of
every selected study. Output datasets are uploaded back into Orthanc unless
`--no-upload` is given, and `--output-dir` also writes them to disk.

```
python3.13 -m OrthancRC.curses --module OrthancRC.examples.clone \
    --output-dir ./out --no-upload
```

The module is loaded before the search runs, so a bad `--module` fails
immediately rather than after studies have been selected.

#### Picking series

`s` opens a second picker over every study that is checked -- not the study
under the cursor -- listing all their series as one list so that a study can
be narrowed to some of them. It is the same idiom as the study list, so the
code and the muscle memory carry over: up/down, PgUp/PgDn, SPACE to toggle,
`a` and `n` over everything at once. The rows are grouped under a heading per
study for reading, but there is one cursor through the whole thing and the
series are in the order the host will offer their instances in.

ENTER means the same thing on both screens: it keeps the sub-selection and
confirms the browse, so narrowing the last study finishes rather than handing
back the study list to press ENTER on again. The two pickers differ in one
thing, and the footer says so: `q`/ESC discards the sub-selection and goes
back to the study list with the previous series intact. That abandons the
sub-selection, not the whole browse.

Everything is checked when the picker opens over a study that has no
sub-selection yet, because the whole study is what it currently means. A study
left with no series at all is un-checked in the study list instead -- no series
is no study -- and a study that only some series are taken from shows `[~]`
rather than `[x]`. Nothing else about the study list changes, and a run that
never presses `s` behaves exactly as it did before the key existed.

Listing series is one request per study, so `s` over a hundred checked studies
costs a hundred of them and says so while it waits; what it fetches is kept
for the session, so re-opening the picker is instant.

#### The selection file

`--save-selection` grows two optional keys, and a file written before they
existed still loads and still means what it always meant:

```json
{
  "criteria": { "...": null },
  "selection_level": "series",
  "study_uids": ["<study UUID>"],
  "series_uids": { "<study UUID>": ["<series UUID>"] }
}
```

A study missing from `series_uids` means the whole study -- whatever it holds
when the file is read -- and `selection_level` is the file saying which of the
two it is, so no reader has to guess from whether `series_uids` happens to be
there. A study the picker was opened over keeps its explicit series list even
when every series is checked: absent and complete-list are different
statements, and only the second records what was actually on the screen.

Both are Orthanc identifiers rather than DICOM UIDs, as `study_uids` already
was. That costs nothing in portability: an Orthanc identifier is a SHA-1 of
`patientID|studyUID|seriesUID`, which is Orthanc's own scheme, so the same
series has the same identifier on every Orthanc that holds it. What is lost is
that the hash is one-way -- the file cannot be read by eye or resolved by a
non-Orthanc system -- and that is the right price for a file whose reason to
exist is passing a selection between two processes looking at the same server.

`--restore-selection` is strict about all of it, and stays strict: a saved
study that no longer matches the criteria, or a saved series its study no
longer holds, is a message on standard error and a non-zero exit. There is no
partial restore and no prompt, because the file is the message of the simplest
IPC there is: a silently degraded selection is a corrupted one, and the sender
would never find out that half of it was dropped. Note that pinning full
series lists makes this bite more often -- any study the picker was opened over
now fails its restore if the archive has since lost one of its series -- which
is the strictness working as intended rather than a regression.

`OrthancHost.fromSelectionFile()` needs no study-versus-series argument for
the same reason: the file says. The download example works unchanged against a
file of either kind.

#### Reviewing output before it is written: `--stage`

`--stage` holds the Application's output back until a table of the output
series it produced has been confirmed:

```
python3.13 -m OrthancRC.curses --module OrthancRC.examples.clone \
    --stage --output-dir ./out
```

The table is one row per output series with a count of the output instances in
it -- which is not the size of the input series, since an Application may emit
one output per input, or fewer, or more -- and every row starts accepted. So
`--stage` is a review step rather than an opt-in: confirming the table
untouched does exactly what the same run without `--stage` would have done,
and rejecting is the action. There is no cancel on that screen, because the
run is already over and there would be nothing to cancel to; the ways out are
ENTER and an explicit `n`. A series that is rejected is never written and
never uploaded, and `getOutputData()` is never called for it.

The Application sees no change at all. Its `getOutputData()` is called once,
synchronously, from inside its own `notifyOutputAvailable()`, exactly where it
is called without `--stage`, so an Application that builds its output on
demand and frees it when the call returns works staged too. That is deliberate
rather than incidental: this interface has no `releaseData()`, so nothing
would tell an Application when it may free an output the Host asked it to
keep, and a Host that deferred the call until after the run would be asking
for data every reasonable Application has already dropped. The Host therefore
takes the output while the call is in its hands and takes responsibility for
it afterwards.

Which means holding it. Encoded output is kept in memory up to
`--output-cache` (a byte count with an optional `K`/`M`/`G` suffix, default
`128M`) and written to a spill file in the host's temporary directory past
that, deleted at the end of the review whether its series was accepted or
rejected. 128 MB is roughly 250 512x512 16-bit CT slices, two or three typical
series; a large single frame -- mammography, whole-slide -- spills almost at
once, which is the right outcome for output that should not be accumulating in
a Python process. `--output-cache 0` spills everything. What was held and what
was spilled is reported at the end of a run, since that is the only way to
find out the number was too low for the work. It is a ceiling on output held,
on top of the `prefetchDepth` decoded input datasets the prefetch window may
hold.

None of that lives in the terminal front end. `StagingHost` wraps an
`OrthancHost` and intercepts exactly one call, forwarding everything else, so
it has one tmp dir and one message list between the two; `curses` contributes
the y/n screen and nothing more.

Taking an instance's data starts the download of the next few on a small
thread pool, so the following `getInputData()` usually only waits for a request
that is already in flight. The window is `OrthancHost(..., prefetchDepth=2)`
instances deep; `prefetchDepth=0` turns it off and fetches each instance at the
moment it is asked for.

It is worth having: downloading a selection over a local network measured
about 11 MB/s with `prefetchDepth=0` and about 22 MB/s at the default depth of
2, as reported by the download example's own rate (see below). Serial fetching
spends most of a run waiting for the server to answer, and that is the time the
pool fills.

Prefetching follows what the Application takes, not where `sendInputs()` has
got to, so it costs nothing for an Application that filters on main tags and
asks for few instances, and it works just as well for one that reads every main
tag first and only asks for the data afterwards -- from the last input, or once
the run is over. In that last case call `host.close()` when done, to stop the
pool `sendInputs()` is no longer around to close.

The Host itself is not part of the terminal front end: `OrthancHost` lives in
`OrthancRC.orthanc` and knows only a list of study UUIDs -- and, optionally,
which series to take from each -- so anything can pick them. `OrthancHost.fromSelectionFile()` builds one from a saved selection, and
`OrthancRC.curses.browser.host_from_browser()` is the whole picker as a single
call for a caller that wants a ready-made Host rather than a command line.

### Download example application

An example Application with its own command line. It takes a study selection
saved earlier by the browser, watches the main tags the host offers for each
instance, and downloads only the series matching every criterion given:
`--match-modality` (exact, case-insensitive), `--match-series-description`
(substring, case-insensitive) and `--match-series-instance-uid` (exact). A
criterion left unset matches every series, so with none of them the whole
selection is downloaded into `--target-folder`:

```
python3.13 -m OrthancRC.examples.download \
    --from-selection-file selection.json \
    --match-modality CT --match-series-description head \
    --target-folder ./series
```

`--match-series-instance-uid` picks out one known series by its
SeriesInstanceUID, rather than describing it:

```
python3.13 -m OrthancRC.examples.download \
    --from-selection-file selection.json \
    --match-series-instance-uid 1.2.840.113619.2.55.3.604688.1 \
    --target-folder ./series
```

The instances of the matching series are written straight into the target
folder as `<SOPInstanceUID>.dcm`. If more than one series matches, the second
goes to `<target-folder>-1`, the third to `<target-folder>-2`, and so on, in
the order the series are first seen -- which from `OrthancHost` is SeriesNumber
order, so the same selection always lands in the same folders. Instances of non-matching series are
filtered on their main tags alone, so they are never pulled from Orthanc.

While it works, the download reports how far it has got on standard error, as
a percentage and a bar over the instances the host has to offer, followed by
the rate at which data is arriving:

```
[############------------------]  42% (42/100)  3.1 MiB/s
```

On a terminal that one line is rewritten in place; when standard error is
redirected it is printed whole every 10% instead, so a captured run stays
readable. Progress counts every instance offered, matching or not, since that
is the work the run has to get through; `--no-progress` turns it off. A host
that does not say how large the selection is leaves nothing to take a
percentage of, and the download then simply reports no progress.

The rate counts only the bytes of the instances actually downloaded -- a
skipped series costs no bandwidth -- over the whole run so far, waiting for
the server included, so it is what the selection is really coming down at
rather than the speed of any one transfer. It is measured whether or not a bar
is drawn, and the closing summary ends with the average for the run:

```
downloaded 42 instance(s) in 2 series into ./series, skipped 58, failed 0, at 3.1 MiB/s
```

It reuses `OrthancHost` (through `OrthancHost.fromSelectionFile`) and adds
only its own `Application`; that Application can equally be driven from the
browser with
`--module OrthancRC.examples.download`, which then downloads every selected
instance into the host's temporary directory.

### Testing

```
python3.13 -m unittest discover -s tests -t tests
```
