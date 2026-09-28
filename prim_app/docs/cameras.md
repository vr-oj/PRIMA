# Camera connections in PRIMA 3.5.0

PRIMA offers native IC4 and Micro-Manager cameras in the same Camera dropdown.
Micro-Manager provides the option to use other camera brands through compatible
device adapters and vendor drivers. It does not guarantee support for every camera.
The connection and setup components are adapted from BURST; PRIMA retains its
own pressure recording, PRIM commands, and recording lifecycle.

## Connect another camera

1. Install a compatible 64-bit Micro-Manager installation and the vendor drivers
   required by its camera adapter. Confirm Live preview works in Micro-Manager,
   then close it and other camera applications.
2. In PRIMA's Camera dropdown, choose **Micro-Manager Camera Setup…**. Stop any
   active camera first. Choose the installation folder if it is not found.
3. Choose **Find cameras**, or **Load configuration…** to load a saved `.cfg`.
   Prefer a camera-only configuration: loading a configuration initializes every
   device listed in it. Refreshing PRIMA's camera list only lists saved connections;
   it does not load configurations or initialize devices.
4. Select the camera, choose **Add camera**, then **Save**. Choose that connection
   in the Camera dropdown and click **Start Camera**. Resolution is read from the
   saved configuration and updated from received images; configure sensor geometry
   in Micro-Manager. PRIMA's manual/auto exposure, gain, preview rate and pixel
   format controls are enabled according to the adapter's reported capabilities.
5. Before recording, connect the Arduino's electrical trigger to the camera's
   correct input and configure its input/edge for that apparatus. Match the PRIM
   pressure/capture workflow described in the main README. PRIMA prepares files,
   verifies camera arming, and only then sends its existing PRIM start sequence.

Profiles store paths and declarative settings in PRIMA's per-user configuration.
They do not copy or modify the original `.cfg`. Keep that file and its resources
in place. Advanced camera mapping supports unusual property names, units and
ordered preview/external-trigger settings; mappings contain data, not executable
scripts. The setup dialog also imports/exports profiles. Imported paths and
camera identifiers must be checked on the receiving computer.

## Timing and recording limits

Video requires a supported external-trigger interface or a validated saved timing
mapping. An unrecognized trigger interface may still preview; recording reports
the limitation before starting PRIM. PRIMA does not fall back to software pairing.
Recognized GenICam adapters retain their configured rising/falling edge and
physical input. TIScam uses its configured external input; properties it does not
expose are listed in the saved recording summary rather than claimed as verified.

PRIMA holds exposed automatic image controls, checks exposure and reported rate
against the requested trigger interval, and uses the highest reported compatible
rate where the adapter provides bounds and a writable control. If the adapter
does not expose rate, the summary explicitly states that only the exposure budget
could be checked. Image/trigger counts and overflow checks remain active. Stop
restores preview and its prior image controls; properties lock during recording.

MMCore image tags such as ImageNumber and ElapsedTime-ms are retained as adapter
metadata. They are not hardware frame IDs or exposure timestamps. PRIMA leaves
those hardware fields empty for this connection, checks pressure-trigger/image
counts and arrival order, and describes the limitation in the completion popup
and recording summary. IC4's existing hardware metadata checks remain strict.
Count agreement does not certify the physical pressure-to-exposure offset.

Micro-Manager supports unpacked Mono8/Mono16 (including lower bit depths stored
in uint16) and packed RGB32 converted to RGB8. Native monochrome depth is preserved
in TIFFs independently of the display. Other layouts fail explicitly. CSV columns
and TIFF pressure associations remain compatible. Playback and annotated exports
use the established 8-bit grayscale view; the source TIFF retains native pixels.

## Runtime and packaging

PRIMA pins `pymmcore==12.5.0.75.0`, matching the BURST bridge used for this port
(MMCore 12.5.0, device API 75). Adapters must match the core API and architecture;
vendor SDK installation alone does not supply a Micro-Manager adapter. Source
users install `prim_app/requirements.txt` into their launch environment. Packaged
releases advertising this feature must bundle the bridge; the build specification
collects it when installed. Development builds can still start without either SDK.

Adapter discovery and acquisition run in a separate helper process. Native adapter
errors, crashes, timeouts and cancellation are reported without taking down the
main window. Use **Details…** in setup for driver diagnostics. If required, set
`PRIMA_CAMERA_DLL_PATH` to vendor runtime folders separated by semicolons on Windows,
then reopen the terminal and PRIMA. No camera SDK or driver is installed by setup.

No non-IC4 camera was available during this implementation. Automated tests cover
the helper, simulated acquisition, arming failure, restoration, overflow, pixels,
TIFF/CSV matching, playback and startup without SDKs. Before using a new apparatus
for experiments, validate repeated preview/start/stop, an armed camera waiting with
no pulses, expected trigger/image counts, sparse capture, exposure/rate limits and
saved TIFF/CSV data using that actual camera and adapter.

References: [Micro-Manager Python integration](https://micro-manager.org/Using_the_Micro-Manager_python_library),
[supported adapters](https://micro-manager.org/Device_Support).
