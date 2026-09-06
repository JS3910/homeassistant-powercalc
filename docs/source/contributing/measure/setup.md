# CLI (Docker / native Python)

This page covers running the measure tool from the command line, either with Docker or natively with Python.

!!! tip

    On Home Assistant OS, the [Home Assistant app](home-assistant-app.md) is the recommended way to run measurements. Use the CLI when you run Home Assistant Container or Core, need a direct device power meter or Hue controller, use an OCR meter or manual readings, or develop the tool itself.

## Prepare a working directory

Create a separate directory for your measurement work, for example `powercalc-measure`. Copy `utils/measure/.env.dist` from the Powercalc repository into that directory and rename it to `.env`.

The working directory will contain:

- `.env` - your local configuration and credentials.
- `export/` - generated measurement files.
- `.persistent/` - resume data and cached dummy load measurements.

Do not commit your `.env` file. It can contain Home Assistant tokens or device keys.

## Docker

Install [Docker](https://docs.docker.com/get-docker/) and verify it is running:

```bash
docker version
```

Run the tool from the directory that contains `.env`.

=== "Linux and macOS"

    ```bash
    docker run --pull=always --rm --name=measure --env-file=.env -v $(pwd)/export:/app/export -v $(pwd)/.persistent:/app/.persistent -it bramgerritsen/powercalc-measure-cli:latest
    ```

=== "Windows command prompt"

    ```bat
    docker run --pull=always --rm --name=measure --env-file=.env -v %CD%/export:/app/export -v %CD%/.persistent:/app/.persistent -it bramgerritsen/powercalc-measure-cli:latest
    ```

When using PowerShell, use full absolute paths for the mounted directories.

## Native Python

Use the native setup when Docker does not work for you or when you are developing the measure tool itself.

Prerequisites:

- Python 3.14 or newer.
- `uv`, installed with `curl -LsSf https://astral.sh/uv/install.sh | sh` or another method from the uv documentation.

From the repository:

```bash
cd utils/measure
uv venv
uv sync --extra cli
uv run --extra cli python -m measure.measure
```

Native runs write output to `utils/measure/export`.

## Required configuration

At minimum, choose a power meter and the controller for the kind of device you measure.

```env
POWER_METER=hass
LIGHT_CONTROLLER=hass
MEDIA_CONTROLLER=hass
FAN_CONTROLLER=hass
CHARGING_CONTROLLER=hass
```

Supported power meters:

| `POWER_METER` | When to use |
| --- | --- |
| `hass` | Recommended general option. Reads a Home Assistant power sensor. |
| `shelly` | Reads directly from a Shelly device API. |
| `tasmota` | Reads directly from a Tasmota device. |
| `tuya` | Reads directly from a Tuya plug. |
| `kasa` | Reads directly from a TP-Link Kasa plug. |
| `mystrom` | Reads directly from a myStrom plug. |
| `manual` | Prompts you to enter readings manually. |
| `ocr` | Reads a meter display through OCR. See [OCR power meter](#ocr-power-meter). |

The `hass` power meter is often the easiest and most reliable path because it can use any power sensor Home Assistant already exposes.

## Home Assistant configuration

For `POWER_METER=hass` or any `hass` controller, set:

```env
HASS_URL=ws://homeassistant.local:8123/api/websocket
HASS_TOKEN=your_long_lived_access_token
```

The tool uses the Home Assistant WebSocket API. Older REST-style URLs (e.g. `http://homeassistant.local:8123/api`) are automatically normalized to the WebSocket endpoint, so existing configurations keep working.

For the power meter, the tool asks you to select a power sensor with unit `W`. When voltage readings are needed, it can also use a voltage sensor with unit `V`.

Set this when your power sensor does not update frequently enough:

```env
HASS_CALL_UPDATE_ENTITY_SERVICE=true
```

A sensor whose device has dropped off the network keeps its last value in Home Assistant until the integration marks it unavailable, which can take minutes. To reject such readings, set the maximum age of the sensor's last report. The timestamp used is `last_reported`, which advances every time the integration writes the state even when the value did not change, so this limit has to be larger than the sensor's normal reporting interval (including any heartbeat interval configured in the device firmware).

```env
HASS_MAX_AGE_SECONDS=60
```

## Direct power meter configuration

Set only the variables needed by your selected `POWER_METER`.

```env
SHELLY_IP=x.x.x.x
SHELLY_TIMEOUT=60

TASMOTA_DEVICE_IP=x.x.x.x

KASA_DEVICE_IP=x.x.x.x

MYSTROM_DEVICE_IP=x.x.x.x

TUYA_DEVICE_ID=aaaaaaaaad89682385bbb
TUYA_DEVICE_IP=x.x.x.x
TUYA_DEVICE_KEY=aaaaaaaae1b8abb
TUYA_DEVICE_VERSION=3.3
```

For Tuya measuring devices, make sure no other integration is connected to the same device while measuring. Some Tuya plugs only allow one local connection at a time.

## Witness meters

You can read one or more additional meters at the same instant as the primary `POWER_METER` and only accept a sample when they agree. Every sample is logged with all readings, and a sample that any witness contradicts is retried exactly like a failed reading. This catches a misread on the primary (for example a dropped decimal point on a display read by camera) with a second, independent measurement.

```env
POWER_METER=ocr
WITNESS_METERS=shelly
```

Each witness is configured through the same variables as when it is the primary (`SHELLY_IP` and so on), so a meter type can appear once. Per witness, replacing `SHELLY` with the type in upper case:

```env
# Subtracted from the witness reading before comparing. Use it when the witness is wired
# upstream of the primary and therefore also measures the primary meter's own consumption.
WITNESS_SHELLY_OFFSET_W=2.45
# A witness agrees when its corrected reading is within max(these two) of the primary.
WITNESS_SHELLY_TOLERANCE_W=0.5
WITNESS_SHELLY_TOLERANCE_PCT=2
# Set to false to only log a witness that cannot be read, instead of failing the sample.
WITNESS_SHELLY_REQUIRED=true
```

Only the primary's reading is recorded; witnesses decide whether it is trusted. Voltage, when used, also comes from the primary.

The Home Assistant app's settings screen offers the same thing under **Power meter →
Witness meters**: pick any meter type (except Manual, which needs an interactive prompt
the app has no console for) as the primary, then add one or more witness rows, each with
its own type, address, offset, and tolerances. A composite meter — the primary plus its
witnesses — is what gets used for the session once at least one witness row exists;
provenance (each witness's type, offset, and tolerances) is written into the resulting
`model.json`'s `measure_settings.WITNESSES`, which existing Powercalc installs ignore
safely since they read `measure_settings` as an unstructured dict.

## OCR power meter

With `POWER_METER=ocr`, the tool reads a physical power meter by pointing a camera at its display. This is useful for a bench meter that has no network interface. Nothing else needs to run: the camera is read inside the measure tool itself.

Install the optional OCR dependencies once. They are pure Python wheels (the text recognition models run on ONNX Runtime), so there is no system package to install:

```bash
cd utils/measure
uv sync --extra cli --extra ocr
```

Then point the tool at the camera:

```env
POWER_METER=ocr
# A local camera index ("0" is the first USB camera), a stream URL (for example the MJPEG
# stream of an ESPHome camera) or a path to a video file.
OCR_SOURCE=http://camera.local:8080/
```

The tool finds the display by itself. Whenever it starts, or when readings keep failing or the picture changes a lot (someone moved the meter or the camera), it detects all the text in the frame, levels the picture by the angle of that text and matches the rows to the meter's fields. The display therefore does not have to be aligned with the camera; it can be tilted or even upside down. After that, every frame is read within the located regions only, about 2 frames per second on a typical laptop.

Each frame is validated before it counts. Power, voltage, current and power factor are all read, and a frame is rejected when any of them does not parse or when power does not agree with voltage × current × power factor within `OCR_CROSSCHECK_TOLERANCE_PCT` (default 3%). A misread digit or a dropped decimal point breaks that relation by far more, so misreads are rejected instead of recorded. Below 0.02 A the cross-check is skipped, because the meter shows 0.000 A there. A reading is the median of the frames accepted within `OCR_WINDOW_SECONDS` (default 1.5 s), which smooths the flicker of the last digit. When nothing has been accepted for `OCR_STALE_AFTER_SECONDS` (default 5 s), the sample fails and is retried like any other failed reading, so a covered or moved display cannot silently freeze the run on the last value. A camera or stream that stops delivering frames is reported in the same way, with the reason, and is reopened automatically.

### Live preview

While measuring, the tool serves a page showing what the camera sees and what is being read: the levelled frame with the located regions drawn on it (green when the frame was accepted, red when it was rejected and why), the parsed values, the frame rate and the counters. Picture and values are always from the same frame, and the page says so when it loses the connection to the tool or no frame has been processed for a while. The address is printed when the meter starts, `http://127.0.0.1:8765/` by default.

```env
OCR_PREVIEW_HOST=127.0.0.1
# Set to 0 to disable the preview
OCR_PREVIEW_PORT=8765
```

Set `OCR_PREVIEW_HOST=0.0.0.0` to open the preview from another machine, for example when the tool runs on a headless box next to the meter.

### Recommended setup: OCR with a witness

Pair the camera with a network meter on the same circuit as a [witness](#witness-meters), so that every sample is confirmed by an independent measurement:

```env
POWER_METER=ocr
OCR_SOURCE=http://camera.local:8080/
WITNESS_METERS=shelly
SHELLY_IP=x.x.x.x
# The witness sits upstream of the display meter and also measures the meter's own consumption
WITNESS_SHELLY_OFFSET_W=2.45
```

Measure the offset once with nothing plugged into the display meter: it is what the witness reads then.

### Supported displays

The layout of the display is described by `OCR_LAYOUT`. The only layout so far is `pr10`, the Zhurui PR10 power recorder, whose real-time page shows power, voltage, current and power factor together. Another meter can be added with a `DisplayLayout` in `measure/powermeter/ocr/layout.py` describing how its rows read; the camera, detection, validation and preview code are shared.

## Predefining wizard answers

The tool normally asks questions in an interactive wizard. You can skip questions by defining the matching uppercase key in `.env`.

Common examples:

```env
SELECTED_MEASURE_TYPE=Light bulb(s)
MODE=color_temp
GENERATE_MODEL_JSON=true
GZIP=true
ENTITY_ID=light.example
MEASURE_DEVICE=Shelly Plug S
MODEL_ID=LED1837R5
MODEL_NAME=Example Light E27
RESUME=true
```

This is useful when you need to rerun one color mode or resume after an interrupted session.

## Timing and sampling

The default timings work for many devices, but you can tune them when the meter updates slowly or the device needs more time to settle.

```env
SLEEP_TIME=3
SLEEP_TIME_SAMPLE=3
SAMPLE_COUNT=2
SLEEP_INITIAL=10
SLEEP_STANDBY=20
```

Use a higher `SAMPLE_COUNT` to reduce noise. Increase `SLEEP_TIME` or `SLEEP_TIME_SAMPLE` when readings are stale or still settling after each device state change.

For lights, extra wait times exist for large transitions:

```env
SLEEP_TIME_HUE=2
SLEEP_TIME_SAT=2
SLEEP_TIME_CT=1
SLEEP_TIME_EFFECT_CHANGE=5
```

## Resume behavior

The tool can resume many interrupted light sessions when `RESUME=true` and the partial CSV still exists. Docker users should keep the `export` and `.persistent` mounts in place between runs.
