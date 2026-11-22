# -*- encoding: utf-8 -*-

"""
Author: Hmily
GitHub: https://github.com/ihmily
Date: 2023-07-17 23:52:05
Update: 2025-10-23 19:48:05
Copyright (c) 2023-2025 by Hmily, All Rights Reserved.
Function: Record live stream video.
"""
import asyncio
import os
import sys
import builtins
import subprocess
import signal
import threading
import time
import json
import datetime
import re
import shutil
import random
import uuid
from pathlib import Path
import urllib.request
from urllib.error import URLError, HTTPError
from typing import Any
import configparser
import httpx
import queue
from src import spider, stream
from src.ffmpeg_options import (
    recording_corrupt_packet_options,
    recording_input_options,
    recording_log_level,
)
from src.proxy import ProxyDetector
from src.utils import logger
from src import utils
from msg_push import (
    dingtalk, xizhi, tg_bot, send_email, bark, ntfy, pushplus
)
from ffmpeg_install import (
    check_ffmpeg, ffmpeg_path, current_env_path
)

version = "v4.0.7"

recording = set()
error_count = 0
pre_max_request = 10
max_request_lock = threading.Lock()
# Serialize SOOP authentication and cookie replacement across recording threads.
soop_cookie_lock = threading.RLock()
error_window = []
error_window_size = 10
error_threshold = 5
monitoring = 0
running_list = []
url_tuples_list = []
url_comments = []
text_no_repeat_url = []
create_var = locals()
first_start = True
exit_recording = False
STOP_SENTINEL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "STOP_ALL_REQUESTED"
)


def is_stop_all_requested() -> bool:
    return os.path.isfile(STOP_SENTINEL_PATH)


def input_or_stop(prompt: str) -> str | None:
    """
    等待使用者輸入，但收到整體停止訊號時立即返回。
    輸入工作放在 daemon thread，避免 input() 阻止核心退出。
    """
    result_queue = queue.Queue(maxsize=1)

    def read_input() -> None:
        try:
            result_queue.put(input(prompt))
        except (EOFError, OSError):
            result_queue.put(None)

    input_thread = threading.Thread(
        target=read_input,
        name="LiveRecorderConsoleInput",
        daemon=True,
    )
    input_thread.start()

    while not is_stop_all_requested():
        try:
            return result_queue.get(timeout=0.25)
        except queue.Empty:
            continue

    return None
need_update_line_list = []
first_run = True
not_record_list = []
start_display_time = datetime.datetime.now()
global_proxy = False
recording_time_list = {}
recording_url_list = {}
script_path = os.path.split(os.path.realpath(sys.argv[0]))[0]
data_root = os.environ.get('LIVE_RECORDER_DATA_ROOT', script_path)

# 桌面系統匣讀取的即時錄影狀態。
# 使用核心目錄，確保 GUI 與錄製核心讀取同一份檔案。
RECORDING_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".live_recording_state.json"
)

STOP_ALL_RESULT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".stop_all_result.json"
)
_STOP_ALL_CONVERSION_RESULTS = []
_STOP_ALL_CONVERSION_LOCK = threading.Lock()


def record_stop_conversion_result(path, success, error=""):
    try:
        item = {
            "path": str(path),
            "success": bool(success),
            "error": str(error or "")
        }
        with _STOP_ALL_CONVERSION_LOCK:
            _STOP_ALL_CONVERSION_RESULTS.append(item)
    except Exception:
        pass


def write_stop_all_result():
    try:
        with _STOP_ALL_CONVERSION_LOCK:
            items = list(_STOP_ALL_CONVERSION_RESULTS)

        failed = [item for item in items if not item["success"]]
        payload = {
            "ok": not failed,
            "items": items,
            "failed": failed,
            "completed_at": time.time()
        }

        temporary = STOP_ALL_RESULT_FILE + f".{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8") as result_file:
            json.dump(payload, result_file, ensure_ascii=False, indent=2)
        os.replace(temporary, STOP_ALL_RESULT_FILE)

    except Exception as exc:
        logger.error(f"寫入停止全部轉檔結果失敗：{exc}")


_RECORDING_STATE_PUBLISH_LOCK = threading.Lock()

def _serialize_recording_state_publish(function):
    def wrapper():
        with _RECORDING_STATE_PUBLISH_LOCK:
            return function()
    return wrapper


@_serialize_recording_state_publish
def publish_recording_state():
    try:
        active_recordings = []
        for record_name in list(recording):
            info = recording_time_list.get(record_name)
            if not info:
                continue
            started_at = info[0]
            active_recordings.append({
                "name": str(record_name),
                "url": str(recording_url_list.get(record_name, "")),
                "started_at": started_at.timestamp(),
                "quality": str(info[1])
            })

        payload = {
            "pid": os.getpid(),
            "updated_at": time.time(),
            "recording": active_recordings
        }

        # 每個核心程序使用自己的狀態檔，避免互相覆蓋。
        state_path = RECORDING_STATE_FILE + f".{os.getpid()}.json"
        temporary = state_path + f".{threading.get_ident()}.tmp"

        with open(temporary, "w", encoding="utf-8") as state_file:
            json.dump(payload, state_file, ensure_ascii=False)

        os.replace(temporary, state_path)

    except Exception as exc:
        # 不影響錄影工作，但保留錯誤供診斷。
        try:
            with open(
                RECORDING_STATE_FILE + ".error.log",
                "a",
                encoding="utf-8"
            ) as error_file:
                error_file.write(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"PID={os.getpid()} {type(exc).__name__}: {exc}\n"
                )
        except Exception:
            pass


# 每次核心啟動先清除上次異常結束留下的狀態。
publish_recording_state()
config_file = f'{data_root}/config/config.ini'
url_config_file = f'{data_root}/config/URL_config.ini'
backup_dir = f'{data_root}/backup_config'
text_encoding = 'utf-8-sig'
rstr = r"[\/\\\:\*\？?\"\<\>\|&#.。,， ~！· ]"
default_path = f'{data_root}/downloads'
os.makedirs(default_path, exist_ok=True)
file_update_lock = threading.Lock()
os_type = os.name
clear_command = "cls" if os_type == 'nt' else "clear"
color_obj = utils.Color()
os.environ['PATH'] = ffmpeg_path + os.pathsep + current_env_path


def signal_handler(_signal, _frame):
    sys.exit(0)


signal.signal(signal.SIGTERM, signal_handler)


def display_info() -> None:
    global start_display_time
    time.sleep(5)
    while True:
        try:
            sys.stdout.flush()
            time.sleep(5)
            # A GUI wrapper captures stdout through a pipe. `cls` only works
            # with a real console and otherwise raises Invalid argument.
            if Path(sys.executable).name != 'pythonw.exe' and sys.stdout.isatty():
                os.system(clear_command)
            print(f"\r共监测{monitoring}个直播中", end=" | ")
            print(f"同一时间访问网络的线程数: {max_request}", end=" | ")
            print(f"是否开启代理录制: {'是' if use_proxy else '否'}", end=" | ")
            if split_video_by_time:
                print(f"录制分段开启: {split_time}秒", end=" | ")
            else:
                print("录制分段开启: 否", end=" | ")
            if create_time_file:
                print("是否生成时间文件: 是", end=" | ")
            print(f"录制视频质量为: {video_record_quality}", end=" | ")
            print(f"录制视频格式为: {video_save_type}", end=" | ")
            print(f"目前瞬时错误数为: {error_count}", end=" | ")
            now = time.strftime("%H:%M:%S", time.localtime())
            print(f"当前时间: {now}")

            if len(recording) == 0:
                time.sleep(5)
                if monitoring == 0:
                    print("\r没有正在监测和录制的直播")
                else:
                    print(f"\r没有正在录制的直播 循环监测间隔时间：{delay_default}秒")
            else:
                now_time = datetime.datetime.now()
                print("x" * 60)
                no_repeat_recording = list(set(recording))
                print(f"正在录制{len(no_repeat_recording)}个直播: ")
                for recording_live in no_repeat_recording:
                    rt, qa = recording_time_list[recording_live]
                    have_record_time = now_time - rt
                    print(f"{recording_live}[{qa}] 正在录制中 {str(have_record_time).split('.')[0]}")

                # print('\n本软件已运行：'+str(now_time - start_display_time).split('.')[0])
                print("x" * 60)
                start_display_time = now_time
        except Exception as e:
            logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")


def update_file(file_path: str, old_str: str, new_str: str, start_str: str = None) -> str | None:
    if old_str == new_str and start_str is None:
        return old_str
    with file_update_lock:
        file_data = []
        with open(file_path, "r", encoding=text_encoding) as f:
            try:
                for text_line in f:
                    if old_str in text_line:
                        text_line = text_line.replace(old_str, new_str)
                        if start_str:
                            text_line = f'{start_str}{text_line}'
                    if text_line not in file_data:
                        file_data.append(text_line)
            except RuntimeError as e:
                logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")
                if ini_URL_content:
                    with open(file_path, "w", encoding=text_encoding) as f2:
                        f2.write(ini_URL_content)
                    return old_str
        if file_data:
            with open(file_path, "w", encoding=text_encoding) as f:
                f.write(''.join(file_data))
        return new_str


def delete_line(file_path: str, del_line: str, delete_all: bool = False) -> None:
    with file_update_lock:
        with open(file_path, 'r+', encoding=text_encoding) as f:
            lines = f.readlines()
            f.seek(0)
            f.truncate()
            skip_line = False
            for txt_line in lines:
                if del_line in txt_line:
                    if delete_all or not skip_line:
                        skip_line = True
                        continue
                else:
                    skip_line = False
                f.write(txt_line)


def get_startup_info(system_type: str):
    if system_type == 'nt':
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    else:
        startup_info = None
    return startup_info


def segment_video(converts_file_path: str, segment_save_file_path: str, segment_format: str, segment_time: str,
                  is_original_delete: bool = True) -> None:
    try:
        if os.path.exists(converts_file_path) and os.path.getsize(converts_file_path) > 0:
            ffmpeg_command = [
                "ffmpeg",
                "-i", converts_file_path,
                "-c:v", "copy",
                "-c:a", "copy",
                "-map", "0",
                "-f", "segment",
                "-segment_time", segment_time,
                "-segment_format", segment_format,
                "-reset_timestamps", "1",
                "-movflags", "+frag_keyframe+empty_moov",
                segment_save_file_path,
            ]
            _output = subprocess.check_output(
                ffmpeg_command, stderr=subprocess.STDOUT, startupinfo=get_startup_info(os_type)
            )
            if is_original_delete:
                time.sleep(1)
                if os.path.exists(converts_file_path):
                    os.remove(converts_file_path)
    except subprocess.CalledProcessError as e:
        logger.error(f'Error occurred during conversion: {e}')
    except Exception as e:
        logger.error(f'An unknown error occurred: {e}')


def converts_mp4(
    converts_file_path: str,
    is_original_delete: bool = True,
    force_copy: bool = False
) -> bool:
    output_file_path = converts_file_path.rsplit('.', maxsplit=1)[0] + ".mp4"

    try:
        if not os.path.exists(converts_file_path):
            logger.error(f"MP4 轉檔失敗，找不到原始檔案：{converts_file_path}")
            return False

        if os.path.getsize(converts_file_path) <= 0:
            logger.error(f"MP4 轉檔失敗，原始檔案是空檔案：{converts_file_path}")
            return False

        if not force_copy and converts_to_h264:
            color_obj.print_colored(
                "正在轉碼為MP4格式並重新編碼為h264\n",
                color_obj.YELLOW
            )

            ffmpeg_command = [
                "ffmpeg",
                "-y",
                "-i", converts_file_path,
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-vf", "format=yuv420p",
                "-c:a", "copy",
                "-f", "mp4",
                output_file_path,
            ]

        else:
            color_obj.print_colored(
                "正在以不重新編碼方式轉換MP4格式\n",
                color_obj.YELLOW
            )

            ffmpeg_command = [
                "ffmpeg",
                "-y",
                "-i", converts_file_path,
                "-c:v", "copy",
                "-c:a", "copy",
                "-f", "mp4",
                output_file_path,
            ]

        subprocess.run(
            ffmpeg_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
            startupinfo=get_startup_info(os_type)
        )

        if (
            not os.path.isfile(output_file_path)
            or os.path.getsize(output_file_path) <= 0
        ):
            logger.error(f"MP4 轉檔未產生有效檔案：{output_file_path}")
            return False

        logger.info(f"MP4 轉檔成功：{output_file_path}")

        if is_original_delete:
            time.sleep(1)
            if os.path.exists(converts_file_path):
                os.remove(converts_file_path)

        return True

    except subprocess.CalledProcessError as e:
        logger.error(f"MP4 轉檔失敗：{converts_file_path}，錯誤：{e}")
        return False

    except Exception as e:
        logger.error(f"MP4 轉檔發生未知錯誤：{converts_file_path}，錯誤：{e}")
        return False


def converts_m4a(converts_file_path: str, is_original_delete: bool = True) -> None:
    try:
        if os.path.exists(converts_file_path) and os.path.getsize(converts_file_path) > 0:
            _output = subprocess.check_output([
                "ffmpeg", "-i", converts_file_path,
                "-n", "-vn",
                "-c:a", "aac", "-bsf:a", "aac_adtstoasc", "-ab", "320k",
                converts_file_path.rsplit('.', maxsplit=1)[0] + ".m4a",
            ], stderr=subprocess.STDOUT, startupinfo=get_startup_info(os_type))
            if is_original_delete:
                time.sleep(1)
                if os.path.exists(converts_file_path):
                    os.remove(converts_file_path)
    except subprocess.CalledProcessError as e:
        logger.error(f'Error occurred during conversion: {e}')
    except Exception as e:
        logger.error(f'An unknown error occurred: {e}')


def generate_subtitles(record_name: str, ass_filename: str, sub_format: str = 'srt') -> None:
    index_time = 0
    today = datetime.datetime.now()
    re_datatime = today.strftime('%Y-%m-%d %H:%M:%S')

    def transform_int_to_time(seconds: int) -> str:
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    while True:
        index_time += 1
        txt = str(index_time) + "\n" + transform_int_to_time(index_time) + ',000 --> ' + transform_int_to_time(
            index_time + 1) + ',000' + "\n" + str(re_datatime) + "\n\n"

        with open(f"{ass_filename}.{sub_format.lower()}", 'a', encoding=text_encoding) as f:
            f.write(txt)

        if record_name not in recording:
            return
        time.sleep(1)
        today = datetime.datetime.now()
        re_datatime = today.strftime('%Y-%m-%d %H:%M:%S')


def adjust_max_request() -> None:
    global max_request, error_count, pre_max_request, error_window
    preset = max_request

    while True:
        time.sleep(5)
        with max_request_lock:
            if error_window:
                error_rate = sum(error_window) / len(error_window)
            else:
                error_rate = 0

            if error_rate > error_threshold:
                max_request = max(1, max_request - 1)
            elif error_rate < error_threshold / 2 and max_request < preset:
                max_request += 1
            else:
                pass

            if pre_max_request != max_request:
                pre_max_request = max_request
                print(f"\r同一时间访问网络的线程数动态改为 {max_request}")

        error_window.append(error_count)
        if len(error_window) > error_window_size:
            error_window.pop(0)
        error_count = 0


def push_message(record_name: str, live_url: str, content: str) -> None:
    msg_title = push_message_title.strip() or "直播间状态更新通知"
    push_functions = {
        '微信': lambda: xizhi(xizhi_api_url, msg_title, content),
        '钉钉': lambda: dingtalk(dingtalk_api_url, content, dingtalk_phone_num, dingtalk_is_atall),
        '邮箱': lambda: send_email(
            email_host, login_email, email_password, sender_email, sender_name,
            to_email, msg_title, content, smtp_port, open_smtp_ssl
        ),
        'TG': lambda: tg_bot(tg_chat_id, tg_token, content),
        'BARK': lambda: bark(
            bark_msg_api, title=msg_title, content=content, level=bark_msg_level, sound=bark_msg_ring
        ),
        'NTFY': lambda: ntfy(
            ntfy_api, title=msg_title, content=content, tags=ntfy_tags, action_url=live_url, email=ntfy_email
        ),
        'PUSHPLUS': lambda: pushplus(pushplus_token, msg_title, content),
    }

    for platform, func in push_functions.items():
        if platform in live_status_push.upper():
            try:
                result = func()
                print(f'提示信息：已经将[{record_name}]直播状态消息推送至你的{platform},'
                      f' 成功{len(result["success"])}, 失败{len(result["error"])}')
            except Exception as e:
                color_obj.print_colored(f"直播消息推送到{platform}失败: {e}", color_obj.RED)


def run_script(command: str) -> None:
    try:
        process = subprocess.Popen(
            command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=get_startup_info(os_type)
        )
        stdout, stderr = process.communicate()
        stdout_decoded = stdout.decode('utf-8')
        stderr_decoded = stderr.decode('utf-8')
        if stdout_decoded.strip():
            print(stdout_decoded)
        if stderr_decoded.strip():
            print(stderr_decoded)
    except PermissionError as e:
        logger.error(e)
        logger.error('脚本无执行权限!, 若是Linux环境, 请先执行:chmod +x your_script.sh 授予脚本可执行权限')
    except OSError as e:
        logger.error(e)
        logger.error('Please add `#!/bin/bash` at the beginning of your bash script file.')


def clear_record_info(record_name: str, record_url: str) -> None:
    global monitoring
    recording.discard(record_name)
    recording_url_list.pop(record_name, None)
    publish_recording_state()
    if record_url in url_comments and record_url in running_list:
        running_list.remove(record_url)
        monitoring -= 1
        color_obj.print_colored(f"[{record_name}]已经从录制列表中移除\n", color_obj.YELLOW)


def is_record_url_disabled(record_url: str) -> bool:
    """Read the URL file directly so a GUI stop takes effect immediately."""
    try:
        with open(url_config_file, "r", encoding=text_encoding, errors="ignore") as file:
            for raw_line in file:
                line = raw_line.strip()
                if line.startswith("#") and record_url in line.lstrip("#").strip():
                    return True
    except OSError:
        pass
    return False


def direct_download_stream(source_url: str, save_path: str, record_name: str, live_url: str, platform: str) -> bool:
    try:
        with open(save_path, 'wb') as f:
            client = httpx.Client(timeout=None)

            headers = {}
            header_params = get_record_headers(platform, live_url)
            if header_params:
                key, value = header_params.split(":", 1)
                headers[key] = value

            with client.stream('GET', source_url, headers=headers, follow_redirects=True) as response:
                if response.status_code != 200:
                    logger.error(f"请求直播流失败，状态码: {response.status_code}")
                    return False

                downloaded = 0
                chunk_size = 1024 * 16

                for chunk in response.iter_bytes(chunk_size):
                    if live_url in url_comments or exit_recording or is_stop_all_requested():
                        color_obj.print_colored(
                            f"[{record_name}]录制时已被注释或请求停止,下载中断",
                            color_obj.YELLOW
                        )
                        clear_record_info(record_name, live_url)
                        return False

                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)

                print()
                return True

    except Exception as e:
        logger.error(f"FLV下载错误: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")
        return False






def create_segment_temp_dir() -> str:
    """
    建立只含 ASCII 檔名的暫存目錄，避免 FFmpeg strftime 在 Windows
    對 Unicode 輸出路徑解析失敗。
    """
    temp_root = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        ".segment_tmp"
    )
    os.makedirs(temp_root, exist_ok=True)
    temp_dir = os.path.join(temp_root, uuid.uuid4().hex)
    os.makedirs(temp_dir, exist_ok=False)
    return temp_dir


def finalize_segment_outputs(
    temp_dir: str,
    final_dir: str,
    final_prefix: str
) -> list[str]:
    """
    FFmpeg 完整結束後，將 ASCII 暫存檔搬到原本的 Unicode 目錄。
    檔名中的時間由 FFmpeg 在每段實際開始時產生。
    """
    moved_files = []
    if not temp_dir or not os.path.isdir(temp_dir):
        return moved_files

    os.makedirs(final_dir, exist_ok=True)
    pattern = re.compile(
        r"^segment_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.([A-Za-z0-9]+)$"
    )

    for filename in sorted(os.listdir(temp_dir)):
        match = pattern.fullmatch(filename)
        if not match:
            continue

        timestamp, extension = match.groups()
        target_path = os.path.join(
            final_dir, f"{final_prefix}{timestamp}.{extension}"
        )

        if os.path.exists(target_path):
            base, ext = os.path.splitext(target_path)
            suffix = 1
            while os.path.exists(f"{base}_{suffix:03d}{ext}"):
                suffix += 1
            target_path = f"{base}_{suffix:03d}{ext}"

        source_path = os.path.join(temp_dir, filename)
        try:
            shutil.move(source_path, target_path)
            moved_files.append(target_path)
            logger.info(f"分段檔案完成命名：{target_path}")
        except Exception as e:
            logger.error(
                f"分段檔案搬移失敗，暫存檔保留：{source_path}，錯誤：{e}"
            )

    try:
        if not os.listdir(temp_dir):
            os.rmdir(temp_dir)
            parent_dir = os.path.dirname(temp_dir)
            if os.path.isdir(parent_dir) and not os.listdir(parent_dir):
                os.rmdir(parent_dir)
    except OSError as e:
        logger.warning(f"清理分段暫存目錄失敗：{temp_dir}，{e}")

    return moved_files


def check_subprocess(record_name: str, record_url: str, ffmpeg_command: list, save_type: str,
                     script_command: str | None = None,
                     segment_temp_dir: str | None = None,
                     segment_final_dir: str | None = None,
                     segment_final_prefix: str | None = None) -> bool:
    save_file_path = ffmpeg_command[-1]

    process = subprocess.Popen(
        ffmpeg_command,
        stdin=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        startupinfo=get_startup_info(os_type)
    )

    subs_file_path = save_file_path.rsplit('.', maxsplit=1)[0]
    subs_thread_name = f'subs_{Path(subs_file_path).name}'

    if create_time_file and not split_video_by_time and '音頻' not in save_type:
        create_var[subs_thread_name] = threading.Thread(
            target=generate_subtitles,
            args=(record_name, subs_file_path)
        )
        create_var[subs_thread_name].daemon = True
        create_var[subs_thread_name].start()

    stop_requested = False
    stop_all_requested = False

    while process.poll() is None:
        stop_all_requested = is_stop_all_requested()

        if (
            record_url in url_comments
            or is_record_url_disabled(record_url)
            or exit_recording
            or stop_all_requested
        ):
            stop_requested = True

            color_obj.print_colored(
                f"[{record_name}]收到停止要求，正在通知 FFmpeg 正常结束",
                color_obj.YELLOW
            )

            clear_record_info(record_name, record_url)

            try:
                if os.name == 'nt':
                    if process.stdin and not process.stdin.closed:
                        process.stdin.write(b'q')
                        process.stdin.flush()
                        process.stdin.close()
                else:
                    process.send_signal(signal.SIGINT)

            except (BrokenPipeError, OSError, ProcessLookupError):
                pass

            break

        time.sleep(1)

    # 等待 FFmpeg 真正結束，確保 TS 已經寫入完成
    try:
        return_code = process.wait()
    except Exception as e:
        logger.error(f"等待 FFmpeg 結束失敗: {e}")
        return_code = process.returncode

    # 再檢查一次停止訊號，避免停止訊號剛好在 FFmpeg 結束時送達而漏掉
    stop_all_requested = (
        stop_all_requested or is_stop_all_requested()
    )

    if stop_all_requested:
        stop_requested = True

    stop_time = time.strftime('%Y-%m-%d %H:%M:%S')

    # FFmpeg 已完全結束，現在才搬移分段檔，避免搬動仍在寫入的檔案。
    segment_files = []
    if segment_temp_dir:
        try:
            segment_files = finalize_segment_outputs(
                segment_temp_dir,
                segment_final_dir or os.path.dirname(save_file_path),
                segment_final_prefix or ""
            )
            if segment_files:
                save_file_path = segment_files[0]
            else:
                logger.warning(
                    f"[{record_name}]FFmpeg 結束後沒有找到可搬移的分段檔：{segment_temp_dir}"
                )
        except Exception as e:
            logger.error(
                f"[{record_name}]分段檔案整理失敗，暫存目錄保留：{segment_temp_dir}，{e}"
            )

    conversion_failed = False
    conversion_attempted = False

    # 停止全部時，不論 FFmpeg 回傳碼是否為 0，
    # 都嘗試處理已經完成寫入的 TS。
    if save_type == 'TS' and converts_to_mp4:

        if stop_all_requested:
            try:
                if split_video_by_time:
                    ts_files = [
                        path for path in segment_files
                        if path.lower().endswith('.ts')
                    ]

                    if not ts_files:
                        conversion_failed = True
                        record_stop_conversion_result(
                            save_file_path, False, "找不到可轉檔的 TS 檔案"
                        )
                        logger.error(
                            f"[{record_name}]停止全部時找不到可轉檔的 TS：{save_file_path}"
                        )

                    for path in ts_files:
                        conversion_attempted = True

                        try:
                            # 強制串流複製，不重新編碼，保留 TS
                            converted = converts_mp4(
                                path,
                                is_original_delete=False,
                                force_copy=False
                            )

                            record_stop_conversion_result(
                                path, converted,
                                "" if converted else "MP4 轉檔失敗"
                            )
                            if not converted:
                                conversion_failed = True
                                logger.error(
                                    f"[{record_name}]MP4 轉檔失敗：{path}"
                                )

                        except Exception as e:
                            conversion_failed = True
                            record_stop_conversion_result(path, False, str(e))
                            logger.error(
                                f"[{record_name}]MP4 轉檔發生錯誤：{path}，{e}"
                            )

                else:
                    conversion_attempted = True

                    # 強制串流複製，不重新編碼，保留 TS
                    converted = converts_mp4(
                        save_file_path,
                        is_original_delete=False,
                        force_copy=False
                    )

                    record_stop_conversion_result(
                        save_file_path, converted,
                        "" if converted else "MP4 轉檔失敗"
                    )
                    if not converted:
                        conversion_failed = True
                        logger.error(
                            f"[{record_name}]MP4 轉檔失敗：{save_file_path}"
                        )

            except Exception as e:
                conversion_failed = True
                record_stop_conversion_result(save_file_path, False, str(e))
                logger.error(
                    f"[{record_name}]停止全部時啟動 MP4 轉檔失敗：{e}"
                )

        elif return_code == 0:
            # 一般錄影完成後的自動轉檔，維持原本行為
            try:
                if split_video_by_time:
                    ts_files = [
                        path for path in segment_files
                        if path.lower().endswith('.ts')
                    ]

                    for path in ts_files:
                        convert_thread = threading.Thread(
                            target=converts_mp4,
                            args=(path, delete_origin_file)
                        )
                        convert_thread.start()

                else:
                    convert_thread = threading.Thread(
                        target=converts_mp4,
                        args=(save_file_path, delete_origin_file)
                    )
                    convert_thread.start()

            except Exception as e:
                logger.error(f"TS 轉 MP4 啟動失敗：{e}")

    # 記錄錄影及轉檔結果
    if return_code == 0:
        print(f"\n{record_name} {stop_time} 直播錄製完成\n")

    else:
        color_obj.print_colored(
            f"\n{record_name} {stop_time} FFmpeg 結束代碼：{return_code}\n",
            color_obj.RED
        )

        if stop_all_requested:
            logger.warning(
                f"[{record_name}]停止全部時 FFmpeg 回傳非 0，仍已嘗試處理 TS"
            )

    if stop_all_requested and save_type == 'TS':
        if conversion_failed:
            logger.error(
                f"[{record_name}]停止全部完成，但 MP4 轉檔有失敗項目"
            )
        elif conversion_attempted:
            logger.info(
                f"[{record_name}]停止全部時 MP4 轉檔工作已成功完成"
            )

    # 自訂腳本維持原本條件：
    # 只有 FFmpeg 正常結束才執行，避免異常停止時誤觸發。
    if return_code == 0 and script_command:
        logger.debug("開始執行腳本命令!")

        if "python" in script_command:
            params = [
                f'--record_name "{record_name}"',
                f'--save_file_path "{save_file_path}"',
                f'--save_type {save_type}',
                f'--split_video_by_time {split_video_by_time}',
                f'--converts_to_mp4 {converts_to_mp4}',
            ]
        else:
            params = [
                f'"{record_name.split(" ", maxsplit=1)[-1]}"',
                f'"{save_file_path}"',
                save_type,
                f'split_video_by_time:{split_video_by_time}',
                f'converts_to_mp4:{converts_to_mp4}'
            ]

        script_command = (
            script_command.strip() + ' ' + ' '.join(params)
        )

        run_script(script_command)
        logger.debug("腳本命令執行結束!")

    recording.discard(record_name)
    recording_url_list.pop(record_name, None)
    publish_recording_state()

    if stop_requested:
        return True

    return False





def clean_name(input_text):
    cleaned_name = re.sub(rstr, "_", input_text.strip()).strip('_')
    cleaned_name = cleaned_name.replace("（", "(").replace("）", ")")
    if clean_emoji:
        cleaned_name = utils.remove_emojis(cleaned_name, '_').strip('_')
    return cleaned_name or '空白昵称'


def get_quality_code(qn):
    QUALITY_MAPPING = {
        "原画": "OD",
        "蓝光": "BD",
        "超清": "UHD",
        "高清": "HD",
        "标清": "SD",
        "流畅": "LD",
        "2K": "OD",
        "1080P": "OD",
        "720P": "UHD",
        "540P": "HD",
        "360P": "SD",
        "240P": "LD"
    }
    return QUALITY_MAPPING.get(qn)


def get_record_headers(platform, live_url):
    live_domain = '/'.join(live_url.split('/')[0:3])
    record_headers = {
        'PandaTV': 'origin:https://www.pandalive.co.kr',
        'WinkTV': 'origin:https://www.winktv.co.kr',
        'PopkonTV': 'origin:https://www.popkontv.com',
        'FlexTV': 'origin:https://www.flextv.co.kr',
        '千度热播': 'referer:https://qiandurebo.com',
        '17Live': 'referer:https://17.live/en/live/6302408',
        '浪Live': 'referer:https://www.lang.live',
        'shopee': f'origin:{live_domain}',
        'Blued直播': 'referer:https://app.blued.cn'
    }
    return record_headers.get(platform)


def is_flv_preferred_platform(link):
    return any(i in link for i in ["douyin", "tiktok"])


def select_source_url(link, stream_info):
    if is_flv_preferred_platform(link):
        codec = utils.get_query_params(stream_info.get('flv_url'), "codec")
        if codec and codec[0] == 'h265':
            logger.warning("FLV is not supported for h265 codec, use HLS source instead")
        else:
            return stream_info.get('flv_url')

    return stream_info.get('record_url')


def start_record(url_data: tuple, count_variable: int = -1) -> None:
    global error_count, sooplive_cookie

    while True:
        if is_stop_all_requested():
            logger.info("收到整體停止要求，停止此直播工作")
            return

        try:
            record_finished = False
            run_once = False
            start_pushed = False
            new_record_url = ''
            count_time = time.time()
            retry = 0
            record_quality_zh, record_url, anchor_name = url_data
            record_quality = get_quality_code(record_quality_zh)
            proxy_address = proxy_addr
            platform = '未知平台'
            live_domain = '/'.join(record_url.split('/')[0:3])

            if proxy_addr:
                proxy_address = None
                for platform in enable_proxy_platform_list:
                    if platform and platform.strip() in record_url:
                        proxy_address = proxy_addr
                        break

            if not proxy_address:
                if extra_enable_proxy_platform_list:
                    for pt in extra_enable_proxy_platform_list:
                        if pt and pt.strip() in record_url:
                            proxy_address = proxy_addr_bak or None

            # print(f'\r代理地址:{proxy_address}')
            # print(f'\r全局代理:{global_proxy}')
            while True:
                if is_stop_all_requested():
                    logger.info("收到整體停止要求，停止直播網址檢查")
                    return

                try:
                    port_info = []

                    if record_url.find("douyin.com/") > -1:
                        platform = '抖音直播'
                        with semaphore:
                            if 'v.douyin.com' not in record_url and '/user/' not in record_url:
                                json_data = asyncio.run(spider.get_douyin_web_stream_data(
                                    url=record_url,
                                    proxy_addr=proxy_address,
                                    cookies=dy_cookie))
                            else:
                                json_data = asyncio.run(spider.get_douyin_app_stream_data(
                                    url=record_url,
                                    proxy_addr=proxy_address,
                                    cookies=dy_cookie))
                            port_info = asyncio.run(
                                stream.get_douyin_stream_url(json_data, record_quality, proxy_address))

                    elif record_url.find("https://www.tiktok.com/") > -1:
                        platform = 'TikTok直播'
                        with semaphore:
                            if global_proxy or proxy_address:
                                json_data = asyncio.run(spider.get_tiktok_stream_data(
                                    url=record_url,
                                    proxy_addr=proxy_address,
                                    cookies=tiktok_cookie))
                                port_info = asyncio.run(
                                    stream.get_tiktok_stream_url(json_data, record_quality, proxy_address))
                            else:
                                logger.error("错误信息: 网络异常，请检查网络是否能正常访问TikTok平台")

                    elif record_url.find("https://live.kuaishou.com/") > -1:
                        platform = '快手直播'
                        with semaphore:
                            json_data = asyncio.run(spider.get_kuaishou_stream_data(
                                url=record_url,
                                proxy_addr=proxy_address,
                                cookies=ks_cookie))
                            port_info = asyncio.run(stream.get_kuaishou_stream_url(json_data, record_quality))

                    elif record_url.find("https://www.huya.com/") > -1:
                        platform = '虎牙直播'
                        with semaphore:
                            if record_quality not in ['OD', 'BD', 'UHD']:
                                json_data = asyncio.run(spider.get_huya_stream_data(
                                    url=record_url,
                                    proxy_addr=proxy_address,
                                    cookies=hy_cookie))
                                port_info = asyncio.run(stream.get_huya_stream_url(json_data, record_quality))
                            else:
                                port_info = asyncio.run(spider.get_huya_app_stream_url(
                                    url=record_url,
                                    proxy_addr=proxy_address,
                                    cookies=hy_cookie
                                ))

                    elif record_url.find("https://www.douyu.com/") > -1:
                        platform = '斗鱼直播'
                        with semaphore:
                            json_data = asyncio.run(spider.get_douyu_info_data(
                                url=record_url, proxy_addr=proxy_address, cookies=douyu_cookie))
                            port_info = asyncio.run(stream.get_douyu_stream_url(
                                json_data, video_quality=record_quality, cookies=douyu_cookie, proxy_addr=proxy_address
                            ))

                    elif record_url.find("https://www.yy.com/") > -1:
                        platform = 'YY直播'
                        with semaphore:
                            json_data = asyncio.run(spider.get_yy_stream_data(
                                url=record_url, proxy_addr=proxy_address, cookies=yy_cookie))
                            port_info = asyncio.run(stream.get_yy_stream_url(json_data))

                    elif record_url.find("https://live.bilibili.com/") > -1:
                        platform = 'B站直播'
                        with semaphore:
                            json_data = asyncio.run(spider.get_bilibili_room_info(
                                url=record_url, proxy_addr=proxy_address, cookies=bili_cookie))
                            port_info = asyncio.run(stream.get_bilibili_stream_url(
                                json_data, video_quality=record_quality, cookies=bili_cookie, proxy_addr=proxy_address))

                    elif record_url.find("http://xhslink.com/") > -1 or \
                            record_url.find("https://www.xiaohongshu.com/") > -1:
                        platform = '小红书直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_xhs_stream_url(
                                record_url, proxy_addr=proxy_address, cookies=xhs_cookie))
                            retry += 1

                    elif record_url.find("www.bigo.tv/") > -1 or record_url.find("slink.bigovideo.tv/") > -1:
                        platform = 'Bigo直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_bigo_stream_url(
                                record_url, proxy_addr=proxy_address, cookies=bigo_cookie))

                    elif record_url.find("https://app.blued.cn/") > -1:
                        platform = 'Blued直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_blued_stream_url(
                                record_url, proxy_addr=proxy_address, cookies=blued_cookie))

                    elif record_url.find("sooplive.co.kr/") > -1 or record_url.find("sooplive.com/") > -1:
                        platform = 'SOOP'
                        with semaphore:
                            # One account cookie is shared by all SOOP recording threads.
                            # Keep request, refresh, and persistence in one critical section
                            # so parallel rooms cannot log in with the same expired cookie.
                            with soop_cookie_lock:
                                if global_proxy or proxy_address:
                                    try:
                                        json_data = asyncio.run(spider.get_sooplive_stream_data(
                                            url=record_url, proxy_addr=proxy_address,
                                            cookies=sooplive_cookie,
                                            username=sooplive_username,
                                            password=sooplive_password,
                                            preferred_fps=preferred_fps,
                                            requested_quality=record_quality_zh
                                        ))
                                    except Exception as soop_error:
                                        # Persist a login cookie before releasing the shared lock.
                                        # Otherwise another room can enter with the stale cookie
                                        # between this request failing and the outer retry handler.
                                        refreshed_cookie = getattr(soop_error, 'new_cookies', None)
                                        if refreshed_cookie:
                                            sooplive_cookie = refreshed_cookie
                                            try:
                                                utils.update_config(
                                                    config_file, 'Cookie', 'sooplive_cookie', sooplive_cookie
                                                )
                                            except Exception as cookie_error:
                                                logger.error(f"保存 SOOP 更新後 Cookie 失敗: {cookie_error}")
                                        raise
                                    if json_data and json_data.get('new_cookies'):
                                        sooplive_cookie = json_data['new_cookies']
                                        utils.update_config(
                                            config_file, 'Cookie', 'sooplive_cookie', sooplive_cookie
                                        )
                                    port_info = asyncio.run(stream.get_stream_url(json_data, record_quality, spec=True))
                                else:
                                    logger.error("错误信息: 网络异常，请检查本网络是否能正常访问SOOP平台")

                    elif record_url.find("cc.163.com/") > -1:
                        platform = '网易CC直播'
                        with semaphore:
                            json_data = asyncio.run(spider.get_netease_stream_data(
                                url=record_url, cookies=netease_cookie))
                            port_info = asyncio.run(stream.get_netease_stream_url(json_data, record_quality))

                    elif record_url.find("qiandurebo.com/") > -1:
                        platform = '千度热播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_qiandurebo_stream_data(
                                url=record_url, proxy_addr=proxy_address, cookies=qiandurebo_cookie))

                    elif record_url.find("www.pandalive.co.kr/") > -1:
                        platform = 'PandaTV'
                        with semaphore:
                            if global_proxy or proxy_address:
                                json_data = asyncio.run(spider.get_pandatv_stream_data(
                                    url=record_url,
                                    proxy_addr=proxy_address,
                                    cookies=pandatv_cookie
                                ))
                                port_info = asyncio.run(stream.get_stream_url(json_data, record_quality, spec=True))
                            else:
                                logger.error("错误信息: 网络异常，请检查本网络是否能正常访问PandaTV直播平台")

                    elif record_url.find("fm.missevan.com/") > -1:
                        platform = '猫耳FM直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_maoerfm_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=maoerfm_cookie))

                    elif record_url.find("www.winktv.co.kr/") > -1:
                        platform = 'WinkTV'
                        with semaphore:
                            if global_proxy or proxy_address:
                                json_data = asyncio.run(spider.get_winktv_stream_data(
                                    url=record_url,
                                    proxy_addr=proxy_address,
                                    cookies=winktv_cookie))
                                port_info = asyncio.run(stream.get_stream_url(json_data, record_quality, spec=True))
                            else:
                                logger.error("错误信息: 网络异常，请检查本网络是否能正常访问WinkTV直播平台")

                    elif record_url.find("www.flextv.co.kr/") > -1 or record_url.find("www.ttinglive.com/") > -1:
                        platform = 'FlexTV'
                        with semaphore:
                            if global_proxy or proxy_address:
                                json_data = asyncio.run(spider.get_flextv_stream_data(
                                    url=record_url,
                                    proxy_addr=proxy_address,
                                    cookies=flextv_cookie,
                                    username=flextv_username,
                                    password=flextv_password
                                ))
                                if json_data and json_data.get('new_cookies'):
                                    utils.update_config(
                                        config_file, 'Cookie', 'flextv_cookie', json_data['new_cookies']
                                    )
                                if 'play_url_list' in json_data:
                                    port_info = asyncio.run(stream.get_stream_url(json_data, record_quality, spec=True))
                                else:
                                    port_info = json_data
                            else:
                                logger.error("错误信息: 网络异常，请检查本网络是否能正常访问FlexTV直播平台")

                    elif record_url.find("look.163.com/") > -1:
                        platform = 'Look直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_looklive_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=look_cookie
                            ))

                    elif record_url.find("www.popkontv.com/") > -1:
                        platform = 'PopkonTV'
                        with semaphore:
                            if global_proxy or proxy_address:
                                port_info = asyncio.run(spider.get_popkontv_stream_url(
                                    url=record_url,
                                    proxy_addr=proxy_address,
                                    access_token=popkontv_access_token,
                                    username=popkontv_username,
                                    password=popkontv_password,
                                    partner_code=popkontv_partner_code
                                ))
                                if port_info and port_info.get('new_token'):
                                    utils.update_config(
                                        file_path=config_file, section='Authorization', key='popkontv_token',
                                        new_value=port_info['new_token']
                                    )

                            else:
                                logger.error("错误信息: 网络异常，请检查本网络是否能正常访问PopkonTV直播平台")

                    elif record_url.find("twitcasting.tv/") > -1:
                        platform = 'TwitCasting'
                        with semaphore:
                            json_data = asyncio.run(spider.get_twitcasting_stream_url(
                                url=record_url,
                                proxy_addr=proxy_address,
                                cookies=twitcasting_cookie,
                                account_type=twitcasting_account_type,
                                username=twitcasting_username,
                                password=twitcasting_password
                            ))
                            port_info = asyncio.run(stream.get_stream_url(json_data, record_quality, spec=False))

                            if port_info and port_info.get('new_cookies'):
                                utils.update_config(
                                    file_path=config_file, section='Cookie', key='twitcasting_cookie',
                                    new_value=port_info['new_cookies']
                                )

                    elif record_url.find("live.baidu.com/") > -1:
                        platform = '百度直播'
                        with semaphore:
                            json_data = asyncio.run(spider.get_baidu_stream_data(
                                url=record_url,
                                proxy_addr=proxy_address,
                                cookies=baidu_cookie))
                            port_info = asyncio.run(stream.get_stream_url(json_data, record_quality))

                    elif record_url.find("weibo.com/") > -1:
                        platform = '微博直播'
                        with semaphore:
                            json_data = asyncio.run(spider.get_weibo_stream_data(
                                url=record_url, proxy_addr=proxy_address, cookies=weibo_cookie))
                            port_info = asyncio.run(stream.get_stream_url(
                                json_data, record_quality, hls_extra_key='m3u8_url'))

                    elif record_url.find("kugou.com/") > -1:
                        platform = '酷狗直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_kugou_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=kugou_cookie))

                    elif record_url.find("www.twitch.tv/") > -1:
                        platform = 'TwitchTV'
                        with semaphore:
                            if global_proxy or proxy_address:
                                json_data = asyncio.run(spider.get_twitchtv_stream_data(
                                    url=record_url,
                                    proxy_addr=proxy_address,
                                    cookies=twitch_cookie
                                ))
                                port_info = asyncio.run(stream.get_stream_url(json_data, record_quality, spec=True))
                            else:
                                logger.error("错误信息: 网络异常，请检查本网络是否能正常访问TwitchTV直播平台")

                    elif record_url.find("www.liveme.com/") > -1:
                        if global_proxy or proxy_address:
                            platform = 'LiveMe'
                            with semaphore:
                                port_info = asyncio.run(spider.get_liveme_stream_url(
                                    url=record_url, proxy_addr=proxy_address, cookies=liveme_cookie))
                        else:
                            logger.error("错误信息: 网络异常，请检查本网络是否能正常访问LiveMe直播平台")

                    elif record_url.find("www.huajiao.com/") > -1:
                        platform = '花椒直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_huajiao_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=huajiao_cookie))

                    elif record_url.find("7u66.com/") > -1:
                        platform = '流星直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_liuxing_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=liuxing_cookie))

                    elif record_url.find("showroom-live.com/") > -1:
                        platform = 'ShowRoom'
                        with semaphore:
                            json_data = asyncio.run(spider.get_showroom_stream_data(
                                url=record_url, proxy_addr=proxy_address, cookies=showroom_cookie))
                            port_info = asyncio.run(stream.get_stream_url(json_data, record_quality, spec=True))

                    elif record_url.find("live.acfun.cn/") > -1 or record_url.find("m.acfun.cn/") > -1:
                        platform = 'Acfun'
                        with semaphore:
                            json_data = asyncio.run(spider.get_acfun_stream_data(
                                url=record_url, proxy_addr=proxy_address, cookies=acfun_cookie))
                            port_info = asyncio.run(stream.get_stream_url(
                                json_data, record_quality, url_type='flv', flv_extra_key='url'))

                    elif record_url.find("live.tlclw.com/") > -1:
                        platform = '畅聊直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_changliao_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=changliao_cookie))

                    elif record_url.find("ybw1666.com/") > -1:
                        platform = '音播直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_yinbo_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=yinbo_cookie))

                    elif record_url.find("www.inke.cn/") > -1:
                        platform = '映客直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_yingke_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=yingke_cookie))

                    elif record_url.find("www.zhihu.com/") > -1:
                        platform = '知乎直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_zhihu_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=zhihu_cookie))

                    elif record_url.find("chzzk.naver.com/") > -1:
                        platform = 'CHZZK'
                        with semaphore:
                            json_data = asyncio.run(spider.get_chzzk_stream_data(
                                url=record_url, proxy_addr=proxy_address, cookies=chzzk_cookie))
                            port_info = asyncio.run(stream.get_stream_url(json_data, record_quality, spec=True))

                    elif record_url.find("www.haixiutv.com/") > -1:
                        platform = '嗨秀直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_haixiu_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=haixiu_cookie))

                    elif record_url.find("vvxqiu.com/") > -1:
                        platform = 'VV星球'
                        with semaphore:
                            port_info = asyncio.run(spider.get_vvxqiu_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=vvxqiu_cookie))

                    elif record_url.find("17.live/") > -1:
                        platform = '17Live'
                        with semaphore:
                            port_info = asyncio.run(spider.get_17live_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=yiqilive_cookie))

                    elif record_url.find("www.lang.live/") > -1:
                        platform = '浪Live'
                        with semaphore:
                            port_info = asyncio.run(spider.get_langlive_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=langlive_cookie))

                    elif record_url.find("m.pp.weimipopo.com/") > -1:
                        platform = '漂漂直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_pplive_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=pplive_cookie))

                    elif record_url.find(".6.cn/") > -1:
                        platform = '六间房直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_6room_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=six_room_cookie))

                    elif record_url.find("lehaitv.com/") > -1:
                        platform = '乐嗨直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_haixiu_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=lehaitv_cookie))

                    elif record_url.find("h.catshow168.com/") > -1:
                        platform = '花猫直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_pplive_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=huamao_cookie))

                    elif record_url.find("live.shopee") > -1 or record_url.find("shp.ee/") > -1:
                        platform = 'shopee'
                        with semaphore:
                            port_info = asyncio.run(spider.get_shopee_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=shopee_cookie))
                            if port_info.get('uid'):
                                new_record_url = record_url.split('?')[0] + '?' + str(port_info['uid'])

                    elif record_url.find("www.youtube.com/") > -1 or record_url.find("youtu.be/") > -1:
                        platform = 'Youtube'
                        with semaphore:
                            json_data = asyncio.run(spider.get_youtube_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=youtube_cookie))
                            port_info = asyncio.run(stream.get_stream_url(json_data, record_quality, spec=True))

                    elif record_url.find("tb.cn") > -1:
                        platform = '淘宝直播'
                        with semaphore:
                            json_data = asyncio.run(spider.get_taobao_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=taobao_cookie))
                            port_info = asyncio.run(stream.get_stream_url(
                                json_data, record_quality,
                                url_type='all', hls_extra_key='hlsUrl', flv_extra_key='flvUrl'
                            ))

                    elif record_url.find("3.cn") > -1 or record_url.find("m.jd.com") > -1:
                        platform = '京东直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_jd_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=jd_cookie))

                    elif record_url.find("faceit.com/") > -1:
                        platform = 'faceit'
                        with semaphore:
                            if global_proxy or proxy_address:
                                with semaphore:
                                    json_data = asyncio.run(spider.get_faceit_stream_data(
                                        url=record_url, proxy_addr=proxy_address, cookies=faceit_cookie))
                                    port_info = asyncio.run(stream.get_stream_url(json_data, record_quality, spec=True))
                            else:
                                logger.error("错误信息: 网络异常，请检查本网络是否能正常访问faceit直播平台")

                    elif record_url.find("www.miguvideo.com") > -1 or record_url.find("m.miguvideo.com") > -1:
                        platform = '咪咕直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_migu_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=migu_cookie))

                    elif record_url.find("show.lailianjie.com") > -1:
                        platform = '连接直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_lianjie_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=lianjie_cookie))

                    elif record_url.find("www.imkktv.com") > -1:
                        platform = '来秀直播'
                        with semaphore:
                            port_info = asyncio.run(spider.get_laixiu_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=laixiu_cookie))

                    elif record_url.find("www.picarto.tv") > -1:
                        platform = 'Picarto'
                        with semaphore:
                            port_info = asyncio.run(spider.get_picarto_stream_url(
                                url=record_url, proxy_addr=proxy_address, cookies=picarto_cookie))

                    elif record_url.find(".m3u8") > -1 or record_url.find(".flv") > -1:
                        platform = '自定义录制直播'
                        port_info = {
                            "anchor_name": platform + '_' + str(uuid.uuid4())[:8],
                            "is_live": True,
                            "record_url": record_url,
                        }
                        if '.flv' in record_url:
                            port_info['flv_url'] = record_url
                        else:
                            port_info['m3u8_url'] = record_url

                    else:
                        logger.error(f'{record_url} {platform}直播地址')
                        return

                    if anchor_name:
                        if '主播:' in anchor_name:
                            anchor_split: list = anchor_name.split('主播:')
                            if len(anchor_split) > 1 and anchor_split[1].strip():
                                anchor_name = anchor_split[1].strip()
                            else:
                                anchor_name = port_info.get("anchor_name", '')
                    else:
                        anchor_name = port_info.get("anchor_name", '')

                    if not port_info.get("anchor_name", ''):
                        print(f'序号{count_variable} 网址内容获取失败,进行重试中...获取失败的地址是:{url_data}')
                        with max_request_lock:
                            error_count += 1
                            error_window.append(1)
                    else:
                        anchor_name = clean_name(anchor_name)
                        record_name = f'序号{count_variable} {anchor_name}'

                        if record_url in url_comments:
                            print(f"[{anchor_name}]已被注释,本条线程将会退出")
                            clear_record_info(record_name, record_url)
                            return

                        if not url_data[-1] and run_once is False:
                            if new_record_url:
                                need_update_line_list.append(
                                    f'{record_url}|{new_record_url},主播: {anchor_name.strip()}')
                                not_record_list.append(new_record_url)
                            else:
                                need_update_line_list.append(f'{record_url}|{record_url},主播: {anchor_name.strip()}')
                            run_once = True

                        push_at = datetime.datetime.today().strftime('%Y-%m-%d %H:%M:%S')
                        if port_info['is_live'] is False:
                            print(f"\r{record_name} 等待直播... ")

                            if start_pushed:
                                if over_show_push:
                                    push_content = "直播间状态更新：[直播间名称] 直播已结束！时间：[时间]"
                                    if over_push_message_text:
                                        push_content = over_push_message_text

                                    push_content = (push_content.replace('[直播间名称]', record_name).
                                                    replace('[时间]', push_at))
                                    threading.Thread(
                                        target=push_message,
                                        args=(record_name, record_url, push_content.replace(r'\n', '\n')),
                                        daemon=True
                                    ).start()
                                start_pushed = False

                        else:
                            content = f"\r{record_name} 正在直播中..."
                            print(content)

                            if live_status_push and not start_pushed:
                                if begin_show_push:
                                    push_content = "直播间状态更新：[直播间名称] 正在直播中，时间：[时间]"
                                    if begin_push_message_text:
                                        push_content = begin_push_message_text

                                    push_content = (push_content.replace('[直播间名称]', record_name).
                                                    replace('[时间]', push_at))
                                    threading.Thread(
                                        target=push_message,
                                        args=(record_name, record_url, push_content.replace(r'\n', '\n')),
                                        daemon=True
                                    ).start()
                                start_pushed = True

                            if disable_record:
                                time.sleep(push_check_seconds)
                                continue

                            real_url = select_source_url(record_url, port_info)
                            full_path = f'{default_path}/{platform}'
                            if real_url:
                                now = datetime.datetime.today().strftime("%Y-%m-%d_%H-%M-%S")
                                live_title = port_info.get('title')
                                title_in_name = ''
                                if live_title:
                                    live_title = clean_name(live_title)
                                    title_in_name = live_title + '_' if filename_by_title else ''

                                try:
                                    if len(video_save_path) > 0:
                                        if not video_save_path.endswith(('/', '\\')):
                                            full_path = f'{video_save_path}/{platform}'
                                        else:
                                            full_path = f'{video_save_path}{platform}'

                                    full_path = full_path.replace("\\", '/')
                                    if folder_by_author:
                                        full_path = f'{full_path}/{anchor_name}'
                                    if folder_by_time:
                                        full_path = f'{full_path}/{now[:10]}'
                                    if folder_by_title and port_info.get('title'):
                                        if folder_by_time:
                                            full_path = f'{full_path}/{live_title}_{anchor_name}'
                                        else:
                                            full_path = f'{full_path}/{now[:10]}_{live_title}'
                                    if not os.path.exists(full_path):
                                        os.makedirs(full_path)
                                except Exception as e:
                                    logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")

                                if platform != '自定义录制直播':
                                    if enable_https_recording and real_url.startswith("http://"):
                                        real_url = real_url.replace("http://", "https://")

                                    http_record_list = ['shopee', "migu"]
                                    if platform in http_record_list:
                                        real_url = real_url.replace("https://", "http://")

                                user_agent = ("Mozilla/5.0 (Linux; Android 11; SAMSUNG SM-G973U) AppleWebKit/537.36 ("
                                              "KHTML, like Gecko) SamsungBrowser/14.2 Chrome/87.0.4280.141 Mobile "
                                              "Safari/537.36")

                                rw_timeout = "15000000"
                                analyzeduration = "20000000"
                                probesize = "10000000"
                                bufsize = "8000k"
                                max_muxing_queue_size = "1024"
                                for pt_host in overseas_platform_host:
                                    if pt_host in record_url:
                                        rw_timeout = "50000000"
                                        analyzeduration = "40000000"
                                        probesize = "20000000"
                                        bufsize = "15000k"
                                        max_muxing_queue_size = "2048"
                                        break

                                ffmpeg_command = [
                                    'ffmpeg', "-y",
                                    "-v", "verbose",
                                    "-rw_timeout", rw_timeout,
                                    "-loglevel", recording_log_level(platform),
                                    "-hide_banner",
                                    "-user_agent", user_agent,
                                    "-protocol_whitelist", "rtmp,crypto,file,http,https,tcp,tls,udp,rtp,httpproxy",
                                    "-thread_queue_size", "1024",
                                    "-analyzeduration", analyzeduration,
                                    "-probesize", probesize,
                                    *recording_corrupt_packet_options(platform),
                                    *recording_input_options(platform, real_url),
                                    "-bufsize", bufsize,
                                    "-sn", "-dn",
                                    "-reconnect_delay_max", "60",
                                    "-reconnect_streamed", "-reconnect_at_eof",
                                    "-max_muxing_queue_size", max_muxing_queue_size,
                                    "-correct_ts_overflow", "1",
                                    "-avoid_negative_ts", "1"
                                ]

                                headers = get_record_headers(platform, record_url)
                                if headers:
                                    ffmpeg_command.insert(11, "-headers")
                                    ffmpeg_command.insert(12, headers)

                                if proxy_address:
                                    ffmpeg_command.insert(1, "-http_proxy")
                                    ffmpeg_command.insert(2, proxy_address)

                                recording.add(record_name)
                                recording_url_list[record_name] = record_url
                                start_record_time = datetime.datetime.now()
                                recording_time_list[record_name] = [start_record_time, record_quality_zh]
                                publish_recording_state()
                                rec_info = f"\r{anchor_name} 准备开始录制视频: {full_path}"
                                if show_url:
                                    re_plat = ('WinkTV', 'PandaTV', 'ShowRoom', 'CHZZK', 'Youtube')
                                    if platform in re_plat:
                                        logger.info(
                                            f"{platform} | {anchor_name} | 直播源地址: {port_info.get('m3u8_url')}")
                                    else:
                                        logger.info(
                                            f"{platform} | {anchor_name} | 直播源地址: {real_url}")

                                only_flv_record = False
                                only_flv_platform_list = ['shopee', '花椒直播']
                                if platform in only_flv_platform_list:
                                    logger.debug(f"提示: {platform} 将强制使用FLV格式录制")
                                    only_flv_record = True

                                only_audio_record = False
                                only_audio_platform_list = ['猫耳FM直播', 'Look直播']
                                if platform in only_audio_platform_list:
                                    only_audio_record = True

                                record_save_type = video_save_type
                                segment_temp_dir = None
                                segment_final_dir = full_path
                                segment_final_prefix = f"{anchor_name}_{title_in_name}"

                                if is_flv_preferred_platform(record_url) and port_info.get('flv_url'):
                                    codec = utils.get_query_params(port_info['flv_url'], "codec")
                                    if codec and codec[0] == 'h265':
                                        logger.warning("FLV is not supported for h265 codec, use TS format instead")
                                        record_save_type = "TS"

                                if only_audio_record or any(i in record_save_type for i in ['MP3', 'M4A']):
                                    try:
                                        now = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())
                                        extension = "mp3" if "m4a" not in record_save_type.lower() else "m4a"
                                        if split_video_by_time:
                                            segment_temp_dir = create_segment_temp_dir()
                                            save_file_path = os.path.join(
                                                segment_temp_dir,
                                                f"segment_%Y-%m-%d_%H-%M-%S.{extension}"
                                            )
                                        else:
                                            save_file_path = (
                                                f"{full_path}/{anchor_name}_{title_in_name}{now}.{extension}"
                                            )

                                        if split_video_by_time:
                                            print(f'\r{anchor_name} 准备开始录制音频: {save_file_path}')

                                            if "MP3" in record_save_type:
                                                command = [
                                                    "-map", "0:a",
                                                    "-c:a", "libmp3lame",
                                                    "-ab", "320k",
                                                    "-f", "segment",
                                                    "-strftime", "1",
                                                    "-segment_time", split_time,
                                                    "-reset_timestamps", "1",
                                                    save_file_path,
                                                ]
                                            else:
                                                command = [
                                                    "-map", "0:a",
                                                    "-c:a", "aac",
                                                    "-bsf:a", "aac_adtstoasc",
                                                    "-ab", "320k",
                                                    "-f", "segment",
                                                    "-strftime", "1",
                                                    "-segment_time", split_time,
                                                    "-segment_format", 'mpegts',
                                                    "-reset_timestamps", "1",
                                                    save_file_path,
                                                ]

                                        else:
                                            if "MP3" in record_save_type:
                                                command = [
                                                    "-map", "0:a",
                                                    "-c:a", "libmp3lame",
                                                    "-ab", "320k",
                                                    save_file_path,
                                                ]

                                            else:
                                                command = [
                                                    "-map", "0:a",
                                                    "-c:a", "aac",
                                                    "-bsf:a", "aac_adtstoasc",
                                                    "-ab", "320k",
                                                    "-movflags", "+faststart",
                                                    save_file_path,
                                                ]

                                        ffmpeg_command.extend(command)
                                        comment_end = check_subprocess(
                                            record_name,
                                            record_url,
                                            ffmpeg_command,
                                            record_save_type,
                                            custom_script,
                                            segment_temp_dir=segment_temp_dir,
                                            segment_final_dir=segment_final_dir,
                                            segment_final_prefix=segment_final_prefix
                                        )
                                        if comment_end:
                                            return

                                    except subprocess.CalledProcessError as e:
                                        logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")
                                        with max_request_lock:
                                            error_count += 1
                                            error_window.append(1)

                                if only_flv_record:
                                    logger.info(f"Use Direct Downloader to Download FLV Stream: {record_url}")
                                    filename = anchor_name + f'_{title_in_name}' + now + '.flv'
                                    save_file_path = f'{full_path}/{filename}'
                                    print(f'{rec_info}/{filename}')

                                    subs_file_path = save_file_path.rsplit('.', maxsplit=1)[0]
                                    subs_thread_name = f'subs_{Path(subs_file_path).name}'
                                    if create_time_file:
                                        create_var[subs_thread_name] = threading.Thread(
                                            target=generate_subtitles, args=(record_name, subs_file_path)
                                        )
                                        create_var[subs_thread_name].daemon = True
                                        create_var[subs_thread_name].start()

                                    try:
                                        flv_url = port_info.get('flv_url')
                                        if flv_url:
                                            recording.add(record_name)
                                            recording_url_list[record_name] = record_url
                                            start_record_time = datetime.datetime.now()
                                            recording_time_list[record_name] = [start_record_time, record_quality_zh]
                                            publish_recording_state()

                                            download_success = direct_download_stream(
                                                flv_url, save_file_path, record_name, record_url, platform
                                            )

                                            if download_success:
                                                record_finished = True
                                                print(
                                                    f"\n{anchor_name} {time.strftime('%Y-%m-%d %H:%M:%S')} 直播录制完成\n")

                                            recording.discard(record_name)
                                            recording_url_list.pop(record_name, None)
                                            publish_recording_state()
                                        else:
                                            logger.debug("未找到FLV直播流，跳过录制")
                                    except Exception as e:
                                        clear_record_info(record_name, record_url)
                                        color_obj.print_colored(
                                            f"\n{anchor_name} {time.strftime('%Y-%m-%d %H:%M:%S')} 直播录制出错,请检查网络\n",
                                            color_obj.RED)
                                        logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")
                                        with max_request_lock:
                                            error_count += 1
                                            error_window.append(1)

                                elif record_save_type == "FLV":
                                    filename = anchor_name + f'_{title_in_name}' + now + ".flv"
                                    print(f'{rec_info}/{filename}')
                                    save_file_path = full_path + '/' + filename

                                    try:
                                        if split_video_by_time:
                                            now = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())
                                            segment_temp_dir = create_segment_temp_dir()
                                            save_file_path = os.path.join(
                                                segment_temp_dir,
                                                "segment_%Y-%m-%d_%H-%M-%S.flv"
                                            )
                                            command = [
                                                "-map", "0",
                                                "-c:v", "copy",
                                                "-c:a", "copy",
                                                "-bsf:a", "aac_adtstoasc",
                                                "-f", "segment",
                                                "-strftime", "1",
                                                "-segment_time", split_time,
                                                "-segment_format", "flv",
                                                "-reset_timestamps", "1",
                                                save_file_path
                                            ]

                                        else:
                                            command = [
                                                "-map", "0",
                                                "-c:v", "copy",
                                                "-c:a", "copy",
                                                "-bsf:a", "aac_adtstoasc",
                                                "-f", "flv",
                                                "{path}".format(path=save_file_path),
                                            ]
                                        ffmpeg_command.extend(command)

                                        comment_end = check_subprocess(
                                            record_name,
                                            record_url,
                                            ffmpeg_command,
                                            record_save_type,
                                            custom_script,
                                            segment_temp_dir=segment_temp_dir,
                                            segment_final_dir=segment_final_dir,
                                            segment_final_prefix=segment_final_prefix
                                        )
                                        if comment_end:
                                            return

                                    except subprocess.CalledProcessError as e:
                                        logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")
                                        with max_request_lock:
                                            error_count += 1
                                            error_window.append(1)

                                    try:
                                        if converts_to_mp4:
                                            seg_file_path = f"{full_path}/{anchor_name}_{title_in_name}{now}_%03d.mp4"
                                            if split_video_by_time:
                                                segment_video(
                                                    save_file_path, seg_file_path,
                                                    segment_format='mp4', segment_time=split_time,
                                                    is_original_delete=delete_origin_file
                                                )
                                            else:
                                                threading.Thread(
                                                    target=converts_mp4,
                                                    args=(save_file_path, delete_origin_file)
                                                ).start()

                                        else:
                                            seg_file_path = f"{full_path}/{anchor_name}_{title_in_name}{now}_%03d.flv"
                                            if split_video_by_time:
                                                segment_video(
                                                    save_file_path, seg_file_path,
                                                    segment_format='flv', segment_time=split_time,
                                                    is_original_delete=delete_origin_file
                                                )
                                    except Exception as e:
                                        logger.error(f"转码失败: {e} ")

                                elif record_save_type == "MKV":
                                    filename = anchor_name + f'_{title_in_name}' + now + ".mkv"
                                    print(f'{rec_info}/{filename}')
                                    save_file_path = full_path + '/' + filename

                                    try:
                                        if split_video_by_time:
                                            now = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())
                                            segment_temp_dir = create_segment_temp_dir()
                                            save_file_path = os.path.join(
                                                segment_temp_dir,
                                                "segment_%Y-%m-%d_%H-%M-%S.mkv"
                                            )
                                            command = [
                                                "-flags", "global_header",
                                                "-c:v", "copy",
                                                "-c:a", "aac",
                                                "-map", "0",
                                                "-f", "segment",
                                                "-strftime", "1",
                                                "-segment_time", split_time,
                                                "-segment_format", "matroska",
                                                "-reset_timestamps", "1",
                                                save_file_path,
                                            ]

                                        else:
                                            command = [
                                                "-flags", "global_header",
                                                "-map", "0",
                                                "-c:v", "copy",
                                                "-c:a", "copy",
                                                "-f", "matroska",
                                                "{path}".format(path=save_file_path),
                                            ]
                                        ffmpeg_command.extend(command)

                                        comment_end = check_subprocess(
                                            record_name,
                                            record_url,
                                            ffmpeg_command,
                                            record_save_type,
                                            custom_script,
                                            segment_temp_dir=segment_temp_dir,
                                            segment_final_dir=segment_final_dir,
                                            segment_final_prefix=segment_final_prefix
                                        )
                                        if comment_end:
                                            return

                                    except subprocess.CalledProcessError as e:
                                        logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")
                                        with max_request_lock:
                                            error_count += 1
                                            error_window.append(1)

                                elif record_save_type == "MP4":
                                    filename = anchor_name + f'_{title_in_name}' + now + ".mp4"
                                    print(f'{rec_info}/{filename}')
                                    save_file_path = full_path + '/' + filename

                                    try:
                                        if split_video_by_time:
                                            now = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())
                                            segment_temp_dir = create_segment_temp_dir()
                                            save_file_path = os.path.join(
                                                segment_temp_dir,
                                                "segment_%Y-%m-%d_%H-%M-%S.mp4"
                                            )
                                            command = [
                                                "-c:v", "copy",
                                                "-c:a", "aac",
                                                "-map", "0",
                                                "-f", "segment",
                                                "-strftime", "1",
                                                "-segment_time", split_time,
                                                "-segment_format", "mp4",
                                                "-reset_timestamps", "1",
                                                "-movflags", "+frag_keyframe+empty_moov",
                                                save_file_path,
                                            ]

                                        else:
                                            command = [
                                                "-map", "0",
                                                "-c:v", "copy",
                                                "-c:a", "copy",
                                                "-f", "mp4",
                                                save_file_path,
                                            ]

                                        ffmpeg_command.extend(command)
                                        comment_end = check_subprocess(
                                            record_name,
                                            record_url,
                                            ffmpeg_command,
                                            record_save_type,
                                            custom_script,
                                            segment_temp_dir=segment_temp_dir,
                                            segment_final_dir=segment_final_dir,
                                            segment_final_prefix=segment_final_prefix
                                        )
                                        if comment_end:
                                            return

                                    except subprocess.CalledProcessError as e:
                                        logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")
                                        with max_request_lock:
                                            error_count += 1
                                            error_window.append(1)

                                else:
                                    if split_video_by_time:
                                        now = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())
                                        filename = anchor_name + f'_{title_in_name}' + now + ".ts"
                                        print(f'{rec_info}/{filename}')

                                        try:
                                            segment_temp_dir = create_segment_temp_dir()
                                            save_file_path = os.path.join(
                                                segment_temp_dir,
                                                "segment_%Y-%m-%d_%H-%M-%S.ts"
                                            )
                                            command = [
                                                "-c:v", "copy",
                                                "-c:a", "copy",
                                                "-map", "0",
                                                "-f", "segment",
                                                "-strftime", "1",
                                                "-segment_time", split_time,
                                                "-segment_format", 'mpegts',
                                                "-reset_timestamps", "1",
                                                save_file_path,
                                            ]

                                            ffmpeg_command.extend(command)
                                            comment_end = check_subprocess(
                                                record_name,
                                                record_url,
                                                ffmpeg_command,
                                                record_save_type,
                                                custom_script,
                                                segment_temp_dir=segment_temp_dir,
                                                segment_final_dir=segment_final_dir,
                                                segment_final_prefix=segment_final_prefix
                                            )
                                            if comment_end:
                                                return

                                        except subprocess.CalledProcessError as e:
                                            logger.error(
                                                f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")
                                            with max_request_lock:
                                                error_count += 1
                                                error_window.append(1)

                                    else:
                                        filename = anchor_name + f'_{title_in_name}' + now + ".ts"
                                        print(f'{rec_info}/{filename}')
                                        save_file_path = full_path + '/' + filename

                                        try:
                                            command = [
                                                "-c:v", "copy",
                                                "-c:a", "copy",
                                                "-map", "0",
                                                "-f", "mpegts",
                                                save_file_path,
                                            ]

                                            ffmpeg_command.extend(command)
                                            comment_end = check_subprocess(
                                                record_name,
                                                record_url,
                                                ffmpeg_command,
                                                record_save_type,
                                                custom_script,
                                                segment_temp_dir=segment_temp_dir,
                                                segment_final_dir=segment_final_dir,
                                                segment_final_prefix=segment_final_prefix
                                            )
                                            if comment_end:
                                                # 一般單間停止且沒有啟用自動轉檔時，保留原本的轉檔行為。
                                                # 自動轉檔及停止全部的轉檔已由 check_subprocess 負責。
                                                if not converts_to_mp4 and not is_stop_all_requested():
                                                    converts_mp4(
                                                        save_file_path,
                                                        is_original_delete=delete_origin_file
                                                    )
                                                return

                                        except subprocess.CalledProcessError as e:
                                            logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")
                                            with max_request_lock:
                                                error_count += 1
                                                error_window.append(1)

                                count_time = time.time()

                except Exception as e:
                    refreshed_soop_cookie = getattr(e, 'new_cookies', None)
                    if refreshed_soop_cookie:
                        # Fallback guard for a cookie-bearing exception raised outside
                        # the request block. Avoid rewriting a cookie already persisted
                        # by the inner handler while the SOOP lock was held.
                        with soop_cookie_lock:
                            if refreshed_soop_cookie != sooplive_cookie:
                                sooplive_cookie = refreshed_soop_cookie
                                try:
                                    utils.update_config(
                                        config_file, 'Cookie', 'sooplive_cookie', sooplive_cookie
                                    )
                                except Exception as cookie_error:
                                    logger.error(f"保存 SOOP 更新後 Cookie 失敗: {cookie_error}")
                    logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")
                    with max_request_lock:
                        error_count += 1
                        error_window.append(1)

                num = random.randint(-5, 5) + delay_default
                if num < 0:
                    num = 0
                x = num

                if error_count > 20:
                    x = x + 60
                    color_obj.print_colored("\r瞬时错误太多,延迟加60秒", color_obj.YELLOW)

                # 这里是.如果录制结束后,循环时间会暂时变成30s后检测一遍. 这样一定程度上防止主播卡顿造成少录
                # 当30秒过后检测一遍后. 会回归正常设置的循环秒数
                if record_finished:
                    count_time_end = time.time() - count_time
                    if count_time_end < 60:
                        x = 30
                    record_finished = False

                else:
                    x = num

                # 这里是正常循环
                while x:
                    if is_stop_all_requested():
                        logger.info("收到整體停止要求，中斷直播監測倒數")
                        return

                    x = x - 1

                    if loop_time:
                        print(f'\r{anchor_name}循环等待{x}秒 ', end="")

                    time.sleep(1)
                if loop_time:
                    print('\r检测直播间中...', end="")
        except Exception as e:
            logger.error(f"错误信息: {e} 发生错误的行数: {e.__traceback__.tb_lineno}")
            with max_request_lock:
                error_count += 1
                error_window.append(1)
        for _ in range(2):
            if is_stop_all_requested():
                return
            time.sleep(1)


def backup_file(file_path: str, backup_dir_path: str, limit_counts: int = 6) -> None:
    try:
        if not os.path.exists(backup_dir_path):
            os.makedirs(backup_dir_path)

        timestamp = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        backup_file_name = os.path.basename(file_path) + '_' + timestamp
        backup_file_path = os.path.join(backup_dir_path, backup_file_name).replace("\\", "/")
        shutil.copy2(file_path, backup_file_path)

        files = os.listdir(backup_dir_path)
        _files = [f for f in files if f.startswith(os.path.basename(file_path))]
        _files.sort(key=lambda x: os.path.getmtime(os.path.join(backup_dir_path, x)))

        while len(_files) > limit_counts:
            oldest_file = _files[0]
            os.remove(os.path.join(backup_dir_path, oldest_file))
            _files = _files[1:]

    except Exception as e:
        logger.error(f'\r备份配置文件 {file_path} 失败：{str(e)}')


def backup_file_start() -> None:
    config_md5 = ''
    url_config_md5 = ''

    while True:
        try:
            if os.path.exists(config_file):
                new_config_md5 = utils.check_md5(config_file)
                if new_config_md5 != config_md5:
                    backup_file(config_file, backup_dir)
                    config_md5 = new_config_md5

            if os.path.exists(url_config_file):
                new_url_config_md5 = utils.check_md5(url_config_file)
                if new_url_config_md5 != url_config_md5:
                    backup_file(url_config_file, backup_dir)
                    url_config_md5 = new_url_config_md5
            time.sleep(600)
        except Exception as e:
            logger.error(f"备份配置文件失败, 错误信息: {e}")


def check_ffmpeg_existence() -> bool:
    try:
        result = subprocess.run(['ffmpeg', '-version'], check=True, capture_output=True, text=True)
        if result.returncode == 0:
            lines = result.stdout.splitlines()
            if lines:
                print(lines[0])
            if len(lines) > 1:
                print(lines[1])
    except subprocess.CalledProcessError as e:
        logger.error(e)
    except FileNotFoundError:
        pass

    # Keep the installer check after the direct PATH check. Returning from a
    # finally block could suppress an unexpected exception from subprocess.run.
    if check_ffmpeg():
        time.sleep(1)
        return True
    return False


# --------------------------初始化程序-------------------------------------
print("LiveRecorder")
print("專案網址: https://github.com/hoWAreU/LiveRecorder-GUI-SOOP-FIX-byhow")
print("維護範圍: 本專案僅持續維護 SOOP 相關功能")
print(f"錄製核心: DouyinLiveRecorder {version}")
print("核心來源: https://github.com/ihmily/DouyinLiveRecorder")
if not check_ffmpeg_existence():
    logger.error("缺少ffmpeg无法进行录制，程序退出")
    sys.exit(1)
os.makedirs(os.path.dirname(config_file), exist_ok=True)
t3 = threading.Thread(target=backup_file_start, args=(), daemon=True)
t3.start()
utils.remove_duplicate_lines(url_config_file)


def read_config_value(config_parser: configparser.RawConfigParser, section: str, option: str, default_value: Any) \
        -> Any:
    try:

        config_parser.read(config_file, encoding=text_encoding)
        if '录制设置' not in config_parser.sections():
            config_parser.add_section('录制设置')
        if '推送配置' not in config_parser.sections():
            config_parser.add_section('推送配置')
        if 'Cookie' not in config_parser.sections():
            config_parser.add_section('Cookie')
        if 'Authorization' not in config_parser.sections():
            config_parser.add_section('Authorization')
        if '账号密码' not in config_parser.sections():
            config_parser.add_section('账号密码')
        return config_parser.get(section, option)
    except (configparser.NoSectionError, configparser.NoOptionError):
        config_parser.set(section, option, str(default_value))
        with open(config_file, 'w', encoding=text_encoding) as f:
            config_parser.write(f)
        return default_value


def safe_int_config(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def safe_float_config(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


options = {"是": True, "否": False}
config = configparser.RawConfigParser()
language = read_config_value(config, '录制设置', 'language(zh_cn/en)', "zh_cn")
skip_proxy_check = options.get(read_config_value(config, '录制设置', '是否跳过代理检测(是/否)', "否"), False)
if language and 'en' not in language.lower():
    from i18n import translated_print

    builtins.print = translated_print

try:
    if skip_proxy_check:
        global_proxy = True
    else:
        print('系统代理检测中，请耐心等待...')
        response_g = urllib.request.urlopen("https://www.google.com/", timeout=15)
        global_proxy = True
        print('\r全局/规则网络代理已开启√')
        pd = ProxyDetector()
        if pd.is_proxy_enabled():
            proxy_info = pd.get_proxy_info()
            print("System Proxy: http://{}:{}".format(proxy_info.ip, proxy_info.port))
except HTTPError as err:
    print(f"HTTP error occurred: {err.code} - {err.reason}")
except URLError:
    color_obj.print_colored("INFO：未检测到全局/规则网络代理，请检查代理配置（若无需录制海外直播请忽略此条提示）",
                            color_obj.YELLOW)
except Exception as err:
    print("An unexpected error occurred:", err)

while True:

    # 收到整體停止訊號時，停止新增工作並等待現有工作完成
    if is_stop_all_requested():
        logger.info("收到整體停止要求，準備停止錄製核心")

        for thread_name, worker_thread in list(create_var.items()):
            if (
                thread_name.startswith("thread_")
                and worker_thread is not threading.current_thread()
                and worker_thread.is_alive()
            ):
                logger.info(f"等待錄製工作結束：{thread_name}")
                worker_thread.join()

        logger.info("所有錄製工作已結束，錄製核心準備退出")
        write_stop_all_result()
        break

    try:
        if not os.path.isfile(config_file):
            with open(config_file, 'w', encoding=text_encoding) as file:
                pass


        ini_URL_content = ''
        if os.path.isfile(url_config_file):
            with open(url_config_file, 'r', encoding=text_encoding) as file:
                ini_URL_content = file.read().strip()

        if not ini_URL_content.strip():
            input_url = input_or_stop(
                '请输入要录制的主播直播间网址（尽量使用PC网页端的直播间地址）:\n'
            )

            if input_url is None:
                logger.info("收到停止訊號或輸入結束，停止核心主迴圈")
                break

            with open(url_config_file, 'w', encoding=text_encoding) as file:
                file.write(input_url)
    except OSError as err:
        logger.error(f"发生 I/O 错误: {err}")

    video_save_path = read_config_value(config, '录制设置', '直播保存路径(不填则默认)', "")
    folder_by_author = options.get(read_config_value(config, '录制设置', '保存文件夹是否以作者区分', "是"), False)
    folder_by_time = options.get(read_config_value(config, '录制设置', '保存文件夹是否以时间区分', "否"), False)
    folder_by_title = options.get(read_config_value(config, '录制设置', '保存文件夹是否以标题区分', "否"), False)
    filename_by_title = options.get(read_config_value(config, '录制设置', '保存文件名是否包含标题', "否"), False)
    clean_emoji = options.get(read_config_value(config, '录制设置', '是否去除名称中的表情符号', "是"), True)
    video_save_type = read_config_value(config, '录制设置', '视频保存格式ts|mkv|flv|mp4|mp3音频|m4a音频', "ts")
    video_record_quality = read_config_value(config, '录制设置', '原画|超清|高清|标清|流畅', "原画")
    preferred_fps = read_config_value(config, '录制设置', '偏好帧率(自动|30|60)', "自动")
    use_proxy = options.get(read_config_value(config, '录制设置', '是否使用代理ip(是/否)', "是"), False)
    proxy_addr_bak = read_config_value(config, '录制设置', '代理地址', "")
    proxy_addr = None if not use_proxy else proxy_addr_bak
    max_request = safe_int_config(read_config_value(config, '录制设置', '同一时间访问网络的线程数', 3), 3)
    semaphore = threading.Semaphore(max_request)
    delay_default = safe_int_config(read_config_value(config, '录制设置', '循环时间(秒)', 120), 120)
    local_delay_default = safe_int_config(read_config_value(config, '录制设置', '排队读取网址时间(秒)', 0), 0)
    loop_time = options.get(read_config_value(config, '录制设置', '是否显示循环秒数', "否"), False)
    show_url = options.get(read_config_value(config, '录制设置', '是否显示直播源地址', "否"), False)
    split_video_by_time = options.get(read_config_value(config, '录制设置', '分段录制是否开启', "否"), False)
    enable_https_recording = options.get(read_config_value(config, '录制设置', '是否强制启用https录制', "否"), False)
    disk_space_limit = safe_float_config(read_config_value(config, '录制设置', '录制空间剩余阈值(gb)', 1.0), 1.0)
    split_time = str(read_config_value(config, '录制设置', '视频分段时间(秒)', 1800))
    converts_to_mp4 = options.get(read_config_value(config, '录制设置', '录制完成后自动转为mp4格式', "否"), False)
    converts_to_h264 = options.get(read_config_value(config, '录制设置', 'mp4格式重新编码为h264', "否"), False)
    delete_origin_file = options.get(read_config_value(config, '录制设置', '追加格式后删除原文件', "否"), False)
    create_time_file = options.get(read_config_value(config, '录制设置', '生成时间字幕文件', "否"), False)
    is_run_script = options.get(read_config_value(config, '录制设置', '是否录制完成后执行自定义脚本', "否"), False)
    custom_script = read_config_value(config, '录制设置', '自定义脚本执行命令', "") if is_run_script else None
    enable_proxy_platform = read_config_value(
        config, '录制设置', '使用代理录制的平台(逗号分隔)',
        'tiktok, soop, pandalive, winktv, flextv, popkontv, twitch, liveme, showroom, chzzk, shopee, shp, youtu, faceit'
    )
    enable_proxy_platform_list = enable_proxy_platform.replace('，', ',').split(',') if enable_proxy_platform else None
    extra_enable_proxy = read_config_value(config, '录制设置', '额外使用代理录制的平台(逗号分隔)', '')
    extra_enable_proxy_platform_list = extra_enable_proxy.replace('，', ',').split(',') if extra_enable_proxy else None
    live_status_push = read_config_value(config, '推送配置', '直播状态推送渠道', "")
    dingtalk_api_url = read_config_value(config, '推送配置', '钉钉推送接口链接', "")
    xizhi_api_url = read_config_value(config, '推送配置', '微信推送接口链接', "")
    bark_msg_api = read_config_value(config, '推送配置', 'bark推送接口链接', "")
    bark_msg_level = read_config_value(config, '推送配置', 'bark推送中断级别', "active")
    bark_msg_ring = read_config_value(config, '推送配置', 'bark推送铃声', "bell")
    dingtalk_phone_num = read_config_value(config, '推送配置', '钉钉通知@对象(填手机号)', "")
    dingtalk_is_atall = options.get(read_config_value(config, '推送配置', '钉钉通知@全体(是/否)', "否"), False)
    tg_token = read_config_value(config, '推送配置', 'tgapi令牌', "")
    tg_chat_id = read_config_value(config, '推送配置', 'tg聊天id(个人或者群组id)', "")
    email_host = read_config_value(config, '推送配置', 'SMTP邮件服务器', "")
    open_smtp_ssl = options.get(read_config_value(config, '推送配置', '是否使用SMTP服务SSL加密(是/否)', "是"), True)
    smtp_port = read_config_value(config, '推送配置', 'SMTP邮件服务器端口', "")
    login_email = read_config_value(config, '推送配置', '邮箱登录账号', "")
    email_password = read_config_value(config, '推送配置', '发件人密码(授权码)', "")
    sender_email = read_config_value(config, '推送配置', '发件人邮箱', "")
    sender_name = read_config_value(config, '推送配置', '发件人显示昵称', "")
    to_email = read_config_value(config, '推送配置', '收件人邮箱', "")
    ntfy_api = read_config_value(config, '推送配置', 'ntfy推送地址', "")
    ntfy_tags = read_config_value(config, '推送配置', 'ntfy推送标签', "tada")
    ntfy_email = read_config_value(config, '推送配置', 'ntfy推送邮箱', "")
    pushplus_token = read_config_value(config, '推送配置', 'pushplus推送token', "")
    push_message_title = read_config_value(config, '推送配置', '自定义推送标题', "直播间状态更新通知")
    begin_push_message_text = read_config_value(config, '推送配置', '自定义开播推送内容', "")
    over_push_message_text = read_config_value(config, '推送配置', '自定义关播推送内容', "")
    disable_record = options.get(read_config_value(config, '推送配置', '只推送通知不录制(是/否)', "否"), False)
    push_check_seconds = safe_int_config(
        read_config_value(config, '推送配置', '直播推送检测频率(秒)', 1800), 1800
    )
    begin_show_push = options.get(read_config_value(config, '推送配置', '开播推送开启(是/否)', "是"), True)
    over_show_push = options.get(read_config_value(config, '推送配置', '关播推送开启(是/否)', "否"), False)
    sooplive_username = read_config_value(config, '账号密码', 'sooplive账号', '')
    sooplive_password = read_config_value(config, '账号密码', 'sooplive密码', '')
    flextv_username = read_config_value(config, '账号密码', 'flextv账号', '')
    flextv_password = read_config_value(config, '账号密码', 'flextv密码', '')
    popkontv_username = read_config_value(config, '账号密码', 'popkontv账号', '')
    popkontv_partner_code = read_config_value(config, '账号密码', 'partner_code', 'P-00001')
    popkontv_password = read_config_value(config, '账号密码', 'popkontv密码', '')
    twitcasting_account_type = read_config_value(config, '账号密码', 'twitcasting账号类型', 'normal')
    twitcasting_username = read_config_value(config, '账号密码', 'twitcasting账号', '')
    twitcasting_password = read_config_value(config, '账号密码', 'twitcasting密码', '')
    popkontv_access_token = read_config_value(config, 'Authorization', 'popkontv_token', '')
    dy_cookie = read_config_value(config, 'Cookie', '抖音cookie', '')
    ks_cookie = read_config_value(config, 'Cookie', '快手cookie', '')
    tiktok_cookie = read_config_value(config, 'Cookie', 'tiktok_cookie', '')
    hy_cookie = read_config_value(config, 'Cookie', '虎牙cookie', '')
    douyu_cookie = read_config_value(config, 'Cookie', '斗鱼cookie', '')
    yy_cookie = read_config_value(config, 'Cookie', 'yy_cookie', '')
    bili_cookie = read_config_value(config, 'Cookie', 'B站cookie', '')
    xhs_cookie = read_config_value(config, 'Cookie', '小红书cookie', '')
    bigo_cookie = read_config_value(config, 'Cookie', 'bigo_cookie', '')
    blued_cookie = read_config_value(config, 'Cookie', 'blued_cookie', '')
    sooplive_cookie = read_config_value(config, 'Cookie', 'sooplive_cookie', '')
    netease_cookie = read_config_value(config, 'Cookie', 'netease_cookie', '')
    qiandurebo_cookie = read_config_value(config, 'Cookie', '千度热播_cookie', '')
    pandatv_cookie = read_config_value(config, 'Cookie', 'pandatv_cookie', '')
    maoerfm_cookie = read_config_value(config, 'Cookie', '猫耳fm_cookie', '')
    winktv_cookie = read_config_value(config, 'Cookie', 'winktv_cookie', '')
    flextv_cookie = read_config_value(config, 'Cookie', 'flextv_cookie', '')
    look_cookie = read_config_value(config, 'Cookie', 'look_cookie', '')
    twitcasting_cookie = read_config_value(config, 'Cookie', 'twitcasting_cookie', '')
    baidu_cookie = read_config_value(config, 'Cookie', 'baidu_cookie', '')
    weibo_cookie = read_config_value(config, 'Cookie', 'weibo_cookie', '')
    kugou_cookie = read_config_value(config, 'Cookie', 'kugou_cookie', '')
    twitch_cookie = read_config_value(config, 'Cookie', 'twitch_cookie', '')
    liveme_cookie = read_config_value(config, 'Cookie', 'liveme_cookie', '')
    huajiao_cookie = read_config_value(config, 'Cookie', 'huajiao_cookie', '')
    liuxing_cookie = read_config_value(config, 'Cookie', 'liuxing_cookie', '')
    showroom_cookie = read_config_value(config, 'Cookie', 'showroom_cookie', '')
    acfun_cookie = read_config_value(config, 'Cookie', 'acfun_cookie', '')
    changliao_cookie = read_config_value(config, 'Cookie', 'changliao_cookie', '')
    yinbo_cookie = read_config_value(config, 'Cookie', 'yinbo_cookie', '')
    yingke_cookie = read_config_value(config, 'Cookie', 'yingke_cookie', '')
    zhihu_cookie = read_config_value(config, 'Cookie', 'zhihu_cookie', '')
    chzzk_cookie = read_config_value(config, 'Cookie', 'chzzk_cookie', '')
    haixiu_cookie = read_config_value(config, 'Cookie', 'haixiu_cookie', '')
    vvxqiu_cookie = read_config_value(config, 'Cookie', 'vvxqiu_cookie', '')
    yiqilive_cookie = read_config_value(config, 'Cookie', '17live_cookie', '')
    langlive_cookie = read_config_value(config, 'Cookie', 'langlive_cookie', '')
    pplive_cookie = read_config_value(config, 'Cookie', 'pplive_cookie', '')
    six_room_cookie = read_config_value(config, 'Cookie', '6room_cookie', '')
    lehaitv_cookie = read_config_value(config, 'Cookie', 'lehaitv_cookie', '')
    huamao_cookie = read_config_value(config, 'Cookie', 'huamao_cookie', '')
    shopee_cookie = read_config_value(config, 'Cookie', 'shopee_cookie', '')
    youtube_cookie = read_config_value(config, 'Cookie', 'youtube_cookie', '')
    taobao_cookie = read_config_value(config, 'Cookie', 'taobao_cookie', '')
    jd_cookie = read_config_value(config, 'Cookie', 'jd_cookie', '')
    faceit_cookie = read_config_value(config, 'Cookie', 'faceit_cookie', '')
    migu_cookie = read_config_value(config, 'Cookie', 'migu_cookie', '')
    lianjie_cookie = read_config_value(config, 'Cookie', 'lianjie_cookie', '')
    laixiu_cookie = read_config_value(config, 'Cookie', 'laixiu_cookie', '')
    picarto_cookie = read_config_value(config, 'Cookie', 'picarto_cookie', '')

    video_save_type_list = ("FLV", "MKV", "TS", "MP4", "MP3音频", "M4A音频", "MP3", "M4A")
    if video_save_type and video_save_type.upper() in video_save_type_list:
        video_save_type = video_save_type.upper()
    else:
        video_save_type = "TS"

    check_path = video_save_path or default_path
    if utils.check_disk_capacity(check_path, show=first_run) < disk_space_limit:
        exit_recording = True
        if not recording:
            logger.warning(f"Disk space remaining is below {disk_space_limit} GB. "
                           f"Exiting program due to the disk space limit being reached.")
            sys.exit(-1)


    def contains_url(string: str) -> bool:
        pattern = r"(https?://)?(www\.)?[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)+(:\d+)?(/.*)?"
        return re.search(pattern, string) is not None


    try:
        url_comments, line_list, url_line_list = [[] for _ in range(3)]
        with (open(url_config_file, "r", encoding=text_encoding, errors='ignore') as file):
            for origin_line in file:
                if origin_line in line_list:
                    delete_line(url_config_file, origin_line)
                line_list.append(origin_line)
                line = origin_line.strip()
                if len(line) < 18:
                    continue

                line_spilt = line.split('主播: ')
                if len(line_spilt) > 2:
                    line = update_file(url_config_file, line, f'{line_spilt[0]}主播: {line_spilt[-1]}')

                is_comment_line = line.startswith("#")
                if is_comment_line:
                    line = line.lstrip('#')

                if re.search('[,，]', line):
                    split_line = re.split('[,，]', line)
                else:
                    split_line = [line, '']

                if len(split_line) == 1:
                    url = split_line[0]
                    quality, name = [video_record_quality, '']
                elif len(split_line) == 2:
                    if contains_url(split_line[0]):
                        quality = video_record_quality
                        url, name = split_line
                    else:
                        quality, url = split_line
                        name = ''
                else:
                    quality, url, name = split_line

                if quality not in ("原画", "蓝光", "超清", "高清", "标清", "流畅", "2K", "1080P", "720P", "540P", "360P", "240P"):
                    quality = '原画'

                if url not in url_line_list:
                    url_line_list.append(url)
                else:
                    delete_line(url_config_file, origin_line)

                url = 'https://' + url if '://' not in url else url
                url_host = url.split('/')[2]

                platform_host = [
                    'live.douyin.com',
                    'v.douyin.com',
                    'www.douyin.com',
                    'live.kuaishou.com',
                    'www.huya.com',
                    'www.douyu.com',
                    'www.yy.com',
                    'live.bilibili.com',
                    'www.redelight.cn',
                    'www.xiaohongshu.com',
                    'xhslink.com',
                    'www.bigo.tv',
                    'slink.bigovideo.tv',
                    'app.blued.cn',
                    'cc.163.com',
                    'qiandurebo.com',
                    'fm.missevan.com',
                    'look.163.com',
                    'twitcasting.tv',
                    'live.baidu.com',
                    'weibo.com',
                    'fanxing.kugou.com',
                    'fanxing2.kugou.com',
                    'mfanxing.kugou.com',
                    'www.huajiao.com',
                    'www.7u66.com',
                    'wap.7u66.com',
                    'live.acfun.cn',
                    'm.acfun.cn',
                    'live.tlclw.com',
                    'wap.tlclw.com',
                    'live.ybw1666.com',
                    'wap.ybw1666.com',
                    'www.inke.cn',
                    'www.zhihu.com',
                    'www.haixiutv.com',
                    "h5webcdnp.vvxqiu.com",
                    "17.live",
                    'www.lang.live',
                    "m.pp.weimipopo.com",
                    "v.6.cn",
                    "m.6.cn",
                    'www.lehaitv.com',
                    'h.catshow168.com',
                    'e.tb.cn',
                    'huodong.m.taobao.com',
                    '3.cn',
                    'eco.m.jd.com',
                    'www.miguvideo.com',
                    'm.miguvideo.com',
                    'show.lailianjie.com',
                    'www.imkktv.com',
                    'www.picarto.tv'
                ]
                overseas_platform_host = [
                    'www.tiktok.com',
                    'play.sooplive.co.kr',
                    'm.sooplive.co.kr',
                    'play.sooplive.com',
                    'www.sooplive.com',
                    'm.sooplive.com',
                    'www.pandalive.co.kr',
                    'www.winktv.co.kr',
                    'www.flextv.co.kr',
                    'www.ttinglive.com',
                    'www.popkontv.com',
                    'www.twitch.tv',
                    'www.liveme.com',
                    'www.showroom-live.com',
                    'chzzk.naver.com',
                    'm.chzzk.naver.com',
                    'live.shopee.',
                    '.shp.ee',
                    'www.youtube.com',
                    'youtu.be',
                    'www.faceit.com'
                ]

                platform_host.extend(overseas_platform_host)
                clean_url_host_list = (
                    "live.douyin.com",
                    "live.bilibili.com",
                    "www.huajiao.com",
                    "www.zhihu.com",
                    "www.huya.com",
                    "chzzk.naver.com",
                    "www.liveme.com",
                    "www.haixiutv.com",
                    "v.6.cn",
                    "m.6.cn",
                    'www.lehaitv.com'
                )

                if 'live.shopee.' in url_host or '.shp.ee' in url_host:
                    url_host = 'live.shopee.' if 'live.shopee.' in url_host else '.shp.ee'

                if url_host in platform_host or any(ext in url for ext in (".flv", ".m3u8")):
                    if url_host in clean_url_host_list:
                        url = update_file(url_config_file, old_str=url, new_str=url.split('?')[0])

                    if 'xiaohongshu' in url:
                        host_id = re.search('&host_id=(.*?)(?=&|$)', url)
                        if host_id:
                            new_url = url.split('?')[0] + f'?host_id={host_id.group(1)}'
                            url = update_file(url_config_file, old_str=url, new_str=new_url)

                    url_comments = [i for i in url_comments if url not in i]
                    if is_comment_line:
                        url_comments.append(url)
                    else:
                        new_line = (quality, url, name)
                        url_tuples_list.append(new_line)
                else:
                    if not origin_line.startswith('#'):
                        color_obj.print_colored(f"\r{origin_line.strip()} 本行包含未知链接.此条跳过", color_obj.YELLOW)
                        update_file(url_config_file, old_str=origin_line, new_str=origin_line, start_str='#')

        while len(need_update_line_list):
            a = need_update_line_list.pop()
            replace_words = a.split('|')
            if replace_words[0] != replace_words[1]:
                if replace_words[1].startswith("#"):
                    start_with = '#'
                    new_word = replace_words[1][1:]
                else:
                    start_with = None
                    new_word = replace_words[1]
                update_file(url_config_file, old_str=replace_words[0], new_str=new_word, start_str=start_with)

        text_no_repeat_url = list(set(url_tuples_list))

        if len(text_no_repeat_url) > 0:
            for url_tuple in text_no_repeat_url:
                if is_stop_all_requested():
                    break

                monitoring = len(running_list)

                if url_tuple[1] in not_record_list:
                    continue

                if url_tuple[1] not in running_list:
                    print(f"\r{'新增' if not first_start else '传入'}地址: {url_tuple[1]}")
                    monitoring += 1
                    args = [url_tuple, monitoring]
                    create_var[f'thread_{monitoring}'] = threading.Thread(
                        target=start_record,
                        args=args
                    )
                    create_var[f'thread_{monitoring}'].daemon = False
                    create_var[f'thread_{monitoring}'].start()
                    running_list.append(url_tuple[1])
                    time.sleep(local_delay_default)

        url_tuples_list = []
        first_start = False


    except Exception as err:
        logger.error(f"错误信息: {err} 发生错误的行数: {err.__traceback__.tb_lineno}")

    if first_run:
        t = threading.Thread(target=display_info, args=(), daemon=True)
        t.start()
        t2 = threading.Thread(target=adjust_max_request, args=(), daemon=True)
        t2.start()
        first_run = False

    time.sleep(3)
