# airscanner

Frequency scanner for the BladeRF 2.0 Micro. Sweeps a range, detects channels
that rise above the noise floor, and lists the ones with recurring activity
("chatter") in a live, scrollable terminal UI. Built for scanning the civil
airband (119–134 MHz AM voice), but works on any range the BladeRF can tune.

## Requirements

- BladeRF 2.0 Micro (xA4/xA9) on USB 3
- libbladeRF 2023.02 installed system-wide (Ubuntu: `apt install libbladerf-dev bladerf-fpga-hostedxa4`)
- Python 3.10+, git (the official Nuand Python bindings install from GitHub — they are not on PyPI)
- Optional, for on-demand listening (`Enter` / `f` keys): an RTL-SDR dongle and `rtl_fm` on
  `PATH`. The RTL-SDR Blog V4 needs the [rtl-sdr-blog](https://github.com/rtlsdrblog/rtl-sdr-blog)
  build of the tools — the stock Debian/Ubuntu `rtl-sdr` package does not
  support the V4's front end (symptom: terrible sensitivity, not an error)

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Sanity check the bindings and device:

```sh
.venv/bin/python -c "import bladerf; d = bladerf.BladeRF(); print(d.get_board_name(), d.get_serial(), d.device_speed); d.close()"
```

## Usage

```sh
.venv/bin/python airscanner.py START END [BANDWIDTH] [options]
```

- `START`, `END` — scan range. Frequencies accept `k`/`M`/`G` suffixes or scientific notation (`119M`, `121.5M`, `119e6`).
- `BANDWIDTH` — capture span per tune (max 56M). If the whole range fits in one span, the scanner stays tuned and sweeps fastest; wider ranges are covered by hopping. Defaults to the whole range, capped at 56M.

Options:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--spacing` | `25000` | Channel grid spacing in Hz (`8333` for European 8.33 kHz airband channels) |
| `--threshold` | `10` | dB over the noise floor for a channel to count as active |
| `--activity` | `5` | Minimum activity % within the window for a channel to be listed |
| `--window` | `30` | Sliding window in seconds for the activity statistics |
| `--rx` | `1` | RX port to use: `1` (RX1) or `2` (RX2) |
| `--bias-tee` | off | Enable the RX port bias tee to power an antenna-side LNA. Only with an LNA that expects it — it puts DC on the connector, which a passive antenna or filter may not like |
| `--gain` | AGC | Manual RX gain in dB (−15..60). Default is slow-attack AGC |
| `--max-width` | none | Ignore signals wider than this (e.g. `100k`). Wide (150–250 kHz), ~100 % activity rows are the fingerprint of FM broadcast intermod near a tower; this drops them at detection time |
| `--names` | none | CSV of `frequency_mhz,name` used to label detected channels (see `frequencies.csv`). A channel gets the nearest listed entry within ±100 kHz; when that entry is not exactly on the channel, its frequency and offset are shown too, e.g. the 8.33 kHz channel 124.530 on the 25 kHz grid appears as `124.525  Brașov ATIS (124.530, +5 kHz)` |
| `--listen-mode` | `am` | Demodulation used by the on-demand listener (`Enter` / `f`): `am`, `fm`, `wbfm`, `usb` or `lsb`, passed to `rtl_fm -M` |

Examples:

```sh
# Civil airband, single tune (15 MHz range fits in one capture),
# with station names from the bundled Romanian frequency table
.venv/bin/python airscanner.py 119M 134M --names frequencies.csv

# European channel grid, more sensitive
.venv/bin/python airscanner.py 119M 134M 15M --spacing 8333 --threshold 8 --activity 2

# FM broadcast sanity check (always-on carriers should show ~100% activity)
.venv/bin/python airscanner.py 87.5M 108M 20.5M --spacing 100k
```

## Display

| Column | Meaning |
| --- | --- |
| Frequency (MHz) | Channel center, snapped to the `--spacing` grid (e.g. 119.025); for a wide signal, its strongest channel |
| dB | Signal power above the estimated noise floor at the last time it was heard |
| Mean / Peak | Mean and maximum dB over floor across the hits inside `--window`; frozen at their last values once the channel goes stale |
| Width | Span of adjacent channels the signal occupied when last heard (`25 kHz` = a single channel) |
| Activity | Fraction of measurements within `--window` where the channel was above threshold |
| Air time | Cumulative time on air over the whole session: each hit is credited with the time since that channel was last measured (≈ one sweep), capped at `--window` so pauses don't count. Reset by `c` |
| Last heard | Seconds since the channel was last above threshold |
| Name | Nearest `--names` entry within ±100 kHz, with its listed frequency and offset when off-channel; empty when nothing is listed nearby |

Channels appear once their activity ratio reaches `--activity` and then stay
listed for the rest of the session — the table is "everything found so far".
Activity % still reflects only the recent `--window`, so it falls back to 0%
when a channel goes quiet, and rows with no hit inside the window render
dimmed. Intermittent voice (tower/aircraft) shows low percentages; continuous
broadcasts (ATIS/VOLMET) sit near 100%.

A signal wider than `--spacing` lights several adjacent channels in the same
measurement; those are merged into one signal listed at its strongest channel,
with the run's span in the Width column. Once a channel is listed it keeps
representing that signal even if the peak drifts to a neighbour. A signal
straddling a hop boundary or the blanked LO notch can still appear as two rows.

The header lines show the device and scan parameters; with AGC the gain entry
also shows the gain currently applied (`AGC slow-attack, now 42 dB`, read after
each sweep), which is handy for spotting front-end overload — a very low value
means something strong is in band.

The table scrolls (arrow keys, PageUp/PageDown, Home/End, mouse wheel) and
the status line below it shows sweep rate, current threshold/activity
settings, sort order and whether scanning is paused.

### Keys

| Key | Action |
| --- | --- |
| `q` | Quit |
| `Space` | Pause / resume sweeping |
| `c` | Clear the list (forget all found channels and restart the statistics) |
| `e` | Export the list to `airscanner_<start>-<end>MHz_<YYYYmmdd-HHMMSS>.csv` in the current directory |
| `Enter` | Listen to the selected row on the RTL-SDR; again on the same row to stop (see [Listening](#listening)) |
| `f` | Type a frequency to listen to (e.g. `121.5M`); `Esc` cancels |
| `1` … `8` | Sort by frequency / dB / activity / last heard / width / mean / peak / air time; press again to reverse |
| `+` `-` | Raise / lower the detection threshold by 1 dB |
| `]` `[` | Raise / lower the minimum activity % by 1 |

Exported files start with `frequency_mhz,name` columns, so an export can be
passed straight back as `--names` in a later session (the remaining columns —
dB, mean/peak dB, width, activity, air time, seconds since last heard, stale flag, the
matched entry's listed frequency and offset — are ignored there). For antenna or site comparisons, run equal-length sessions with
`--window` set to the run length so Mean/Peak cover the whole session.

Threshold changes apply to new measurements only — hits already recorded in
the sliding window keep the verdict they got at the time.

## Listening

While the BladeRF keeps sweeping, a second receiver — an RTL-SDR dongle — can
monitor one channel's audio on demand. Move the cursor to a row and press
`Enter` (or click it): the script tunes `rtl_fm` to that row's frequency (demodulation per
`--listen-mode`, AM by default) and serves the audio as a live WAV stream on
**port 8081**, reachable from any device on the network:

```sh
mpv http://<host>:8081/audio.wav      # or VLC, or a browser tab
```

The exact URL appears in the status line while listening. Press `Enter` on
another row to retune (the stream goes silent for about a second while `rtl_fm`
restarts), or on the same row to stop. To listen to a frequency that is not in
the table, press `f`, type it (same syntax as the scan range, e.g. `121.5M` or
`118.850M`) and press `Enter`; entering the currently tuned frequency stops
listening. Players stay connected through retunes
and stops — the server feeds silence whenever nothing is tuned. Scanning is
unaffected either way; the two radios are independent.

There is no startup probe: the dongle is only looked for when listening starts,
so it can be plugged in at any time. If it is missing (or `rtl_fm` is not
installed), a notification says so and nothing else changes. If the dongle
disappears mid-listen, the stream falls back to silence and the error is shown.

A USB hiccup (on a Pi, typically when the BladeRF resets on the shared
controller) can wedge the dongle so that `rtl_fm` stays alive but never
receives samples — the stream goes silent with no error. A watchdog catches
this: with squelch off `rtl_fm` outputs continuously, so 3 s without output
means stalled. The script then kills it, USB-resets the dongle (needs write
access to `/dev/bus/usb/…`, which the rtl-sdr udev rule grants to `plugdev`)
and restarts once, with a notification. If it stalls again right after the
reset, listening stops with an error.

The stream binds to all interfaces. When tunneling instead is preferred (e.g.
scanning a remote box over the internet), an ssh forward works unchanged:
`ssh -L 8081:localhost:8081 user@host`, then open `http://localhost:8081/audio.wav`.

The row's grid frequency is what gets tuned — for 8.33 kHz stations shown at an
offset on the 25 kHz grid, scan with `--spacing 8333` so the row sits on the
station's actual frequency.

## Notes

- **AGC vs manual gain**: AGC "just works" but re-converges after every retune,
  which slows multi-hop sweeps (the scanner discards extra settle samples).
  For multi-hop scans a fixed `--gain 40` (adjust for your antenna) sweeps faster.
- Detection is relative to the per-hop median noise floor, so gain changes
  between hops don't skew it.
- The LO leakage spike at the tuned center is blanked; hop centers are dithered
  by 2 channels on alternate sweeps so no channel stays hidden behind it.
- The plain SC16 sample stream has no overrun flagging — at very high sample
  rates on a loaded USB bus, overruns are silent. For a scanner this only
  costs a bit of dwell time.

## Deploy

`make deploy` rsyncs the project to `admin@192.168.0.37:~/airscanner` (override
with `make deploy DEPLOY_HOST=user@host DEPLOY_DIR=path`). The venv is not
copied; run the setup steps on the target once.
