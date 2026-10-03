"""Read frames from a capture card (or OBS Virtual Camera) with OpenCV."""

from __future__ import annotations

import logging
import sys
import threading
import time

import cv2
import numpy as np

logger = logging.getLogger(__name__)


def _open(camera: int, width: int, height: int) -> cv2.VideoCapture:
    # DirectShow opens capture cards quickly and honours the requested size on Windows.
    backend = cv2.CAP_DSHOW if sys.platform == 'win32' else cv2.CAP_ANY
    capture = cv2.VideoCapture(camera, backend)
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return capture


def list_cameras(max_index: int = 6) -> list[tuple[int, int, int]]:
    """Return (index, width, height) for each camera index that delivers frames."""
    found = []
    for index in range(max_index):
        capture = _open(index, 1920, 1080)
        try:
            ok, frame = capture.read()
            if ok and frame is not None:
                found.append((index, frame.shape[1], frame.shape[0]))
        finally:
            capture.release()
    return found


class FrameGrabber:
    """Continuously reads the capture device so the newest frame is always at hand.

    Capture cards buffer frames; reading on demand would return stale images.
    """

    def __init__(self, camera: int = 0, width: int = 1920, height: int = 1080) -> None:
        self.camera = camera
        self.width = width
        self.height = height
        self._capture: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Condition()
        self._frame: np.ndarray | None = None
        self._frame_time = 0.0

    def start(self) -> None:
        self._capture = _open(self.camera, self.width, self.height)
        if not self._capture.isOpened():
            raise RuntimeError(
                f'could not open camera {self.camera}. Run "python -m shinybot cameras" to list '
                'them. If OBS is using the capture card, start its Virtual Camera and use that '
                'camera number instead.'
            )
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        frame = self.latest(timeout=5.0)
        logger.info('camera %s: %dx%d', self.camera, frame.shape[1], frame.shape[0])

    def _run(self) -> None:
        while self._running:
            ok, frame = self._capture.read()
            if not ok:
                time.sleep(0.01)
                continue
            with self._lock:
                self._frame = frame
                self._frame_time = time.monotonic()
                self._lock.notify_all()

    def latest(self, timeout: float = 2.0) -> np.ndarray:
        """The first frame captured after this call (never a buffered old one)."""
        requested = time.monotonic()
        with self._lock:
            if not self._lock.wait_for(lambda: self._frame_time > requested, timeout):
                raise RuntimeError(f'no new frame from camera {self.camera} for {timeout:.0f}s')
            return self._frame.copy()

    def peek(self) -> tuple[float, np.ndarray | None]:
        """The newest frame and its capture time, without waiting (for live previews)."""
        with self._lock:
            return self._frame_time, self._frame

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if self._capture:
            self._capture.release()

    def __enter__(self) -> FrameGrabber:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
