
"""Run the React desktop UI in a native WebView2 window."""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable
from typing import Any

from app_runtime import APP_ROOT, BUNDLE_ROOT
from tray_runtime import TrayController
from webui import server as recorder_server

_WINDOW_STATE_FILE = APP_ROOT / "config" / "desktop_window.json"


def load_window_size() -> tuple[int, int]:
    defaults = (1280, 780)
    try:
        data = json.loads(_WINDOW_STATE_FILE.read_text(encoding="utf-8"))
        width = int(data.get("width", defaults[0]))
        height = int(data.get("height", defaults[1]))
        if width < 950 or height < 650:
            return defaults
        return min(width, 3000), min(height, 2000)
    except Exception:
        return defaults


def _write_window_state_error(message: str) -> None:
    try:
        _WINDOW_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        error_file = _WINDOW_STATE_FILE.with_name("desktop_window.error.log")
        with error_file.open("a", encoding="utf-8") as stream:
            from datetime import datetime
            stream.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
    except Exception:
        pass


def save_window_size(
    window: Any,
    width: Any = None,
    height: Any = None,
) -> None:
    try:
        # Prefer the dimensions supplied by pywebview's resized event.
        if width is not None and height is not None:
            try:
                width, height = int(width), int(height)
            except (TypeError, ValueError):
                width, height = (window.width, window.height)
        else:
            width, height = (window.width, window.height)

        width, height = int(width), int(height)

        if width < 950 or height < 650 or width >= 3000 or height >= 2000:
            return

        _WINDOW_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = _WINDOW_STATE_FILE.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"width": width, "height": height},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(_WINDOW_STATE_FILE)

    except Exception as exc:
        _write_window_state_error(
            f"save_window_size failed: {type(exc).__name__}: {exc}"
        )


class DesktopTraySession:
    def __init__(self, window: Any, active: Callable[[], bool]) -> None:
        self._window = window
        self._active = active
        self._exiting = threading.Event()
        self._tray = TrayController(
            open_ui=self.open_ui,
            exit_app=self.exit_app,
            recorder_running=lambda: recorder_server.RECORDER.monitoring_active,
            recording_active=lambda: recorder_server.RECORDER.recording_active,
            language=recorder_server.read_ui_language,
        )

    def start(self) -> None:
        self._tray.start_background()

    def stop(self) -> None:
        self._tray.stop()

    def open_ui(self) -> None:
        if self._active() and not self._exiting.is_set():
            self._window.show()

    def exit_app(self) -> None:
        # 完全結束前保存最後一次視窗尺寸。
        if self._active():
            save_window_size(self._window)

        self._exiting.set()
        self._window.destroy()

    def on_closing(self) -> bool:
        if self._active():
            save_window_size(self._window)

        if not self._active() or self._exiting.is_set():
            return True
        # pywebview's closing event runs on the UI thread. Hide after the
        # close has been cancelled so the WinForms event handler can return.
        threading.Thread(target=self._window.hide, name="desktop-hide", daemon=True).start()
        return False


class DesktopAppBridge:
    def __init__(self) -> None:
        self._session: DesktopTraySession | None = None

    def exit_app(self) -> dict[str, bool]:
        if self._session is None:
            return {"ok": False}
        self._session.exit_app()
        return {"ok": True}


class ModePickerBridge:
    """Handle the first choice without starting recording before it is made."""

    def __init__(self) -> None:
        # pywebview recursively inspects public js_api attributes. Keep the
        # native Window private or it will traverse its entire object graph.
        self._mode: str | None = None
        self._window: Any = None
        self._tray_session: DesktopTraySession | None = None
        self._lock = threading.Lock()

    def choose_mode(self, mode: str) -> dict[str, str]:
        if mode not in ("desktop", "web"):
            raise ValueError("不支援的介面模式")
        with self._lock:
            if self._mode is not None:
                return {"mode": self._mode}
            if mode == "desktop":
                if self._tray_session is None:
                    raise RuntimeError("系統匣尚未就緒")
                self._tray_session.start()
            self._mode = mode

        if mode == "desktop":
            try:
                recorder_server.start_enabled_rooms("桌面版")
            except Exception as exc:
                recorder_server.RECORDER.append(f"自動啟動錄製失敗：{exc}", "error")
            try:
                self._window.title = "LiveRecorder 控制台"
            except Exception:
                pass  # The React view can still load at the picker's size.
        else:
            # Let the JS bridge reply before destroying the picker window.
            closer = threading.Timer(0.2, self._window.destroy)
            closer.daemon = True
            closer.start()
        return {"mode": mode}


def run_mode_picker() -> str | None:
    import webview

    index = BUNDLE_ROOT / "desktop-ui" / "dist" / "index.html"
    if not index.is_file():
        raise FileNotFoundError(f"找不到桌面介面：{index}。請先執行 desktop-ui 的 npm run build。")

    httpd = recorder_server.create_server(0, auto_start=False)
    worker = threading.Thread(target=httpd.serve_forever, name="mode-picker-http", daemon=True)
    worker.start()
    bridge = ModePickerBridge()
    session: DesktopTraySession | None = None
    completed = False
    try:
        port = httpd.server_address[1]
        bridge._window = webview.create_window(
            "LiveRecorder · 選擇介面", f"http://127.0.0.1:{port}/desktop/?launcher=1",
            js_api=bridge, width=820, height=500, min_size=(700, 450),
            background_color="#121720",
        )
        session = DesktopTraySession(bridge._window, lambda: bridge._mode == "desktop")
        bridge._tray_session = session
        bridge._window.events.closing += session.on_closing
        webview.start(gui="edgechromium", private_mode=False)
        completed = True
        return bridge._mode
    finally:
        if session is not None:
            session.stop()
        httpd.shutdown()
        worker.join(timeout=3)
        if completed and bridge._mode == "web":
            # launch_web starts a new server in this process. Keep the recorder's
            # Windows Job alive so its later core process remains tied to it.
            httpd.server_close()
        else:
            recorder_server.close_server(httpd)


def run_desktop() -> None:
    import webview

    index = BUNDLE_ROOT / "desktop-ui" / "dist" / "index.html"
    if not index.is_file():
        raise FileNotFoundError(f"找不到桌面介面：{index}。請先執行 desktop-ui 的 npm run build。")

    httpd = recorder_server.create_server(18765)
    worker = threading.Thread(target=httpd.serve_forever, name="desktop-ui-http", daemon=True)
    worker.start()
    session: DesktopTraySession | None = None
    try:
        port = httpd.server_address[1]
        width, height = load_window_size()
        bridge = DesktopAppBridge()
        window = webview.create_window(
            "LiveRecorder 控制台", f"http://127.0.0.1:{port}/desktop/",
            js_api=bridge,
            width=width, height=height, min_size=(950, 650), maximized=False,
            background_color="#121720",
        )
        session = DesktopTraySession(window, lambda: True)
        bridge._session = session
        window.events.closing += session.on_closing
        window.events.resized += lambda width=None, height=None, *_: save_window_size(window, width, height)
        session.start()

        # 排程到期後，使用桌面版的正常關閉流程退出視窗。
        recorder_server.SHUTDOWN_SCHEDULER.exit_program = session.exit_app

        # 恢復已儲存的每日排程。
        recorder_server.SHUTDOWN_SCHEDULER.start_restored_schedule()

        if "--minimize-after-5" in sys.argv:
            def schedule_minimize():
                timer = threading.Timer(5, window.hide)
                timer.daemon = True
                timer.start()

            webview.start(schedule_minimize, gui="edgechromium", private_mode=False)
        else:
            webview.start(gui="edgechromium", private_mode=False)
    finally:
        if session is not None:
            session.stop()
        httpd.shutdown()
        worker.join(timeout=3)
        recorder_server.close_server(httpd)
