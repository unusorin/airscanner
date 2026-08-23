#!/usr/bin/env python3
"""airscanner - BladeRF 2.0 frequency scanner.

Sweeps a frequency range, detects channels with intermittent activity
("chatter") relative to the noise floor, and shows them in a live table.
The selected channel can be monitored on demand (Enter / 'f') through a second
receiver (an RTL-SDR driven by rtl_fm), streamed over HTTP as WAV.

Example (civil airband):
    airscanner.py 119M 134M 15M
"""

import argparse
import bisect
import csv
import fcntl
import glob
import math
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Input, Static

from bladerf import _bladerf as brf

MIN_FREQ = 47_000_000
MAX_FREQ = 6_000_000_000
MIN_SAMPLE_RATE = 520_834
MAX_SAMPLE_RATE = 61_440_000
MIN_ANALOG_BW = 200_000
MAX_ANALOG_BW = 56_000_000
CHUNK = 65_536  # samples per sync_rx call; keeps Ctrl+C responsive
DWELL_SECONDS = 0.1
STREAM_BUFFERS = 16
STREAM_BUFFER_SIZE = 16_384  # must be a multiple of 1024
NAMES_MAX_DIST = 100_000  # Hz; farthest listed frequency still shown as a channel's name
LISTEN_PORT = 8081
LISTEN_RATE = 12_000  # Hz; mono s16 output rate of rtl_fm and the WAV stream
LISTEN_MODES = ("am", "fm", "wbfm", "usb", "lsb")
RTL_FM = "rtl_fm"
STALL_SECONDS = 3.0  # no rtl_fm output for this long means the dongle wedged
RTL_USB_IDS = {("0bda", "2838"), ("0bda", "2832")}
USBDEVFS_RESET = 0x5514  # _IO('U', 20)


def parse_freq(text):
    s = text.strip()
    mult = 1.0
    if s and s[-1].lower() in "kmg":
        mult = {"k": 1e3, "m": 1e6, "g": 1e9}[s[-1].lower()]
        s = s[:-1]
    try:
        value = float(s) * mult
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid frequency: {text!r}")
    return int(round(value))


@dataclass
class Config:
    start: int
    end: int
    bandwidth: int
    spacing: int
    threshold: float
    activity: float
    window: float
    gain: int | None
    names: str | None
    rx: int  # 0-based RX channel index (port RX1 -> 0, RX2 -> 1)
    max_width: int | None  # Hz; signals wider than this are ignored
    bias_tee: bool  # power an antenna-side LNA from the RX port
    listen_mode: str  # rtl_fm demodulation for the on-demand listener


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Scan a frequency range with a BladeRF and list channels with chatter.",
        epilog="Frequencies accept k/M/G suffixes or scientific notation, e.g. 119M, 121.5M, 119e6.",
    )
    ap.add_argument("start", type=parse_freq, help="start frequency (e.g. 119M)")
    ap.add_argument("end", type=parse_freq, help="end frequency (e.g. 134M)")
    ap.add_argument("bandwidth", type=parse_freq, nargs="?", default=None,
                    help="capture span per tune/hop (e.g. 15M); max 56M; "
                         "default: fit the whole range, capped at 56M")
    ap.add_argument("--spacing", type=parse_freq, default=25_000,
                    help="channel grid spacing in Hz (default 25000; use 8333 for 8.33 kHz)")
    ap.add_argument("--threshold", type=float, default=10.0,
                    help="dB over noise floor to count a channel as active (default 10)")
    ap.add_argument("--activity", type=float, default=5.0,
                    help="minimum activity %% within the window to list a channel (default 5)")
    ap.add_argument("--window", type=float, default=30.0,
                    help="sliding window in seconds for activity stats (default 30)")
    ap.add_argument("--gain", type=int, default=None,
                    help="manual RX gain in dB (-15..60); default is slow-attack AGC")
    ap.add_argument("--rx", type=int, choices=(1, 2), default=1,
                    help="RX port to use: 1 (RX1) or 2 (RX2); default 1")
    ap.add_argument("--bias-tee", action="store_true",
                    help="enable the RX bias tee (DC power for an antenna LNA); default off")
    ap.add_argument("--max-width", type=parse_freq, default=None, metavar="HZ",
                    help="ignore signals wider than this (e.g. 100k); drops FM-shaped "
                         "intermod products near broadcast towers; default: no limit")
    ap.add_argument("--names", metavar="CSV", default=None,
                    help="CSV file of frequency_mhz,name to label detected channels "
                         "with the nearest listed frequency (within 100 kHz) and its offset")
    ap.add_argument("--listen-mode", choices=LISTEN_MODES, default="am",
                    help="rtl_fm demodulation used by the on-demand listener, "
                         "Enter/f keys (default am)")
    args = ap.parse_args(argv)

    if not MIN_FREQ <= args.start < args.end <= MAX_FREQ:
        ap.error(f"need {MIN_FREQ/1e6:.0f}M <= start < end <= {MAX_FREQ/1e9:.0f}G")
    if args.bandwidth is None:
        args.bandwidth = min(args.end - args.start, MAX_ANALOG_BW)
    if not 0 < args.bandwidth <= MAX_ANALOG_BW:
        ap.error("bandwidth must be in (0, 56M]")
    if not 0 < args.spacing <= args.bandwidth:
        ap.error("spacing must be in (0, bandwidth]")
    if args.window <= 0 or not 0 <= args.activity <= 100:
        ap.error("window must be > 0 and activity in [0, 100]")
    if args.gain is not None and not -15 <= args.gain <= 60:
        ap.error("gain must be within -15..60 dB")
    if args.max_width is not None and args.max_width < args.spacing:
        ap.error("max-width must be >= spacing")
    return Config(args.start, args.end, args.bandwidth, args.spacing,
                  args.threshold, args.activity, args.window, args.gain,
                  args.names, args.rx - 1, args.max_width, args.bias_tee,
                  args.listen_mode)


def load_names(path):
    """Load a frequency_mhz,name CSV as a sorted list of (freq_hz, name).

    Frequencies are kept exact; see nearest_name() for how a detected channel
    picks its label.
    """
    by_freq = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 2:
                continue
            try:
                freq = int(round(float(row[0]) * 1e6))
            except ValueError:
                continue  # header or malformed line
            name = row[1].strip()
            if name:
                by_freq[freq] = f"{by_freq[freq]}; {name}" if freq in by_freq else name
    return sorted(by_freq.items())


def nearest_name(names, freq_hz, max_dist=NAMES_MAX_DIST):
    """Nearest listed entry to freq_hz as (name, listed_hz, offset_hz), or None.

    offset_hz = listed - channel; entries farther than max_dist are ignored.
    """
    if not names:
        return None
    i = bisect.bisect_left(names, (freq_hz,))
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(names):
            listed, name = names[j]
            if best is None or abs(listed - freq_hz) < abs(best[1] - freq_hz):
                best = (name, listed, listed - freq_hz)
    return best if abs(best[2]) <= max_dist else None


def format_name(match, decimals):
    if match is None:
        return ""
    name, listed, offset = match
    if offset == 0:
        return name
    return f"{name} ({listed / 1e6:.{decimals}f}, {offset / 1e3:+g} kHz)"


@dataclass
class Hop:
    center: int
    keep: np.ndarray        # bool mask over fftshifted bins
    boundaries: np.ndarray  # reduceat segment starts into psd[keep]
    chan_idx: np.ndarray    # channel index per reduceat segment


class SweepPlan:
    """Hop centers for both dither phases, with per-hop bin->channel mapping.

    Phase 1 offsets every center by +2*spacing so the DC-blanked channels
    differ between alternate sweeps and every channel still gets measured.
    """

    def __init__(self, cfg, fs, nfft):
        self.cfg = cfg
        self.fs = fs
        self.nfft = nfft
        span = cfg.end - cfg.start
        b = cfg.bandwidth
        if span <= b:
            centers = [(cfg.start + cfg.end) // 2]
        else:
            n_hops = math.ceil(span / b)
            centers = [cfg.start + b // 2 + i * b for i in range(n_hops)]
            centers[-1] = cfg.end - b // 2
        self.phases = [
            [self._build_hop(c + phase * 2 * cfg.spacing) for c in centers]
            for phase in (0, 1)
        ]

    def _build_hop(self, center):
        cfg, fs, nfft = self.cfg, self.fs, self.nfft
        freqs = center + (np.arange(nfft) - nfft // 2) * (fs / nfft)
        usable = np.abs(freqs - center) <= cfg.bandwidth / 2
        dc_halfwidth = max(3 * fs / nfft, cfg.spacing / 2)
        not_dc = np.abs(freqs - center) > dc_halfwidth
        chan = np.round(freqs / cfg.spacing).astype(np.int64)
        in_range = (chan * cfg.spacing >= cfg.start) & (chan * cfg.spacing <= cfg.end)
        keep = usable & not_dc & in_range
        kept_chan = chan[keep]
        if kept_chan.size == 0:
            raise SystemExit("no usable bins per hop; spacing/bandwidth combination is invalid")
        boundaries = np.concatenate(([0], np.flatnonzero(np.diff(kept_chan)) + 1))
        return Hop(center, keep, boundaries, kept_chan[boundaries])


class Radio:
    def __init__(self, gain=None, rx_channel=0, bias_tee=False):
        self.dev = brf.BladeRF()
        self.rx_channel = rx_channel
        self.rx = self.dev.Channel(brf.CHANNEL_RX(rx_channel))
        self.gain = gain
        self.bias_tee = bias_tee
        self.fs = None
        # libbladeRF's RX_X1 layout only streams RX1; using RX2 requires the
        # MIMO RX_X2 layout, which interleaves RX1/RX2 samples. Stream both and
        # keep only the wanted channel in that case.
        self.n_stream = 2 if rx_channel != 0 else 1
        self._buf = bytearray(CHUNK * 4 * self.n_stream)

    def info(self):
        return (self.dev.get_board_name(), self.dev.get_serial(),
                self.dev.get_fpga_version(), self.dev.device_speed)

    def configure(self, sample_rate, analog_bw):
        self.rx.sample_rate = int(sample_rate)
        self.fs = float(self.rx.sample_rate)  # actual rate; used in all bin math
        self.rx.bandwidth = int(analog_bw)
        if self.gain is None:
            self.rx.gain_mode = brf.GainMode.SlowAttack_AGC
        else:
            self.rx.gain_mode = brf.GainMode.Manual
            self.rx.gain = int(self.gain)
        # set explicitly either way so a previous session's state doesn't linger
        self.rx.bias_tee = bool(self.bias_tee)
        layout = (brf.ChannelLayout.RX_X2 if self.n_stream == 2
                  else brf.ChannelLayout.RX_X1)
        self.dev.sync_config(layout=layout,
                             fmt=brf.Format.SC16_Q11,
                             num_buffers=STREAM_BUFFERS,
                             buffer_size=STREAM_BUFFER_SIZE,
                             num_transfers=8,
                             stream_timeout=3500)
        if self.n_stream == 2:
            # MIMO streaming needs both RX channels enabled.
            self.dev.Channel(brf.CHANNEL_RX(0)).enable = True
        self.rx.enable = True
        # After a retune the USB pipeline still holds old-frequency samples;
        # also allow settle time (longer for slow-attack AGC re-convergence).
        settle = 0.01 if self.gain is not None else 0.03
        self._flush_samples = STREAM_BUFFERS * STREAM_BUFFER_SIZE + int(settle * self.fs)

    def tune(self, freq):
        self.rx.frequency = int(freq)
        self._flush()

    def _flush(self):
        remaining = self._flush_samples
        while remaining > 0:
            n = min(CHUNK, remaining)
            self.dev.sync_rx(self._buf, n * self.n_stream, timeout_ms=3500)
            remaining -= n

    def capture(self, n_samps):
        out = np.empty(n_samps, np.complex64)
        got = 0
        while got < n_samps:
            n = min(CHUNK, n_samps - got)
            self.dev.sync_rx(self._buf, n * self.n_stream, timeout_ms=3500)
            raw = np.frombuffer(self._buf, dtype=np.int16,
                                count=2 * n * self.n_stream)
            iq = raw.astype(np.float32).view(np.complex64)
            if self.n_stream > 1:
                iq = iq.reshape(n, self.n_stream)[:, self.rx_channel]
            out[got:got + n] = iq
            got += n
        out *= 1.0 / 2048.0  # SC16 Q11 full scale -> +/-1.0
        return out

    def current_gain(self):
        """Overall RX gain in dB as currently applied (tracks AGC); None if unreadable."""
        try:
            return int(self.rx.gain)
        except brf.BladeRFError:
            return None

    def close(self):
        if self.bias_tee:
            try:
                self.rx.bias_tee = False
            except brf.BladeRFError:
                pass
        for ch in range(self.n_stream):
            try:
                self.dev.Channel(brf.CHANNEL_RX(ch)).enable = False
            except brf.BladeRFError:
                pass
        self.dev.close()


def wav_header(rate):
    """Header for an endless mono s16 WAV stream; sizes set to the maximum."""
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", 0xFFFFFFFF))


def lan_ip():
    """Best-effort LAN IP for display purposes; no packets are sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return socket.gethostname()
    finally:
        s.close()


def reset_rtl_usb():
    """USB-reset the first RTL2832 dongle (like a replug); error message or None.

    Needs write access to /dev/bus/usb/BBB/DDD, which the rtl-sdr udev rule
    grants to the plugdev group.
    """
    for vid_path in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        d = os.path.dirname(vid_path)
        try:
            with open(vid_path) as f:
                vid = f.read().strip()
            with open(os.path.join(d, "idProduct")) as f:
                pid = f.read().strip()
            if (vid, pid) not in RTL_USB_IDS:
                continue
            with open(os.path.join(d, "busnum")) as f:
                bus = int(f.read())
            with open(os.path.join(d, "devnum")) as f:
                num = int(f.read())
        except (OSError, ValueError):
            continue
        node = f"/dev/bus/usb/{bus:03d}/{num:03d}"
        try:
            fd = os.open(node, os.O_WRONLY)
            try:
                fcntl.ioctl(fd, USBDEVFS_RESET, 0)
            finally:
                os.close(fd)
        except OSError as e:
            return f"{node}: {e.strerror or e}"
        return None
    return "no RTL2832 device found on USB"


class _AudioHandler(BaseHTTPRequestHandler):
    """Streams the listener's audio as an endless WAV.

    Clients receive silence while no channel is tuned, so a player stays
    connected across stop/retune gaps.
    """

    def do_GET(self):
        if self.path not in ("/", "/audio.wav"):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        listener = self.server.listener
        q = queue.Queue(maxsize=32)
        listener.add_client(q)
        idle = 0.35  # seconds of silence per fill while rtl_fm is not running
        silence = bytes(2 * int(LISTEN_RATE * idle))
        try:
            self.wfile.write(wav_header(LISTEN_RATE))
            while not listener.closing:
                try:
                    data = q.get(timeout=idle)
                except queue.Empty:
                    data = silence
                self.wfile.write(data)
        except OSError:
            pass  # client disconnected
        finally:
            listener.remove_client(q)

    def log_message(self, format, *args):
        pass  # keep the terminal clean for the TUI


class _AudioServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    listener = None


class Listener:
    """On-demand audio monitor on a second receiver (RTL-SDR via rtl_fm).

    start() tunes an rtl_fm subprocess to one frequency; its s16 mono output
    is fanned out to every connected HTTP client. Dongle detection is
    implicit: rtl_fm exits immediately when none is plugged in, which the
    pump thread reports through on_notify. Synchronous failures (spawn, port
    bind) are returned as error strings instead.

    A watchdog covers the other failure mode seen on the Pi: after a USB
    hiccup the dongle wedges and rtl_fm stays alive but never receives
    samples (zero CPU, silent stream). With squelch off rtl_fm emits audio
    continuously, so no output for STALL_SECONDS means stalled; the watchdog
    then kills it, USB-resets the dongle and restarts once.
    """

    def __init__(self, mode, on_notify):
        self.mode = mode
        self.on_notify = on_notify  # (message, severity) from a worker thread
        self.freq = None
        self.proc = None
        self.server = None
        self.closing = False
        self.clients = set()
        self.lock = threading.Lock()
        self.generation = 0  # bumped on stop/start; orphans older pump/watchdog threads
        self.last_data = 0.0  # monotonic time of the last rtl_fm output
        self.recoveries = 0   # watchdog restarts since the last user-initiated start

    def ensure_server(self):
        """Start the HTTP server once; returns an error message or None."""
        if self.server is not None:
            return None
        try:
            server = _AudioServer(("", LISTEN_PORT), _AudioHandler)
        except OSError as e:
            return f"cannot serve audio on port {LISTEN_PORT}: {e}"
        server.listener = self
        self.server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return None

    def start(self, freq):
        """(Re)tune rtl_fm to freq; returns an error message or None."""
        with self.lock:
            self.recoveries = 0
        return self._spawn(freq)

    def _spawn(self, freq):
        self.stop()
        rate = (["-s", "171k", "-r", str(LISTEN_RATE)] if self.mode == "wbfm"
                else ["-s", str(LISTEN_RATE)])
        argv = [RTL_FM, "-f", str(int(freq)), "-M", self.mode, *rate, "-"]
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        except OSError as e:
            return f"cannot run {RTL_FM}: {e}"
        with self.lock:
            self.proc = proc
            self.freq = freq
            self.generation += 1
            gen = self.generation
            self.last_data = time.monotonic()
        threading.Thread(target=self._pump, args=(proc, gen), daemon=True).start()
        threading.Thread(target=self._watchdog, args=(gen,), daemon=True).start()
        return None

    def stop(self):
        with self.lock:
            self.generation += 1
            proc, self.proc, self.freq = self.proc, None, None
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def close(self):
        self.closing = True
        self.stop()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    def add_client(self, q):
        with self.lock:
            self.clients.add(q)

    def remove_client(self, q):
        with self.lock:
            self.clients.discard(q)

    def _pump(self, proc, gen):
        while True:
            data = proc.stdout.read(4096)
            if not data:
                break
            with self.lock:
                if gen != self.generation:
                    return  # stopped or retuned; a terminate is on its way
                self.last_data = time.monotonic()
                clients = list(self.clients)
            for q in clients:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    # drop the client's oldest chunk rather than stall everyone
                    try:
                        q.get_nowait()
                        q.put_nowait(data)
                    except (queue.Empty, queue.Full):
                        pass
        # EOF: either a deliberate stop() or rtl_fm died (no dongle, USB error)
        err = proc.stderr.read()
        proc.wait()
        with self.lock:
            if gen != self.generation:
                return
            self.generation += 1  # also retires this generation's watchdog
            self.proc = None
            self.freq = None
        text = err.decode(errors="replace")
        if "no supported devices" in text.lower():
            self.on_notify("RTL-SDR not detected", "error")
        else:
            lines = [ln for ln in text.strip().splitlines() if ln.strip()]
            tail = lines[-1] if lines else f"exit code {proc.returncode}"
            self.on_notify(f"{RTL_FM} stopped: {tail}", "error")

    def _watchdog(self, gen):
        while True:
            time.sleep(0.5)
            with self.lock:
                if gen != self.generation:
                    return  # stopped, retuned or exited; nothing to watch
                if time.monotonic() - self.last_data < STALL_SECONDS:
                    continue
                freq, recoveries = self.freq, self.recoveries
            # stalled: rtl_fm is alive but the dongle stopped delivering samples
            self.stop()
            if recoveries >= 1:
                self.on_notify("RTL-SDR stalled again after a USB reset; stopped", "error")
                return
            err = reset_rtl_usb()
            if err:
                self.on_notify(f"RTL-SDR stalled and USB reset failed: {err}", "error")
                return
            time.sleep(1.0)  # let the dongle re-enumerate
            with self.lock:
                self.recoveries = recoveries + 1
            err = self._spawn(freq)
            if err:
                self.on_notify(err, "error")
            else:
                self.on_notify("RTL-SDR stalled: dongle reset, rtl_fm restarted", "warning")
            return


def process_hop(iq, hop, nfft, window, threshold):
    k = iq.size // nfft
    segs = iq[:k * nfft].reshape(k, nfft) * window
    spec = np.fft.fft(segs, axis=1)
    psd = np.fft.fftshift((spec.real ** 2 + spec.imag ** 2).mean(axis=0))
    kept = psd[hop.keep]
    floor = max(float(np.median(kept)), 1e-30)
    chan_pwr = np.maximum.reduceat(kept, hop.boundaries)
    db_over = 10.0 * np.log10(np.maximum(chan_pwr, 1e-30) / floor)
    hits = db_over >= threshold
    return hop.chan_idx, db_over, hits, find_runs(hop.chan_idx, db_over, hits)


def find_runs(chan_idx, db_over, hits):
    """Group consecutive above-threshold channels into signals.

    Returns [(lo_idx, hi_idx, peak_idx, peak_db)] per run. A signal wider than
    the channel spacing lights several adjacent channels in one measurement;
    treating the run as one signal avoids listing it N times. Runs cannot
    extend past the hop's kept bins (hop edge, DC notch), so a signal
    straddling those may be split in two.
    """
    runs = []
    hit_pos = np.flatnonzero(hits)
    if hit_pos.size == 0:
        return runs
    # break where positions are not consecutive or channel indices have a gap
    breaks = np.flatnonzero((np.diff(hit_pos) != 1)
                            | (np.diff(chan_idx[hit_pos]) != 1)) + 1
    for seg in np.split(hit_pos, breaks):
        peak = seg[int(np.argmax(db_over[seg]))]
        runs.append((int(chan_idx[seg[0]]), int(chan_idx[seg[-1]]),
                     int(chan_idx[peak]), float(db_over[peak])))
    return runs


class ChannelStats:
    """Sliding-window activity per channel; qualified channels persist all session.

    Each run of adjacent hit channels counts as one signal: only one
    representative channel per run records a hit, the rest record misses.
    """

    def __init__(self, spacing, window_s, max_width=None):
        self.spacing = spacing
        self.window_s = window_s
        self.max_width = max_width
        self.chans = {}      # chan_idx -> deque[(t, hit, db, width_hz)]
        self.airtime = {}    # chan_idx -> cumulative seconds on air (whole session)
        self.last_seen = {}  # chan_idx -> t of the previous measurement
        # chan_idx -> (last_hit_t, last_hit_db, width_hz, mean_db, peak_db); never evicted
        self.qualified = {}

    def _representative(self, lo, hi, peak):
        """Keep an already-listed channel inside the run so rows don't jitter."""
        best = None
        for idx in self.qualified:
            if lo <= idx <= hi and (best is None or abs(idx - peak) < abs(best - peak)):
                best = idx
        return peak if best is None else best

    def update(self, idxs, dbs, hits, runs, now):
        rep = {}  # chan_idx -> (db, width) for the one hit channel per run
        for lo, hi, peak, peak_db in runs:
            width = (hi - lo + 1) * self.spacing
            if self.max_width is not None and width > self.max_width:
                continue  # too wide: treated as a miss on all its channels
            rep[self._representative(lo, hi, peak)] = (peak_db, width)
        for idx, db in zip(idxs.tolist(), dbs.tolist()):
            d = self.chans.get(idx)
            if d is None:
                d = self.chans[idx] = deque()
            r = rep.get(idx)
            if r is None:
                d.append((now, False, db, 0))
            else:
                d.append((now, True, r[0], r[1]))
                # a hit is worth the time since this channel was last measured
                # (about one sweep); first-ever hit gets one dwell; capped so a
                # pause gap cannot be credited as air time
                gap = now - self.last_seen.get(idx, now - DWELL_SECONDS)
                self.airtime[idx] = self.airtime.get(idx, 0.0) + min(gap, self.window_s)
                q = self.qualified.get(idx)
                if q is not None:
                    self.qualified[idx] = (now, r[0], r[1], q[3], q[4])
            self.last_seen[idx] = now

    def rows(self, now, min_activity):
        cutoff = now - self.window_s
        dead = []
        for idx, d in self.chans.items():
            while d and d[0][0] < cutoff:
                d.popleft()
            if not d:
                if idx not in self.qualified:
                    dead.append(idx)
                continue
            hit_entries = [e for e in d if e[1]]
            if not hit_entries:
                continue
            hit_dbs = [e[2] for e in hit_entries]
            mean_db, peak_db = sum(hit_dbs) / len(hit_dbs), max(hit_dbs)
            q = self.qualified.get(idx)
            if q is not None:
                # refresh window stats; frozen at these values once stale
                self.qualified[idx] = (q[0], q[1], q[2], mean_db, peak_db)
            elif 100.0 * len(hit_entries) / len(d) >= min_activity:
                last_t, _, last_db, width = hit_entries[-1]
                self.qualified[idx] = (last_t, last_db, width, mean_db, peak_db)
        for idx in dead:
            del self.chans[idx]
            self.airtime.pop(idx, None)
            self.last_seen.pop(idx, None)

        out = []
        for idx, (last_t, last_db, width, mean_db, peak_db) in self.qualified.items():
            d = self.chans.get(idx)
            activity = (100.0 * sum(1 for e in d if e[1]) / len(d)) if d else 0.0
            stale = now - last_t > self.window_s
            out.append((idx * self.spacing, last_db, activity, last_t, stale, width,
                        mean_db, peak_db, self.airtime.get(idx, 0.0)))
        return sorted(out)


def fmt_duration(seconds):
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


class Cell(Text):
    """Table cell with a numeric sort value alongside its rendered text."""

    def __init__(self, value, text, stale=False):
        super().__init__(text, style="dim" if stale else "")
        self.value = value


class ScannerApp(App):
    TITLE = "airscanner"
    CSS = """
    #info, #status { padding: 0 1; color: $text-muted; }
    #channels { height: 1fr; }
    #freq { display: none; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("space", "toggle_pause", "Pause/resume"),
        Binding("c", "clear", "Clear list"),
        Binding("e", "export", "Export CSV"),
        Binding("enter", "listen_selected", "Listen"),
        Binding("f", "listen_prompt", "Frequency"),
        Binding("1", "sort('freq')", "Sort freq"),
        Binding("2", "sort('db')", "Sort dB"),
        Binding("3", "sort('activity')", "Sort activity"),
        Binding("4", "sort('last')", "Sort last heard"),
        Binding("5", "sort('width')", "Sort width"),
        Binding("6", "sort('mean')", "Sort mean"),
        Binding("7", "sort('peak')", "Sort peak"),
        Binding("8", "sort('airtime')", "Sort air time"),
        Binding("plus", "threshold(1)", "Threshold +", key_display="+"),
        Binding("minus", "threshold(-1)", "Threshold -", key_display="-"),
        Binding("right_square_bracket", "activity(1)", "Activity +", key_display="]"),
        Binding("left_square_bracket", "activity(-1)", "Activity -", key_display="["),
    ]

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.names = None
        self.threshold = cfg.threshold
        self.min_activity = cfg.activity
        self.paused = threading.Event()
        self.stop = threading.Event()
        self.done = threading.Event()  # set once the worker released the radio
        self.reset_requested = False
        self.listener = Listener(cfg.listen_mode, self._listen_notify_from_thread)
        self.listen_freq = None
        self._listen_url = None
        self.sort_column = "freq"
        self.sort_reverse = False
        self._last_rows = []
        self._last_now = 0.0
        self.sub_title = f"{cfg.start / 1e6:.3f}-{cfg.end / 1e6:.3f} MHz"

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("opening BladeRF...", id="info")
        yield DataTable(id="channels", cursor_type="row", zebra_stripes=True)
        yield Input(placeholder="frequency to listen to, e.g. 121.5M (Esc cancels)", id="freq")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self):
        table = self.query_one(DataTable)
        table.add_column("Frequency (MHz)", key="freq")
        table.add_column("dB", key="db")
        table.add_column("Mean", key="mean")
        table.add_column("Peak", key="peak")
        table.add_column("Width", key="width")
        table.add_column("Activity", key="activity")
        table.add_column("Air time", key="airtime")
        table.add_column("Last heard", key="last")
        if self.cfg.names is not None:
            table.add_column("Name", key="name")
        self.scan()

    def on_unmount(self):
        self.stop.set()
        self.listener.close()

    # --- actions -------------------------------------------------------

    def action_toggle_pause(self):
        if self.paused.is_set():
            self.paused.clear()
        else:
            self.paused.set()
        self._refresh_status()

    def action_clear(self):
        self.reset_requested = True
        self._last_rows = []
        self.query_one(DataTable).clear()

    def action_export(self):
        """Write the listed channels to a CSV; first two columns match --names."""
        rows, now, cfg = self._last_rows, self._last_now, self.cfg
        if not rows:
            self.notify("no channels to export", severity="warning")
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = f"airscanner_{cfg.start / 1e6:.3f}-{cfg.end / 1e6:.3f}MHz_{stamp}.csv"
        decimals = 3 if cfg.spacing % 1000 == 0 else 5
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["frequency_mhz", "name", "db_over_floor", "mean_db", "peak_db",
                            "width_khz", "activity_pct", "airtime_s", "last_heard_s_ago",
                            "stale", "listed_mhz", "offset_khz"])
                for freq, db, activity, last_t, stale, width, mean_db, peak_db, airtime in rows:
                    m = nearest_name(self.names, freq) if self.names else None
                    w.writerow([f"{freq / 1e6:.{decimals}f}", m[0] if m else "", f"{db:.1f}",
                                f"{mean_db:.1f}", f"{peak_db:.1f}",
                                f"{width / 1e3:g}", f"{activity:.0f}", f"{airtime:.0f}",
                                f"{now - last_t:.0f}", int(stale),
                                f"{m[1] / 1e6:.{decimals}f}" if m else "",
                                f"{m[2] / 1e3:g}" if m else ""])
        except OSError as e:
            self.notify(f"export failed: {e}", severity="error")
            return
        self.notify(f"exported {len(rows)} rows to {path}")

    def action_listen_selected(self):
        """Enter on the table: listen to the selected row (again to stop)."""
        table = self.query_one(DataTable)
        if not table.row_count:
            self.notify("no channel to listen to", severity="warning")
            return
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        self._listen_to(int(key.value) * self.cfg.spacing)

    def on_data_table_row_selected(self, event):
        self._listen_to(int(event.row_key.value) * self.cfg.spacing)

    def action_listen_prompt(self):
        """Open the manual frequency prompt."""
        box = self.query_one("#freq", Input)
        box.value = ""
        box.display = True
        box.focus()

    def _hide_prompt(self):
        box = self.query_one("#freq", Input)
        box.display = False
        self.query_one(DataTable).focus()

    def on_input_submitted(self, event):
        self._hide_prompt()
        try:
            freq = parse_freq(event.value)
        except argparse.ArgumentTypeError as e:
            self.notify(str(e), severity="error")
            return
        self._listen_to(freq)

    def on_key(self, event):
        if event.key == "escape" and self.query_one("#freq", Input).display:
            self._hide_prompt()

    def _listen_to(self, freq):
        """Tune the listener to freq; the same frequency again stops it."""
        if self.listen_freq == freq:
            self.listener.stop()
            self.listen_freq = None
        else:
            err = self.listener.ensure_server() or self.listener.start(freq)
            if err:
                self.listen_freq = None
                self.notify(err, severity="error")
            else:
                self.listen_freq = freq
                if self._listen_url is None:
                    self._listen_url = f"http://{lan_ip()}:{LISTEN_PORT}/audio.wav"
        self._refresh_status()

    def _listen_notify(self, message, severity):
        if severity == "error":
            self.listen_freq = None  # the listener has stopped itself
        self.notify(message, severity=severity, timeout=8)
        self._refresh_status()

    def _listen_notify_from_thread(self, message, severity):
        if self.stop.is_set():
            return
        try:
            self.call_from_thread(self._listen_notify, message, severity)
        except RuntimeError:
            pass  # app already shut down

    def action_sort(self, column):
        if column == self.sort_column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column, self.sort_reverse = column, False
        self._apply_sort()
        self._refresh_status()

    def action_threshold(self, delta):
        self.threshold += delta
        self._refresh_status()

    def action_activity(self, delta):
        self.min_activity = min(100.0, max(0.0, self.min_activity + delta))
        self._refresh_status()

    # --- UI updates (main thread) ---------------------------------------

    def set_info(self, text):
        self.query_one("#info", Static).update(text)

    def _apply_sort(self):
        """Re-sort, keeping the cursor on the same frequency rather than row index."""
        table = self.query_one(DataTable)
        key = None
        if table.row_count:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        table.sort(self.sort_column, key=lambda c: c.value, reverse=self.sort_reverse)
        if key is not None and key in table.rows:
            table.move_cursor(row=table.get_row_index(key), scroll=False)

    def _refresh_status(self, sweep_text=None):
        if sweep_text is not None:
            self._sweep_text = sweep_text
        parts = [getattr(self, "_sweep_text", "starting"),
                 f"threshold {self.threshold:g} dB",
                 f"activity >= {self.min_activity:g}% over {self.cfg.window:g}s",
                 *([f"max width {self.cfg.max_width / 1e3:g} kHz"]
                   if self.cfg.max_width is not None else []),
                 f"sort {self.sort_column}{' desc' if self.sort_reverse else ''}"]
        if self.listen_freq is not None:
            decimals = 3 if self.listen_freq % 1000 == 0 else 5
            parts.append(f"listening {self.listen_freq / 1e6:.{decimals}f}"
                         f" -> {self._listen_url}")
        if self.paused.is_set():
            parts.append("PAUSED")
        self.query_one("#status", Static).update(" | ".join(parts))

    def update_rows(self, rows, now, sweep_text):
        self._last_rows, self._last_now = rows, now
        table = self.query_one(DataTable)
        cfg = self.cfg
        decimals = 3 if cfg.spacing % 1000 == 0 else 5
        for freq, db, activity, last_t, stale, width, mean_db, peak_db, airtime in rows:
            age = now - last_t
            cells = [Cell(freq, f"{freq / 1e6:.{decimals}f}", stale),
                     Cell(db, f"{db:.1f}", stale),
                     Cell(mean_db, f"{mean_db:.1f}", stale),
                     Cell(peak_db, f"{peak_db:.1f}", stale),
                     Cell(width, f"{width / 1e3:g} kHz", stale),
                     Cell(activity, f"{activity:.0f}%", stale),
                     Cell(airtime, fmt_duration(airtime), stale),
                     Cell(age, f"{age:.0f}s ago", stale)]
            if self.names is not None:
                cells.append(Cell(0, format_name(nearest_name(self.names, freq), decimals),
                                  stale))
            key = str(round(freq / cfg.spacing))
            if key in table.rows:
                for col, cell in zip(table.columns, cells):
                    table.update_cell(key, col, cell)
            else:
                table.add_row(*cells, key=key)
        self._apply_sort()
        self._refresh_status(sweep_text)

    # --- sweep loop (worker thread) -------------------------------------

    @work(thread=True, exclusive=True)
    def scan(self):
        cfg = self.cfg

        def call(fn, *args, **kwargs):
            # After shutdown the event loop is gone and call_from_thread would
            # block forever waiting for its result, so drop UI updates then.
            if self.stop.is_set():
                return
            try:
                self.call_from_thread(fn, *args, **kwargs)
            except RuntimeError:
                pass  # app already shut down

        if cfg.names is not None:
            try:
                self.names = load_names(cfg.names)
            except OSError as e:
                call(self.exit, return_code=1, message=f"cannot read names file: {e}")
                self.done.set()
                return

        try:
            radio = Radio(cfg.gain, cfg.rx, cfg.bias_tee)
        except brf.BladeRFError as e:
            call(self.exit, return_code=1, message=f"failed to open BladeRF: {e}")
            self.done.set()
            return

        try:
            board, serial, fpga, speed = radio.info()
            device_line = f"{board} serial={serial} fpga={fpga} usb={speed}"

            sample_rate = min(max(1.25 * cfg.bandwidth, MIN_SAMPLE_RATE), MAX_SAMPLE_RATE)
            analog_bw = min(max(cfg.bandwidth, MIN_ANALOG_BW), MAX_ANALOG_BW)
            radio.configure(sample_rate, analog_bw)
            fs = radio.fs
            if fs > 20e6 and "super" not in str(speed).lower():
                call(self.notify, f"not on USB 3 SuperSpeed; {fs / 1e6:.1f} Msps may overrun",
                     severity="warning", timeout=15)

            nfft = 1 << max(8, min(16, math.ceil(math.log2(4 * fs / cfg.spacing))))
            n_samps = max(1, round(DWELL_SECONDS * fs / nfft)) * nfft
            fft_window = np.hanning(nfft).astype(np.float32)
            plan = SweepPlan(cfg, fs, nfft)
            stats = ChannelStats(cfg.spacing, cfg.window, cfg.max_width)

            n_hops = len(plan.phases[0])
            names_desc = (f", {len(self.names)} names from {cfg.names}"
                          if self.names is not None else "")

            def info_text(gain_now):
                if cfg.gain is not None:
                    gain_desc = f"manual {cfg.gain} dB"
                elif gain_now is None:
                    gain_desc = "AGC slow-attack"
                else:
                    gain_desc = f"AGC slow-attack, now {gain_now} dB"
                return (f"{device_line}\n"
                        f"{n_hops} hop(s), fs={fs / 1e6:.2f} Msps, nfft={nfft}, "
                        f"spacing={cfg.spacing / 1e3:g} kHz, gain={gain_desc}, "
                        f"rx=RX{cfg.rx + 1}{', bias-tee on' if cfg.bias_tee else ''}"
                        f"{names_desc}")

            shown_gain = None
            call(self.set_info, info_text(None))

            sweep = 0
            sweep_times = deque(maxlen=20)
            current_freq = None
            while not self.stop.is_set():
                if self.paused.is_set():
                    time.sleep(0.1)
                    continue
                if self.reset_requested:
                    self.reset_requested = False
                    stats = ChannelStats(cfg.spacing, cfg.window, cfg.max_width)
                for hop in plan.phases[sweep % 2]:
                    if self.stop.is_set():
                        return
                    if hop.center != current_freq:
                        radio.tune(hop.center)
                        current_freq = hop.center
                    iq = radio.capture(n_samps)
                    now = time.monotonic()
                    idxs, dbs, hits, runs = process_hop(iq, hop, nfft, fft_window,
                                                        self.threshold)
                    stats.update(idxs, dbs, hits, runs, now)
                sweep += 1
                now = time.monotonic()
                sweep_times.append(now)
                rate = ((len(sweep_times) - 1) / (sweep_times[-1] - sweep_times[0])
                        if len(sweep_times) > 1 else 0.0)
                rows = stats.rows(now, self.min_activity)
                call(self.update_rows, rows, now, f"sweep {sweep} | {rate:.1f} sweeps/s")
                if cfg.gain is None:
                    gain_now = radio.current_gain()  # gain at the last hop of the sweep
                    if gain_now != shown_gain:
                        shown_gain = gain_now
                        call(self.set_info, info_text(gain_now))
        except brf.BladeRFError as e:
            call(self.exit, return_code=1, message=f"BladeRF error: {e}")
        finally:
            radio.close()
            self.done.set()


def main(argv=None):
    cfg = parse_args(argv)
    app = ScannerApp(cfg)
    try:
        app.run()
    finally:
        app.done.wait()  # let the worker finish its current capture and close the radio
    return app.return_code or 0


if __name__ == "__main__":
    sys.exit(main())
