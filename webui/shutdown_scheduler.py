
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable


class ShutdownScheduler:
    def __init__(
        self,
        state_file: Path,
        stop_recorder: Callable[[], tuple[bool, str]],
        get_process: Callable[[], object | None],
        stop_sentinel: Path,
    ) -> None:
        self.state_file = Path(state_file)
        self.stop_recorder = stop_recorder
        self.get_process = get_process
        self.stop_sentinel = Path(stop_sentinel)

        self.exit_program: Callable[[], None] | None = None

        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None

        self.state = {
            "enabled": False,
            "mode": "once",
            "time": "03:00",
            "nextRun": None,
            "phase": "idle",
            "remaining": 0,
            "message": "",
            "conversionStatus": "IDLE",
        }

        self._load()

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.state_file)

    def _load(self) -> None:
        try:
            data = json.loads(
                self.state_file.read_text(encoding="utf-8")
            )

            if isinstance(data, dict):
                mode = data.get("mode", "once")
                value = data.get("time", "03:00")

                if mode in ("once", "daily"):
                    self.state["mode"] = mode

                try:
                    hour, minute = map(int, value.split(":"))
                    if 0 <= hour <= 23 and 0 <= minute <= 59:
                        self.state["time"] = value
                except (ValueError, AttributeError):
                    pass

                # 只有每日排程會在重新啟動後自動恢復。
                # 單次排程仍維持原本重新啟動後不自動恢復的行為。
                self.state["enabled"] = (
                    data.get("enabled") is True
                    and self.state["mode"] == "daily"
                )

        except (OSError, ValueError, TypeError):
            pass

        if self.state["enabled"]:
            target = self._next_daily(
                self.state["time"],
                datetime.now(),
            )

            self.state.update({
                "nextRun": target.isoformat(timespec="seconds"),
                "phase": "scheduled",
                "remaining": 0,
                "message": "每日定時關閉已恢復",
                "conversionStatus": "IDLE",
            })
        else:
            self.state.update({
                "enabled": False,
                "nextRun": None,
                "phase": "idle",
                "remaining": 0,
                "message": "排程尚未啟用，請重新設定",
                "conversionStatus": "IDLE",
            })

        self._save()

    @staticmethod
    def _next_daily(value: str, now: datetime) -> datetime:
        hour, minute = map(int, value.split(":"))

        target = now.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )

        if target <= now:
            target += timedelta(days=1)

        return target

    def get_state(self) -> dict:
        with self.lock:
            return dict(self.state)

    def _stop_old_thread(self) -> None:
        with self.lock:
            old_event = self.cancel_event
            old_thread = self.thread
            old_event.set()

        if (
            old_thread is not None
            and old_thread.is_alive()
            and old_thread is not threading.current_thread()
        ):
            old_thread.join(timeout=10)

            if old_thread.is_alive():
                raise RuntimeError(
                    "上一個排程執行緒尚未停止，請稍後再試"
                )

    def _start_thread(self) -> None:
        with self.lock:
            if not self.state.get("enabled"):
                return

            if not self.state.get("nextRun"):
                return

            self.cancel_event = threading.Event()
            event = self.cancel_event

            self.thread = threading.Thread(
                target=self._run,
                args=(event,),
                name="LiveRecorderShutdownScheduler",
                daemon=True,
            )

            self.thread.start()

    def start_restored_schedule(self) -> None:
        # 等 main() 設定好 exit_program 後才啟動恢復的排程。
        self._start_thread()

    def set_schedule(self, mode: str, value: str) -> dict:
        if mode not in ("once", "daily"):
            raise ValueError("排程模式不正確")

        try:
            hour, minute = map(int, value.split(":"))

            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError

        except (ValueError, AttributeError):
            raise ValueError("請輸入有效的 24 小時制時間")

        self._stop_old_thread()

        with self.lock:
            now = datetime.now()
            target = self._next_daily(value, now)

            self.state = {
                "enabled": True,
                "mode": mode,
                "time": value,
                "nextRun": target.isoformat(timespec="seconds"),
                "phase": "scheduled",
                "remaining": 0,
                "message": "定時關閉程式已啟用",
                "conversionStatus": "IDLE",
            }

            self._save()

        self._start_thread()

        return self.get_state()

    def cancel(self) -> dict:
        self._stop_old_thread()

        with self.lock:
            self.state.update({
                "enabled": False,
                "nextRun": None,
                "phase": "cancelled",
                "remaining": 0,
                "message": "定時關閉程式已取消",
                "conversionStatus": "IDLE",
            })

            self._save()

            return dict(self.state)

    def stop(self) -> None:
        # 程式退出時只停止目前的監控執行緒。
        # 不清除每日排程的 enabled 狀態。
        with self.lock:
            event = self.cancel_event
            thread = self.thread
            event.set()

        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=10)

    def _update(self, **values) -> None:
        with self.lock:
            self.state.update(values)
            self._save()

    def _run(self, event: threading.Event) -> None:
        while not event.is_set():
            with self.lock:
                enabled = self.state.get("enabled")
                next_run_text = self.state.get("nextRun")

            if not enabled or not next_run_text:
                return

            try:
                next_run = datetime.fromisoformat(next_run_text)
            except (TypeError, ValueError):
                self._update(
                    enabled=False,
                    phase="error",
                    message="排程時間資料錯誤",
                )
                return

            now = datetime.now()

            if now < next_run:
                event.wait(
                    min(1, max(0, (next_run - now).total_seconds()))
                )
                continue

            self._trigger(event)
            return

    def _write_shutdown_log(self, message: str) -> None:
        """Append scheduler/conversion log; retry a secondary location if needed."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}\n"
        primary = Path(r"O:\\userssss\\WIN11\\TEMP") / "LiveRecorder_Shutdown.log"
        fallback = Path(__file__).resolve().parent / "LiveRecorder_Shutdown_Fallback.log"

        for log_file in (primary, fallback):
            try:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                with log_file.open("a", encoding="utf-8") as stream:
                    stream.write(line)
                return
            except Exception:
                continue

        # Last resort: surface logging failure to the process console.
        try:
            print(f"[SHUTDOWN LOG FAILURE] {line.rstrip()}", flush=True)
        except Exception:
            pass
    def _trigger(self, event: threading.Event) -> None:
        with self.lock:
            if event.is_set():
                return

            mode = self.state.get("mode", "once")

            self.state.update({
                "enabled": mode == "daily",
                "nextRun": None,
                "phase": "stopping",
                "remaining": 0,
                "conversionStatus": "PENDING",
                "message": "時間已到，正在停止錄製並等待轉檔完成",
            })

            self._save()

        if event.is_set():
            return

        self._write_shutdown_log("=" * 55)
        self._write_shutdown_log("排程時間到，開始執行關閉流程")
        self._write_shutdown_log(f"排程模式：{mode}")

        result_file = self.stop_sentinel.parent / ".stop_all_result.json"
        result_file_ready = True

        try:
            result_file.unlink(missing_ok=True)
            self._write_shutdown_log("已清除上一次的轉檔結果，準備記錄本次結果")
        except OSError as exc:
            result_file_ready = False
            self._write_shutdown_log(f"警告：無法清除舊轉檔結果：{exc}")

        process_before_stop = self.get_process()
        had_running_process = (
            process_before_stop is not None
            and process_before_stop.poll() is None
        )

        self._write_shutdown_log(
            f"停止前核心狀態：{'RUNNING' if had_running_process else 'NOT_RUNNING'}"
        )

        try:
            stop_ok, stop_message = self.stop_recorder()
            self._write_shutdown_log(
                f"停止錄影指令：{'SUCCESS' if stop_ok else 'FAILED'}；{stop_message}"
            )

            if not stop_ok:
                process = self.get_process()

                if process is not None and process.poll() is None:
                    self._write_shutdown_log("停止錄影失敗，核心仍在執行，取消關閉")
                    self._update(
                        phase="error",
                        message=f"停止錄製失敗：{stop_message}",
                        conversionStatus="UNKNOWN",
                    )
                    return

        except Exception as exc:
            self._write_shutdown_log(f"停止錄影發生例外：{type(exc).__name__}: {exc}")
            self._update(
                phase="error",
                message=f"停止錄製失敗：{exc}",
                conversionStatus="UNKNOWN",
            )
            return

        process = self.get_process()

        if process is not None:
            self._write_shutdown_log("開始等待錄影核心退出")
            while process.poll() is None:
                if event.wait(1):
                    self._write_shutdown_log("排程等待被取消，停止關閉流程")
                    return
            self._write_shutdown_log("錄影核心已退出")

        if event.is_set():
            self._write_shutdown_log("排程已取消，不繼續關閉")
            return

        conversion_status = "SUCCESS"
        conversion_message = "沒有待處理的轉檔工作"
        items = []
        failed = []

        if had_running_process:
            if not result_file_ready:
                conversion_status = "UNKNOWN"
                conversion_message = "無法清除舊結果，不能確認本次轉檔結果"
            else:
                try:
                    summary = json.loads(
                        result_file.read_text(encoding="utf-8")
                    )

                    if not isinstance(summary, dict):
                        raise ValueError("結果檔不是有效物件")

                    items = summary.get("items")
                    completed_at = summary.get("completed_at")

                    if not isinstance(items, list):
                        raise ValueError("缺少有效的 items 清單")

                    if not isinstance(completed_at, (int, float)):
                        raise ValueError("缺少有效的 completed_at 完成時間")

                    for index, item in enumerate(items, start=1):
                        if not isinstance(item, dict):
                            raise ValueError(f"第 {index} 個轉檔項目格式錯誤")

                        if not isinstance(item.get("success"), bool):
                            raise ValueError(f"第 {index} 個轉檔項目缺少 success 狀態")

                        path = str(item.get("path", "未提供檔名"))
                        success = item["success"]
                        error = str(item.get("error", "") or "")

                        if success:
                            self._write_shutdown_log(
                                f"轉檔成功：{path}"
                            )
                        else:
                            failed.append(item)
                            self._write_shutdown_log(
                                f"轉檔失敗：{path}；原因：{error or '未提供錯誤原因'}"
                            )

                    # 以逐項結果為準，不只相信摘要中的 ok 欄位。
                    if failed:
                        conversion_status = "FAILED"
                        conversion_message = (
                            f"轉檔工作已全部結束，但有 {len(failed)} 個失敗項目"
                        )
                    else:
                        conversion_status = "SUCCESS"
                        conversion_message = (
                            f"所有轉檔工作已結束，共 {len(items)} 個項目"
                        )

                    if summary.get("ok") is not (not failed):
                        self._write_shutdown_log(
                            "警告：結果摘要 ok 欄位與逐項結果不一致，以逐項結果為準"
                        )

                    completed_time = datetime.fromtimestamp(completed_at).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    self._write_shutdown_log(
                        f"核心轉檔結果完成時間：{completed_time}"
                    )

                except Exception as exc:
                    conversion_status = "UNKNOWN"
                    conversion_message = f"無法確認完整轉檔結果：{exc}"
                    self._write_shutdown_log(
                        f"轉檔結果核對失敗：{type(exc).__name__}: {exc}"
                    )

        success_count = sum(
            1 for item in items
            if isinstance(item, dict) and item.get("success") is True
        )
        failed_count = len(failed)

        self._write_shutdown_log(
            f"轉檔總數：{len(items)}；成功：{success_count}；失敗：{failed_count}"
        )
        self._write_shutdown_log(
            f"轉檔最終狀態：{conversion_status}"
        )

        if conversion_status == "SUCCESS":
            self._write_shutdown_log("確認：所有轉檔工作均已結束")
        elif conversion_status == "FAILED":
            self._write_shutdown_log("確認：轉檔工作均已結束，但有失敗項目")
        else:
            self._write_shutdown_log("警告：無法確認所有轉檔工作結果")

        self._update(
            phase="completed",
            remaining=0,
            conversionStatus=conversion_status,
            message=f"{conversion_message}，正在關閉 LiveRecorder",
        )

        callback = self.exit_program

        if callback is not None and not event.is_set():
            self._write_shutdown_log("開始關閉 LiveRecorder 桌面程式")
            callback()
        else:
            self._write_shutdown_log("沒有可用的程式退出 callback，未執行視窗關閉")
