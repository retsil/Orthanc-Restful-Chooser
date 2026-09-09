
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
`OrthancRC.orthanc` and knows only a list of study UUIDs, so anything can pick
them. `OrthancHost.fromSelectionFile()` builds one from a saved selection, and
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
