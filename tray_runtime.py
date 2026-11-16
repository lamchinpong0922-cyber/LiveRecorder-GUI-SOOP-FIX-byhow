"""Windows notification-area controls for the desktop and web interfaces."""

from __future__ import annotations

import threading
from collections.abc import Callable

import pystray
from PIL import Image, ImageDraw


_LABELS = {
    "zh-TW": {
        "open": "開啟介面",
        "running": "錄製核心：執行中",
        "stopped": "錄製核心：未啟動",
        "exit": "停止錄製並結束程式",
    },
    "en": {
        "open": "Open interface",
        "running": "Recorder core: running",
        "stopped": "Recorder core: stopped",
        "exit": "Stop recording and exit",
    },
}


def _icon_image(state: str = "inactive") -> Image.Image:
    colors = {
        "inactive": (235, 239, 245, 255),
        "monitoring": (82, 128, 255, 255),
        "recording": (255,  70,  70, 255),
    }
    color = colors.get(state, colors["inactive"])

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (3, 3, 60, 60),
        radius=12,
        fill=(27, 33, 46, 255)
    )
    draw.ellipse((13, 13, 50, 50), outline=color, width=6)
    draw.ellipse((27, 27, 36, 36), fill=color)
    return image


class TrayController:
    def __init__(
        self,
        *,
        open_ui: Callable[[], None],
        exit_app: Callable[[], None],
        recorder_running: Callable[[], bool],
        language: Callable[[], str | None],
        recording_active: Callable[[], bool] | None = None,
    ) -> None:
        self._open_ui = open_ui
        self._exit_app = exit_app
        self._recorder_running = recorder_running
        self._language = language
        self._recording_active = recording_active or recorder_running
        self._icon_recording = False
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._icon = pystray.Icon(
            "LiveRecorder",
            _icon_image(),
            "LiveRecorder",
            menu=pystray.Menu(
                pystray.MenuItem(lambda _: self._label("open"), self._on_open, default=True),
                pystray.MenuItem(
                    lambda _: self._label("running" if self._recorder_running() else "stopped"),
                    lambda _icon, _item: None,
                    enabled=False,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(lambda _: self._label("exit"), self._on_exit),
            ),
        )

    def _label(self, key: str) -> str:
        labels = _LABELS["en" if self._language() == "en" else "zh-TW"]
        return labels[key]

    def _on_open(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self._open_ui()

    def _on_exit(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self._exit_app()

    def _monitor_icon(self) -> None:
        while not self._monitor_stop.is_set():
            try:
                monitoring = bool(self._recorder_running())
                recording = monitoring and bool(self._recording_active())

                state = (
                    "recording" if recording
                    else "monitoring" if monitoring
                    else "inactive"
                )

                if state != getattr(self, "_icon_state", None):
                    self._icon.icon = _icon_image(state)
                    self._icon_state = state

            except Exception:
                # 狀態暫時讀取失敗時，不影響系統匣及錄影程序。
                pass

            self._monitor_stop.wait(1)

    def start_background(self) -> None:
        if self._thread is not None:
            return

        def setup(icon: pystray.Icon) -> None:
            try:
                monitoring = bool(self._recorder_running())
                recording = monitoring and bool(self._recording_active())

                state = (
                    "recording" if recording
                    else "monitoring" if monitoring
                    else "inactive"
                )
            except Exception:
                state = "inactive"

            self._icon_state = state
            icon.icon = _icon_image(state)
            icon.visible = True
            self._ready.set()

        def run() -> None:
            try:
                self._icon.run(setup)
            except BaseException as exc:
                self._error = exc
                self._ready.set()

        self._monitor_stop.clear()
        self._thread = threading.Thread(
            target=run,
            name="LiveRecorder-tray",
            daemon=True
        )
        self._thread.start()

        if not self._ready.wait(timeout=5) or self._error is not None:
            self.stop()
            raise RuntimeError("無法建立 LiveRecorder 系統匣圖示") from self._error

        self._monitor_thread = threading.Thread(
            target=self._monitor_icon,
            name="LiveRecorder-tray-state",
            daemon=True
        )
        self._monitor_thread.start()

    def stop(self) -> None:
        self._monitor_stop.set()

        if self._monitor_thread is not None:
            if threading.current_thread() is not self._monitor_thread:
                self._monitor_thread.join(timeout=2)
            self._monitor_thread = None

        if self._thread is None:
            return

        self._icon.stop()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=3)
        self._thread = None
