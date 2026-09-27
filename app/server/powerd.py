#!/usr/bin/env python3
import argparse
import http.server
import json
import math
import mimetypes
import os
import re
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path


APP = "ryzen-power-control"
VERSION = "1.00"
MIN_WATTS = 10
MAX_WATTS = 200
MAX_TEST_SECONDS = 120
TEMP_LIMIT_C = 85.0
LIMIT_FIELDS = {
    "stapm": "STAPM LIMIT",
    "fast": "PPT LIMIT FAST",
    "slow": "PPT LIMIT SLOW",
}
SUPPORTED_CPU_FAMILIES = {
    "Raven": ("Ryzen 2000 APU / Mobile", frozenset(LIMIT_FIELDS)),
    "Picasso": ("Ryzen 3000 APU / Mobile", frozenset(LIMIT_FIELDS)),
    "Dali": ("Athlon 3000 Mobile", frozenset(LIMIT_FIELDS)),
    "Renoir": ("Ryzen 4000 APU / Mobile", frozenset(LIMIT_FIELDS)),
    "Lucienne": ("Ryzen 5000 Mobile", frozenset(LIMIT_FIELDS)),
    "Cezanne": ("Ryzen 5000 APU / Mobile", frozenset(LIMIT_FIELDS)),
    "Vangogh": ("Ryzen Z1 / Steam Deck 等定制 APU", frozenset(LIMIT_FIELDS)),
    "Rembrandt": ("Ryzen 6000 Mobile", frozenset(LIMIT_FIELDS)),
    "Mendocino": ("Ryzen 7020 系列", frozenset(LIMIT_FIELDS)),
    "Phoenix Point": ("Ryzen 7040 系列", frozenset(LIMIT_FIELDS)),
    "Hawk Point": ("Ryzen 8040 系列", frozenset(LIMIT_FIELDS)),
    "Dragon Range": ("Ryzen 7045 系列", frozenset(LIMIT_FIELDS)),
    "Krackan Point": ("Ryzen AI 300 系列", frozenset(LIMIT_FIELDS)),
    "Strix Point": ("Ryzen AI 300 系列", frozenset(LIMIT_FIELDS)),
    "Strix Halo": ("Ryzen AI Max 300 系列", frozenset(LIMIT_FIELDS)),
    "Fire Range": ("Ryzen 9000HX 系列", frozenset(LIMIT_FIELDS)),
}
MENDOCINO_CPU_ID = (0x17, 160)


class PowerError(Exception):
    pass


def parse_info(text):
    values = {}
    for line in text.splitlines():
        columns = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(columns) < 2:
            continue
        try:
            values[columns[0]] = float(columns[1])
        except ValueError:
            continue
    required = tuple(LIMIT_FIELDS.values()) + (
        "STAPM VALUE",
        "PPT VALUE FAST",
        "PPT VALUE SLOW",
        "THM VALUE CORE",
    )
    missing = [name for name in required if name not in values]
    if missing:
        raise PowerError("ryzenadj 输出缺少字段：" + ", ".join(missing))
    family_match = re.search(r"^CPU Family:\s*(.+)$", text, re.MULTILINE)
    version_match = re.search(r"^Version:\s*(.+)$", text, re.MULTILINE)
    socket_match = re.search(r"^\|\s*0x0098\s*\|\s*0x[0-9A-Fa-f]+\s*\|\s*([^|]+)", text, re.MULTILINE)

    def optional_watts(value):
        return value if value is not None and math.isfinite(value) else None

    return {
        "cpu_family": family_match.group(1).strip() if family_match else "Unknown",
        "ryzenadj_version": version_match.group(1).strip() if version_match else "unknown",
        "limits": {key: values[field] for key, field in LIMIT_FIELDS.items()},
        "platform_limit": values.get("PPT LIMIT APU"),
        "power": {
            "stapm": values["STAPM VALUE"],
            "fast": values["PPT VALUE FAST"],
            "slow": values["PPT VALUE SLOW"],
            "socket": optional_watts(float(socket_match.group(1)) if socket_match else None),
            "apu": optional_watts(values.get("PPT VALUE APU")),
        },
        "temperature": values["THM VALUE CORE"],
    }


class PowerApp:
    def __init__(self, ryzenadj, state_dir):
        self.ryzenadj = str(Path(ryzenadj).resolve())
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.baseline_path = self.state_dir / "baseline.json"
        self.control_path = self.state_dir / "control.json"
        try:
            self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except OSError:
            self.boot_id = None
        self.io_lock = threading.Lock()
        self.test_lock = threading.Lock()
        self.baseline = None
        self.baseline_error = None
        self.control_enabled = False
        self.target_watts = None
        self.target_limits = None
        self.separate_limits = False
        self.test_active = False
        self.test_processes = []
        self.test_started = 0.0
        self.test_deadline = 0.0
        self.test_result = "未运行"
        self.test_token = 0
        self._load_or_capture_baseline()
        self._load_control_state()
        if self.control_enabled:
            try:
                info = self._read_info()
            except PowerError:
                self.control_enabled = False
                self._save_control_state()
                return
            if not self._supported_limit_keys(info):
                self.control_enabled = False
                self._save_control_state()
                return
            supported_keys = self._supported_limit_keys(info)
            if max(self.target_limits[key] for key in supported_keys) > self._limit_range(info)["max"]:
                raise PowerError("保存的功耗目标超过固件报告范围")
            self._apply_limits({key: self.target_limits[key] for key in supported_keys})

    @staticmethod
    def _power_limit_support(cpu_family):
        family = SUPPORTED_CPU_FAMILIES.get(cpu_family)
        supported = family[1] if family else ()
        return {key: key in supported for key in LIMIT_FIELDS}

    @classmethod
    def _supported_limit_keys(cls, info):
        return [key for key, supported in cls._power_limit_support(info["cpu_family"]).items() if supported]

    @classmethod
    def _require_supported_cpu(cls, info):
        if not cls._supported_limit_keys(info):
            raise PowerError("当前处理器系列不受 RyzenAdj 功耗限制接口支持")

    def _run(self, args):
        try:
            result = subprocess.run(
                [self.ryzenadj, *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PowerError("无法运行 ryzenadj：%s" % exc) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise PowerError("ryzenadj 失败：%s" % (detail[-1200:] or result.returncode))
        return result.stdout

    def _read_info(self):
        with self.io_lock:
            info = parse_info(self._run(["--info", "--dump-table"]))
        if info["cpu_family"] == "Unknown":
            try:
                cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8")
            except OSError:
                return info
            family = re.search(r"^cpu family\s*:\s*(\d+)$", cpuinfo, re.MULTILINE)
            model = re.search(r"^model\s*:\s*(\d+)$", cpuinfo, re.MULTILINE)
            if family and model and (int(family.group(1)), int(model.group(1))) == MENDOCINO_CPU_ID:
                info["cpu_family"] = "Mendocino"
        return info

    def _validate_limits(self, limits, expected_keys=None):
        expected_keys = set(LIMIT_FIELDS) if expected_keys is None else set(expected_keys)
        if not isinstance(limits, dict) or set(limits) != expected_keys:
            raise PowerError("功耗墙数据无效")
        checked = {}
        for key in expected_keys:
            value = limits.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PowerError("功耗墙数据无效：%s" % key)
            if not math.isfinite(value) or value <= 0 or value > 200:
                raise PowerError("功耗墙超出可恢复范围：%s" % key)
            checked[key] = float(value)
        return checked

    def _save_baseline(self, limits):
        payload = json.dumps({"limits": limits, "boot_id": self.boot_id}, ensure_ascii=False).encode("utf-8")
        temporary = self.baseline_path.with_suffix(".tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, self.baseline_path)

    def _save_control_state(self):
        payload = json.dumps({
            "enabled": self.control_enabled,
            "watts": self.target_watts,
            "limits": self.target_limits,
            "separate_limits": self.separate_limits,
        }, ensure_ascii=False).encode("utf-8")
        temporary = self.control_path.with_suffix(".tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, self.control_path)

    def _load_control_state(self):
        if not self.control_path.exists():
            if self.baseline is not None:
                self.restore()
            self._save_control_state()
            return
        try:
            payload = json.loads(self.control_path.read_text(encoding="utf-8"))
            enabled = payload.get("enabled")
            watts = payload.get("watts")
            limits = payload.get("limits")
            separate = payload.get("separate_limits", False)
            if not isinstance(enabled, bool):
                raise ValueError("enabled 无效")
            if limits is None and watts is not None:
                if (isinstance(watts, bool) or not isinstance(watts, int)
                        or not MIN_WATTS <= watts <= MAX_WATTS):
                    raise ValueError("watts 无效")
                limits = {key: watts for key in LIMIT_FIELDS}
            if limits is not None:
                limits = self._validate_limits(limits)
                if any(value < MIN_WATTS or value > MAX_WATTS for value in limits.values()):
                    raise ValueError("功耗上限无效")
            if not isinstance(separate, bool):
                raise ValueError("separate_limits 无效")
            if enabled and limits is None:
                raise ValueError("启用状态缺少功耗目标")
            self.control_enabled = enabled
            self.target_limits = limits
            self.target_watts = self._common_watts(limits)
            self.separate_limits = separate or (limits is not None and self.target_watts is None)
        except (OSError, ValueError, AttributeError, PowerError) as exc:
            raise PowerError("保存的功耗接管状态无效：%s" % exc) from exc

    def _load_or_capture_baseline(self):
        if self.baseline_path.exists():
            try:
                payload = json.loads(self.baseline_path.read_text(encoding="utf-8"))
                self.baseline = self._validate_limits(payload.get("limits"))
                saved_boot_id = payload.get("boot_id")
            except (OSError, ValueError, AttributeError, PowerError) as exc:
                self.baseline_error = "保存的恢复值无效：%s" % exc
                return
            if not self.boot_id or not saved_boot_id:
                if self.boot_id and not saved_boot_id:
                    try:
                        self._save_baseline(self.baseline)
                    except OSError as exc:
                        self.baseline = None
                        self.baseline_error = "无法保存 BIOS 功耗基准：%s" % exc
                return
            if saved_boot_id == self.boot_id:
                return
            try:
                self.baseline = self._read_info()["limits"]
                self._save_baseline(self.baseline)
                self.baseline_error = None
            except (OSError, PowerError) as exc:
                self.baseline = None
                self.baseline_error = "新启动后无法更新 BIOS 功耗基准：%s" % exc
            return
        try:
            self.baseline = self._read_info()["limits"]
            self._save_baseline(self.baseline)
        except (OSError, PowerError) as exc:
            self.baseline_error = "首次读取时无法记录恢复值：%s" % exc

    def _set_limits(self, limits):
        info = self._read_info()
        supported_keys = self._supported_limit_keys(info)
        if not supported_keys:
            raise PowerError("当前处理器系列不受 RyzenAdj 功耗限制接口支持")
        limits = self._validate_limits(limits, supported_keys)
        args = []
        for key, field in LIMIT_FIELDS.items():
            if key not in limits:
                continue
            args.append("--%s-limit=%d" % (
                "stapm" if key == "stapm" else key,
                round(limits[key] * 1000),
            ))
        with self.io_lock:
            self._run(args + ["--info"])
            actual = parse_info(self._run(["--info"]))["limits"]
        for key, expected in limits.items():
            if abs(actual[key] - expected) > 0.01:
                raise PowerError("%s 读回为 %.3fW，期望 %.3fW" % (
                    LIMIT_FIELDS[key], actual[key], expected
                ))
        return actual

    def set_watts(self, watts):
        if isinstance(watts, bool) or not isinstance(watts, int):
            raise PowerError("功耗必须是整数瓦数")
        info = self._read_info()
        supported_keys = self._supported_limit_keys(info)
        if not supported_keys:
            raise PowerError("当前处理器系列不受 RyzenAdj 功耗限制接口支持")
        max_watts = self._limit_range(info)["max"]
        if not MIN_WATTS <= watts <= max_watts:
            raise PowerError("当前设备支持 %d–%dW" % (MIN_WATTS, max_watts))
        if not self.control_enabled:
            raise PowerError("请先开启功耗接管")
        return self._apply_watts(watts, supported_keys)

    def set_limits(self, limits):
        if not self.control_enabled:
            raise PowerError("请先开启功耗接管")
        info = self._read_info()
        supported_keys = self._supported_limit_keys(info)
        if not supported_keys:
            raise PowerError("当前处理器系列不受 RyzenAdj 功耗限制接口支持")
        if not isinstance(limits, dict) or set(limits) != set(supported_keys):
            raise PowerError("请只提供当前处理器支持调整的功耗项")
        for value in limits.values():
            if isinstance(value, bool) or not isinstance(value, int):
                raise PowerError("功耗必须是整数瓦数")
        maximum = self._limit_range(info)["max"]
        if any(not MIN_WATTS <= value <= maximum for value in limits.values()):
            raise PowerError("当前设备支持 %d–%dW" % (MIN_WATTS, maximum))
        return self._apply_limits({key: float(value) for key, value in limits.items()}, separate=True)

    def _apply_watts(self, watts, supported_keys):
        limits = {key: float(watts) for key in supported_keys}
        return self._apply_limits(limits, separate=False)

    @staticmethod
    def _common_watts(limits):
        if limits is None or len(set(limits.values())) != 1:
            return None
        return round(next(iter(limits.values())))

    def _apply_limits(self, limits, separate=None):
        limits = self._validate_limits(limits, limits.keys())
        actual = self._set_limits(limits)
        self.target_limits = actual
        self.target_watts = self._common_watts({key: actual[key] for key in limits})
        if separate is not None:
            self.separate_limits = separate
        self._save_control_state()
        return actual

    def set_control(self, enabled):
        if not isinstance(enabled, bool):
            raise PowerError("功耗接管状态无效")
        if enabled == self.control_enabled:
            return self.control_enabled
        info = self._read_info()
        supported_keys = self._supported_limit_keys(info)
        if not supported_keys:
            if not enabled:
                self.control_enabled = False
                self._save_control_state()
                return False
            self._require_supported_cpu(info)
        if enabled:
            if self.target_limits is None:
                watts = round(info["limits"]["stapm"])
                self.target_limits = {key: float(watts) for key in LIMIT_FIELDS}
                self.target_watts = watts
            max_watts = self._limit_range(info)["max"]
            if any(self.target_limits[key] < MIN_WATTS or self.target_limits[key] > max_watts
                   for key in supported_keys):
                raise PowerError("已保存的功耗值超出当前设备范围")
            self.control_enabled = True
            try:
                self._apply_limits({key: self.target_limits[key] for key in supported_keys})
            except Exception:
                self.control_enabled = False
                raise
            return self.control_enabled
        self.restore(persist=False)
        self.control_enabled = False
        self._save_control_state()
        return self.control_enabled

    def restore(self, persist=True):
        if self.baseline is None:
            raise PowerError(self.baseline_error or "没有可恢复的功耗墙记录")
        info = self._read_info()
        self._require_supported_cpu(info)
        supported_keys = self._supported_limit_keys(info)
        actual = self._set_limits({key: self.baseline[key] for key in supported_keys})
        self.target_limits = actual
        self.target_watts = self._common_watts({key: actual[key] for key in supported_keys})
        self.separate_limits = self.target_watts is None
        if persist:
            self._save_control_state()
        return actual

    def _limit_range(self, info):
        supported_keys = self._supported_limit_keys(info)
        limits = [
            info["limits"][key] for key in supported_keys
            if key in info["limits"]
        ]
        limits = [
            value for value in limits
            if math.isfinite(value) and value > 0
        ]
        platform_limit = info["platform_limit"]
        if isinstance(platform_limit, (int, float)) and math.isfinite(platform_limit) and platform_limit > 0:
            limits.append(platform_limit)
        if not limits:
            raise PowerError("无法从 RyzenAdj 读取有效的功耗范围")
        max_watts = min(MAX_WATTS, max(MIN_WATTS, math.ceil(max(limits))))
        return {"min": MIN_WATTS, "max": max_watts}

    def _worker_count(self):
        try:
            return max(1, len(os.sched_getaffinity(0)))
        except AttributeError:
            return max(1, os.cpu_count() or 1)

    def start_test(self, seconds):
        if isinstance(seconds, bool) or not isinstance(seconds, int):
            raise PowerError("测试时长必须是整数秒")
        if not 5 <= seconds <= MAX_TEST_SECONDS:
            raise PowerError("测试时长需在 5–%d 秒之间" % MAX_TEST_SECONDS)
        with self.test_lock:
            if self.test_active:
                raise PowerError("负载测试正在运行")
            current = self._read_info()
            self._require_supported_cpu(current)
            if current["temperature"] >= TEMP_LIMIT_C:
                raise PowerError("CPU 温度已达到 %.1f°C，暂不启动负载测试" % TEMP_LIMIT_C)
            count = self._worker_count()
            deadline = time.monotonic() + seconds
            workers = []
            worker_file = Path(__file__).with_name("cpu_burn.py")
            try:
                for _ in range(count):
                    workers.append(subprocess.Popen(
                        [sys.executable, str(worker_file), str(os.getpid()), str(seconds)],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        close_fds=True,
                    ))
            except OSError as exc:
                for process in workers:
                    process.terminate()
                for process in workers:
                    process.wait()
                raise PowerError("启动负载进程失败：%s" % exc) from exc
            self.test_processes = workers
            self.test_active = True
            self.test_started = time.monotonic()
            self.test_deadline = deadline
            self.test_result = "运行中"
            self.test_token += 1
            token = self.test_token
        threading.Thread(target=self._watch_test, args=(token,), daemon=True).start()
        return self.test_status()

    def _watch_test(self, token):
        while True:
            with self.test_lock:
                if not self.test_active or token != self.test_token:
                    return
                deadline = self.test_deadline
                workers = list(self.test_processes)
            if time.monotonic() >= deadline:
                self.stop_test("测试完成")
                return
            if not any(process.poll() is None for process in workers):
                self.stop_test("负载进程已退出")
                return
            try:
                temperature = self._read_info()["temperature"]
            except PowerError as exc:
                self.stop_test("功耗监测失败，已停止：%s" % exc)
                return
            if temperature >= TEMP_LIMIT_C:
                self.stop_test("温度达到 %.1f°C，已自动停止" % TEMP_LIMIT_C)
                return
            time.sleep(1)

    def stop_test(self, reason="已手动停止"):
        with self.test_lock:
            if not self.test_active:
                return self._test_status_unlocked()
            workers = self.test_processes
            self.test_active = False
            self.test_processes = []
            self.test_result = reason
            for process in workers:
                if process.poll() is None:
                    process.terminate()
            for process in workers:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            return self._test_status_unlocked()

    def _test_status_unlocked(self):
        remaining = max(0, int(self.test_deadline - time.monotonic())) if self.test_active else 0
        return {
            "running": self.test_active,
            "remaining_seconds": remaining,
            "workers": sum(1 for process in self.test_processes if process.poll() is None),
            "result": self.test_result,
        }

    def test_status(self):
        with self.test_lock:
            return self._test_status_unlocked()

    def status(self):
        try:
            current = self._read_info()
        except PowerError as exc:
            return {
                "ok": True,
                "version": VERSION,
                "cpu_family": "Unknown",
                "cpu_supported": False,
                "cpu_series": None,
                "power_limit_support": self._power_limit_support("Unknown"),
                "cpu_error": str(exc),
            }
        cpu_family = current["cpu_family"]
        power_limit_support = self._power_limit_support(cpu_family)
        return {
            "ok": True,
            "version": VERSION,
            "cpu_family": cpu_family,
            "cpu_supported": any(power_limit_support.values()),
            "cpu_series": (SUPPORTED_CPU_FAMILIES.get(cpu_family) or (None, ()))[0],
            "power_limit_support": power_limit_support,
            "ryzenadj_version": current["ryzenadj_version"],
            "limits": current["limits"],
            "baseline": self.baseline,
            "baseline_error": self.baseline_error,
            "control_enabled": self.control_enabled,
            "target_watts": self.target_watts,
            "target_limits": self.target_limits,
            "separate_limits": self.separate_limits,
            "power": current["power"],
            "temperature": current["temperature"],
            "test": self.test_status(),
            "limit_range": self._limit_range(current),
        }


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = APP + "/" + VERSION

    def address_string(self):
        return "unix"

    def log_message(self, format_string, *args):
        print("http " + (format_string % args), flush=True)

    def _path(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        prefix = self.server.gateway_prefix
        if path.startswith(prefix):
            path = path[len(prefix):]
        return path if path.startswith("/") else "/" + path

    def _respond(self, code, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, code=200):
        self._respond(code, json.dumps(payload, ensure_ascii=False))

    def _authorise(self):
        userid = self.headers.get("X-Trim-Userid")
        if userid is not None:
            is_admin = self.headers.get("X-Trim-Isadmin", "").strip().lower()
            return is_admin in ("1", "true", "yes")
        return True

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > 16384:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        root = Path(self.server.ui_dir).resolve()
        target = (root / path.lstrip("/")).resolve()
        if target != root and root not in target.parents:
            self._json({"ok": False, "error": "forbidden"}, 403)
            return
        if not target.is_file():
            self._json({"ok": False, "error": "not found"}, 404)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript",):
            content_type += "; charset=utf-8"
        self._respond(200, target.read_bytes(), content_type)

    def do_GET(self):
        if not self._authorise():
            self._json({"ok": False, "error": "需要管理员权限"}, 403)
            return
        path = self._path()
        if path == "/api/status":
            try:
                self._json(self.server.app.status())
            except PowerError as exc:
                self._json({"ok": False, "error": str(exc)}, 503)
            return
        if path.startswith("/api/"):
            self._json({"ok": False, "error": "unknown endpoint"}, 404)
            return
        self._static(path)

    do_HEAD = do_GET

    def do_POST(self):
        if not self._authorise():
            self._json({"ok": False, "error": "需要管理员权限"}, 403)
            return
        payload = self._body()
        if not isinstance(payload, dict):
            self._json({"ok": False, "error": "请求数据无效"}, 400)
            return
        path = self._path()
        try:
            if path == "/api/power/set":
                limits = (self.server.app.set_limits(payload.get("limits"))
                          if "limits" in payload
                          else self.server.app.set_watts(payload.get("watts")))
                self._json({"ok": True, "limits": limits})
            elif path == "/api/power/control":
                enabled = self.server.app.set_control(payload.get("enabled"))
                self._json({"ok": True, "enabled": enabled})
            elif path == "/api/power/restore":
                limits = self.server.app.restore()
                self._json({"ok": True, "limits": limits})
            elif path == "/api/test/start":
                test = self.server.app.start_test(payload.get("seconds", 30))
                self._json({"ok": True, "test": test})
            elif path == "/api/test/stop":
                test = self.server.app.stop_test("已手动停止")
                self._json({"ok": True, "test": test})
            else:
                self._json({"ok": False, "error": "unknown endpoint"}, 404)
        except PowerError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ryzenadj", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--ui", required=True)
    parser.add_argument("--gateway-prefix", default="")
    args = parser.parse_args()

    app = PowerApp(args.ryzenadj, args.state_dir)
    socket_path = Path(args.socket)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    server = UnixHTTPServer(str(socket_path), Handler)
    os.chmod(socket_path, 0o600)
    server.app = app
    server.ui_dir = args.ui
    server.gateway_prefix = args.gateway_prefix.rstrip("/")

    stopping = threading.Event()
    def stop_signal(_signum, _frame):
        stopping.set()
    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("%s %s serving unix:%s" % (APP, VERSION, socket_path), flush=True)
    try:
        stopping.wait()
    finally:
        app.stop_test("应用服务已停止")
        server.shutdown()
        server.server_close()
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
