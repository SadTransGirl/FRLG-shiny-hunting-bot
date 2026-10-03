import numpy as np

from shinybot.audio import AudioDevice, guess_capture_device, mix


def test_mix_copies_with_volume():
    indata = np.full((4, 2), 0.5, np.float32)
    outdata = np.zeros((4, 2), np.float32)
    mix(indata, outdata, 0.5, muted=False)
    assert np.allclose(outdata, 0.25)


def test_mix_mute_outputs_silence():
    outdata = np.ones((4, 2), np.float32)
    mix(np.ones((4, 2), np.float32), outdata, 1.0, muted=True)
    assert not outdata.any()


def test_mix_mono_input_to_stereo_output():
    indata = np.array([[0.1], [0.2]], np.float32)
    outdata = np.zeros((2, 2), np.float32)
    mix(indata, outdata, 1.0, muted=False)
    assert np.allclose(outdata, [[0.1, 0.1], [0.2, 0.2]])


def test_guess_prefers_capture_card_over_microphone():
    devices = [AudioDevice(1, 'Microphone (Realtek Audio)', 2, 48000),
               AudioDevice(2, 'Digital Audio Interface (USB3 Video)', 2, 48000),
               AudioDevice(3, 'Line (Elgato Game Capture HD)', 2, 48000)]
    assert guess_capture_device(devices).index == 3
    assert guess_capture_device(devices[:2]).index == 2
    assert guess_capture_device(devices[:1]) is None
