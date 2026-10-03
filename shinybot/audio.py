"""Play the capture card's audio through the PC speakers (with mute/volume)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

# DirectSound resamples for us (capture cards are 48 kHz, speakers may differ)
# and keeps latency low enough for watching; MME is the fallback.
PREFERRED_HOST_APIS = ('Windows DirectSound', 'Windows MME')
# Words in capture card audio device names, best match first.
CAPTURE_HINTS = ('capture', 'hdmi', 'cam link', 'elgato', 'avermedia', 'game', 'usb video',
                 'digital audio interface', 'line')
SKIP_NAMES = ('primary sound capture', 'microsoft sound mapper')


@dataclass
class AudioDevice:
    index: int
    name: str
    channels: int
    samplerate: float


def _sounddevice():
    try:
        import sounddevice
    except (ImportError, OSError) as error:  # not installed / no PortAudio
        raise RuntimeError(f'audio unavailable ({error}); run: python -m pip install -r '
                           'requirements.txt') from error
    return sounddevice


def _host_api(sd) -> tuple[int, dict]:
    apis = list(enumerate(sd.query_hostapis()))
    for wanted in PREFERRED_HOST_APIS:
        for index, api in apis:
            if api['name'] == wanted:
                return index, api
    index = sd.default.hostapi
    return index, apis[index][1]


def list_inputs() -> list[AudioDevice]:
    """Audio inputs (one host API only, so each device is listed once)."""
    sd = _sounddevice()
    api_index, _api = _host_api(sd)
    devices = []
    for index, info in enumerate(sd.query_devices()):
        if info['hostapi'] != api_index or info['max_input_channels'] < 1:
            continue
        if info['name'].lower().startswith(SKIP_NAMES):
            continue
        devices.append(AudioDevice(index, info['name'], min(2, info['max_input_channels']),
                                   info['default_samplerate']))
    return devices


def guess_capture_device(devices: list[AudioDevice]) -> AudioDevice | None:
    """The input that looks most like a capture card (None if nothing matches)."""
    for hint in CAPTURE_HINTS:
        for device in devices:
            if hint in device.name.lower():
                return device
    return None


def mix(indata: np.ndarray, outdata: np.ndarray, volume: float, muted: bool) -> None:
    """Copy input to output with volume, matching channel counts."""
    if muted or volume <= 0:
        outdata.fill(0)
        return
    if indata.shape[1] == outdata.shape[1]:
        np.multiply(indata, volume, out=outdata)
    else:  # e.g. mono capture to stereo speakers: repeat the first channel
        outdata[:] = indata[:, :1] * volume


class AudioPassthrough:
    """Streams one input device to the default output device."""

    def __init__(self) -> None:
        self.stream = None
        self.device: AudioDevice | None = None
        self.volume = 1.0
        self.muted = False

    def _callback(self, indata, outdata, _frames, _time, status) -> None:
        if status:
            logger.debug('audio status: %s', status)
        mix(indata, outdata, self.volume, self.muted)

    def start(self, device: AudioDevice) -> None:
        self.stop()
        sd = _sounddevice()
        _api_index, api = _host_api(sd)
        output = api['default_output_device']
        if output < 0:
            raise RuntimeError('no speakers/headphones found')
        out_channels = min(2, sd.query_devices(output)['max_output_channels'])
        self.stream = sd.Stream(
            device=(device.index, output), channels=(device.channels, out_channels),
            samplerate=device.samplerate, dtype='float32', latency='low',
            callback=self._callback)
        self.stream.start()
        self.device = device
        logger.info('audio: %s -> %s', device.name, sd.query_devices(output)['name'])

    def stop(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            finally:
                self.stream = None
                self.device = None
