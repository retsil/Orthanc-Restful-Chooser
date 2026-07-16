
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
python3.13 cmdline.py --input test/CT_small.dcm --module cmdline
```

### Testing

```
python3.13 test/test_cmdline.py
```
