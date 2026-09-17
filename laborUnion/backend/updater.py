"""Signed update checks and platform-specific update staging."""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import config
from .logutil import setup as _setup_log
from .update_security import UpdateSignatureError, verify_manifest_envelope


log = _setup_log()

def _load_manifest_url() -> str:
    """Load the update endpoint from local build/runtime configuration only."""
    configured = os.environ.get("LUCIMA_UPDATE_URL", "").strip()
    if configured:
        return configured
    asset_root = os.environ.get("ARK_ASSET_ROOT", "").strip()
    candidates = []
    if asset_root:
        candidates.append(Path(asset_root) / "update-endpoint.json")
    candidates.extend((
        Path(__file__).resolve().parent / "update-endpoint.json",
        Path(__file__).resolve().parent.parent / "update-endpoint.json",
    ))
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            endpoint = str(data.get("manifestUrl") or "").strip()
            if endpoint:
                return endpoint
        except (OSError, ValueError, AttributeError):
            continue
    return ""


MANIFEST_URL = _load_manifest_url()
# Check for published updates about once per hour while the app is running.
CHECK_INTERVAL = 60 * 60
CHECK_JITTER = 15 * 60
RETRY_INTERVAL = 30 * 60
MAX_ARTIFACT_SIZE = 1024 * 1024 * 1024
MAX_EXTRACTED_SIZE = 2 * 1024 * 1024 * 1024
_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(RuntimeError):
    pass


def _version_key(value: str) -> tuple[int, int, int, int, str]:
    match = _VERSION_RE.fullmatch(str(value or ""))
    if not match:
        raise UpdateError(f"无效的版本号：{value}")
    prerelease = match.group(4)
    return (
        int(match.group(1)), int(match.group(2)), int(match.group(3)),
        1 if prerelease is None else 0, prerelease or "",
    )


def _newer(left: str, right: str) -> bool:
    return _version_key(left) > _version_key(right)


def _http_client(read_timeout: float = 30.0) -> httpx.Client:
    proxy = config.get_proxy() or None
    return httpx.Client(
        proxy=proxy,
        follow_redirects=True,
        timeout=httpx.Timeout(15.0, connect=15.0, read=read_timeout, write=30.0),
    )


def _validate_artifact(value: object, platform: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise UpdateError(f"{platform} 更新信息格式无效")
    required = ("format", "fileName", "url", "size", "sha256")
    if any(key not in value for key in required):
        raise UpdateError(f"{platform} 更新信息不完整")
    filename = str(value["fileName"])
    if not filename or Path(filename).name != filename:
        raise UpdateError("更新文件名无效")
    parsed = urlparse(str(value["url"]))
    if parsed.scheme not in ("http", "https") or not parsed.hostname \
            or parsed.username or parsed.password:
        raise UpdateError("更新下载地址无效")
    try:
        size = int(value["size"])
    except (TypeError, ValueError) as exc:
        raise UpdateError("更新文件大小无效") from exc
    if size <= 0 or size > MAX_ARTIFACT_SIZE:
        raise UpdateError("更新文件大小超出允许范围")
    digest = str(value["sha256"]).lower()
    if not _SHA256_RE.fullmatch(digest):
        raise UpdateError("更新文件哈希无效")
    return {
        "format": str(value["format"]),
        "fileName": filename,
        "url": str(value["url"]),
        "size": size,
        "sha256": digest,
    }


class UpdateService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._check_lock = threading.Lock()
        self._install_lock = threading.Lock()
        self._wake = threading.Event()
        self._started = False
        self._etag = ""
        self._payload: dict | None = None
        self._checking = False
        self._installing = False
        self._progress_phase = "idle"
        self._progress_downloaded = 0
        self._progress_total = 0
        self._last_checked_at: int | None = None
        self._last_error = ""

    def _set_progress(
        self, phase: str, *, downloaded: int | None = None, total: int | None = None,
    ) -> None:
        with self._lock:
            self._progress_phase = phase
            if downloaded is not None:
                self._progress_downloaded = max(0, int(downloaded))
            if total is not None:
                self._progress_total = max(0, int(total))

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._run, name="update-check", daemon=True).start()

    def _run(self) -> None:
        delay = 0.0
        while True:
            if self._wake.wait(delay):
                self._wake.clear()
            ok = self.check_now()
            delay = (
                CHECK_INTERVAL + random.uniform(-CHECK_JITTER, CHECK_JITTER)
                if ok else RETRY_INTERVAL
            )

    def _validated_payload(self, payload: dict) -> dict:
        if payload.get("schema") != 1 or payload.get("channel") != "stable":
            raise UpdateError("更新清单版本或通道无效")
        latest = str(payload.get("latestVersion") or "")
        minimum = str(payload.get("minSupportedVersion") or "")
        _version_key(latest)
        _version_key(minimum)
        if _newer(minimum, latest):
            raise UpdateError("最低支持版本不能高于最新版本")
        platforms = payload.get("platforms")
        if not isinstance(platforms, dict):
            raise UpdateError("更新平台信息缺失")
        normalized_platforms = {
            "windows": _validate_artifact(platforms.get("windows"), "Windows"),
            "android": _validate_artifact(platforms.get("android"), "Android"),
        }
        notes = payload.get("notes") or []
        if not isinstance(notes, list) or any(not isinstance(note, str) for note in notes):
            raise UpdateError("更新说明格式无效")
        return {
            "schema": 1,
            "channel": "stable",
            "development": bool(payload.get("development")),
            "latestVersion": latest,
            "minSupportedVersion": minimum,
            "publishedAt": str(payload.get("publishedAt") or ""),
            "notes": notes[:20],
            "platforms": normalized_platforms,
        }

    def check_now(self) -> bool:
        if not self._check_lock.acquire(blocking=False):
            return True
        with self._lock:
            self._checking = True
        try:
            if not MANIFEST_URL:
                raise UpdateError("更新服务未配置")
            headers = {"Accept": "application/json"}
            if self._etag and self._payload is not None:
                headers["If-None-Match"] = self._etag
            with _http_client() as client:
                response = client.get(MANIFEST_URL, headers=headers)
            if response.status_code == 304 and self._payload is not None:
                with self._lock:
                    self._last_checked_at = int(time.time() * 1000)
                    self._last_error = ""
                return True
            response.raise_for_status()
            if len(response.content) > 256 * 1024:
                raise UpdateError("更新清单异常过大")
            payload = self._validated_payload(verify_manifest_envelope(response.content))
            with self._lock:
                self._payload = payload
                self._etag = response.headers.get("ETag", "")
                self._last_checked_at = int(time.time() * 1000)
                self._last_error = ""
            return True
        except (httpx.HTTPError, UpdateSignatureError, UpdateError) as exc:
            with self._lock:
                self._last_checked_at = int(time.time() * 1000)
                self._last_error = str(exc)
            log.warning("检查更新失败：%s", exc)
            return False
        except Exception as exc:
            with self._lock:
                self._last_checked_at = int(time.time() * 1000)
                self._last_error = "检查更新时发生未知错误"
            log.exception("检查更新异常：%s", exc)
            return False
        finally:
            with self._lock:
                self._checking = False
            self._check_lock.release()

    def request_check(self) -> dict:
        self.check_now()
        return self.status()

    def status(self) -> dict:
        with self._lock:
            payload = dict(self._payload or {})
            checking = self._checking
            installing = self._installing
            progress_phase = self._progress_phase
            progress_downloaded = self._progress_downloaded
            progress_total = self._progress_total
            last_checked = self._last_checked_at
            last_error = self._last_error
        current = config.APP_VERSION
        latest = payload.get("latestVersion")
        minimum = payload.get("minSupportedVersion")
        available = bool(latest and _newer(latest, current))
        forced = bool(minimum and _newer(minimum, current))
        ignored_version = config.get_ignored_update_version()
        platform = "android" if config.PLATFORM == "android" else "windows"
        artifact = (payload.get("platforms") or {}).get(platform)
        install_supported = config.PLATFORM == "android" or bool(getattr(sys, "frozen", False))
        return {
            "checked": self._payload is not None,
            "checking": checking,
            "available": available,
            "forced": forced,
            "ignored": bool(available and not forced and ignored_version == latest),
            "installing": installing,
            "progress": {
                "phase": progress_phase,
                "downloadedBytes": progress_downloaded,
                "totalBytes": progress_total,
                "percent": round(progress_downloaded * 100 / progress_total, 1)
                if progress_total else 0,
            },
            "installSupported": install_supported,
            "currentVersion": current,
            "latestVersion": latest,
            "minSupportedVersion": minimum,
            "publishedAt": payload.get("publishedAt"),
            "notes": payload.get("notes") or [],
            "platform": platform,
            "artifactAvailable": artifact is not None,
            "lastCheckedAt": last_checked,
            "lastError": last_error,
        }

    def ignore(self, version: str) -> dict:
        state = self.status()
        if state["forced"]:
            raise UpdateError("此版本为强制更新，不能忽略")
        if not state["available"] or version != state["latestVersion"]:
            raise UpdateError("该版本当前不可忽略")
        config.set_ignored_update_version(version)
        return self.status()

    def is_installing(self) -> bool:
        with self._lock:
            return self._installing

    def _download(self, artifact: dict, version: str) -> Path:
        update_dir = config.data_dir() / "updates" / "downloads"
        update_dir.mkdir(parents=True, exist_ok=True)
        final = update_dir / artifact["fileName"]
        temporary = update_dir / f".{artifact['fileName']}.{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        written = 0
        self._set_progress("downloading", downloaded=0, total=artifact["size"])
        try:
            with _http_client(read_timeout=120.0) as client:
                with client.stream("GET", artifact["url"]) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as stream:
                        for chunk in response.iter_bytes(1024 * 1024):
                            written += len(chunk)
                            if written > artifact["size"] or written > MAX_ARTIFACT_SIZE:
                                raise UpdateError("更新文件大小与清单不一致")
                            digest.update(chunk)
                            stream.write(chunk)
                            self._set_progress("downloading", downloaded=written)
            if written != artifact["size"]:
                raise UpdateError("更新文件未下载完整")
            self._set_progress("verifying", downloaded=written)
            if digest.hexdigest() != artifact["sha256"]:
                raise UpdateError("更新文件校验失败")
            os.replace(temporary, final)
            return final
        finally:
            temporary.unlink(missing_ok=True)

    def _extract_windows(self, archive_path: Path, version: str) -> Path:
        stage_root = config.data_dir() / "updates" / "staging" / f"{version}-{uuid.uuid4().hex}"
        stage_root.mkdir(parents=True, exist_ok=False)
        total = 0
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for entry in archive.infolist():
                    name = entry.filename.replace("\\", "/")
                    parts = [part for part in name.split("/") if part]
                    if not parts or name.startswith("/") or ".." in parts:
                        raise UpdateError("Windows 更新包包含非法路径")
                    target = (stage_root / Path(*parts)).resolve()
                    try:
                        target.relative_to(stage_root.resolve())
                    except ValueError as exc:
                        raise UpdateError("Windows 更新包包含越界路径") from exc
                    total += entry.file_size
                    if total > MAX_EXTRACTED_SIZE:
                        raise UpdateError("Windows 更新包解压后异常过大")
                    if entry.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as source, target.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
            actual = {entry.name for entry in stage_root.iterdir()}
            if actual != {"LucimaTools.exe", "_internal"} \
                    or not (stage_root / "LucimaTools.exe").is_file() \
                    or not (stage_root / "_internal").is_dir():
                raise UpdateError("Windows 更新包结构无效")
            return stage_root
        except Exception:
            shutil.rmtree(stage_root, ignore_errors=True)
            raise

    def _launch_windows_helper(self, stage_root: Path, version: str) -> None:
        if not getattr(sys, "frozen", False):
            raise UpdateError("源码调试模式不执行程序覆盖，请使用打包版本测试更新")
        app_root = Path(sys.executable).resolve().parent
        if config.data_dir().resolve() != app_root:
            raise UpdateError("当前数据目录与应用目录不一致，拒绝自动覆盖")
        updates = app_root / "updates"
        updates.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        helper = updates / f"apply-{version}-{token}.ps1"
        success = updates / f"launch-success-{token}.flag"
        pending = updates / "pending-launch.json"
        pending.write_text(json.dumps({"token": token}), encoding="utf-8")
        helper.write_text(_WINDOWS_HELPER, encoding="utf-8-sig")
        command = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(helper), "-AppRoot", str(app_root), "-StageRoot", str(stage_root),
            "-OldPid", str(os.getpid()), "-Token", token, "-Version", version,
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(command, close_fds=True, creationflags=creationflags)
        threading.Thread(target=self._exit_for_update, daemon=True).start()

    @staticmethod
    def _wait_for_account_safe_point(timeout: float = 5 * 60) -> None:
        """Wait until in-flight game operations have released every account lock."""
        from . import accounts

        deadline = time.monotonic() + timeout
        while any(account.lock.locked() for account in accounts.all_accounts()):
            if time.monotonic() >= deadline:
                raise UpdateError("等待当前任务结束超时，请稍后重试更新")
            time.sleep(0.2)

    @staticmethod
    def _exit_for_update() -> None:
        time.sleep(1.0)
        os._exit(0)

    def install(self) -> dict:
        if not self._install_lock.acquire(blocking=False):
            raise UpdateError("更新正在处理中")
        keep_locked_until_exit = False
        with self._lock:
            self._installing = True
            self._progress_phase = "preparing"
            self._progress_downloaded = 0
            self._progress_total = 0
        try:
            state = self.status()
            if not state["available"]:
                raise UpdateError("当前没有可安装的更新")
            if not state["installSupported"]:
                raise UpdateError("当前运行方式不支持自动覆盖更新")
            platform = state["platform"]
            with self._lock:
                payload = self._payload or {}
                artifact = (payload.get("platforms") or {}).get(platform)
            if artifact is None:
                raise UpdateError("服务器尚未提供当前平台的安装包")
            expected_format = "android-apk" if platform == "android" else "windows-portable-zip"
            if artifact["format"] != expected_format:
                raise UpdateError("当前平台的更新包格式不受支持")
            version = str(state["latestVersion"])
            downloaded = self._download(artifact, version)
            self._set_progress("waiting")
            self._wait_for_account_safe_point()
            if platform == "android":
                self._set_progress("ready")
                return {
                    "ok": True,
                    "platform": "android",
                    "version": version,
                    "installerPath": str(downloaded.resolve()),
                }
            self._set_progress("preparing-install")
            stage_root = self._extract_windows(downloaded, version)
            self._set_progress("restarting")
            self._launch_windows_helper(stage_root, version)
            # Keep requests blocked during the short hand-off to the helper.
            keep_locked_until_exit = True
            return {"ok": True, "platform": "windows", "version": version, "restarting": True}
        finally:
            if not keep_locked_until_exit:
                with self._lock:
                    self._installing = False
                    self._progress_phase = "idle"
                self._install_lock.release()


_WINDOWS_HELPER = r'''param(
  [Parameter(Mandatory=$true)][string]$AppRoot,
  [Parameter(Mandatory=$true)][string]$StageRoot,
  [Parameter(Mandatory=$true)][int]$OldPid,
  [Parameter(Mandatory=$true)][string]$Token,
  [Parameter(Mandatory=$true)][string]$Version
)
$ErrorActionPreference = "Stop"
$updates = Join-Path $AppRoot "updates"
$rollback = Join-Path $updates ("rollback-" + $Version + "-" + $Token)
$success = Join-Path $updates ("launch-success-" + $Token + ".flag")
$pending = Join-Path $updates "pending-launch.json"
$helperLog = Join-Path $updates ("update-helper-" + $Token + ".log")
$newProcess = $null
$oldExeMoved = $false
$oldInternalMoved = $false
$newExeMoved = $false
$newInternalMoved = $false

function Write-UpdateLog([string]$Message) {
  Add-Content -LiteralPath $helperLog -Value ((Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff") + " " + $Message) -Encoding UTF8
}

Write-UpdateLog ("helper started; old pid=" + $OldPid)
for ($i = 0; $i -lt 240; $i++) {
  if (-not (Get-Process -Id $OldPid -ErrorAction SilentlyContinue)) { break }
  Start-Sleep -Milliseconds 500
}
if (Get-Process -Id $OldPid -ErrorAction SilentlyContinue) {
  Write-UpdateLog "old process did not exit"
  exit 20
}

try {
  New-Item -ItemType Directory -Path $rollback -Force | Out-Null
  Write-UpdateLog "moving old executable"
  Move-Item -LiteralPath (Join-Path $AppRoot "LucimaTools.exe") -Destination (Join-Path $rollback "LucimaTools.exe")
  $oldExeMoved = $true
  Write-UpdateLog "moving old internal directory"
  Move-Item -LiteralPath (Join-Path $AppRoot "_internal") -Destination (Join-Path $rollback "_internal")
  $oldInternalMoved = $true
  Write-UpdateLog "installing new executable"
  Move-Item -LiteralPath (Join-Path $StageRoot "LucimaTools.exe") -Destination (Join-Path $AppRoot "LucimaTools.exe")
  $newExeMoved = $true
  Write-UpdateLog "installing new internal directory"
  $internalSource = Join-Path $StageRoot "_internal"
  $internalTarget = Join-Path $AppRoot "_internal"
  if (Test-Path -LiteralPath $internalTarget) { throw "New internal target already exists" }
  New-Item -ItemType Directory -Path $internalTarget | Out-Null
  $newInternalMoved = $true
  & robocopy.exe $internalSource $internalTarget /E /COPY:DAT /DCOPY:DAT /R:8 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
  $robocopyExit = $LASTEXITCODE
  if ($robocopyExit -gt 7) { throw ("Copying new internal directory failed with robocopy exit " + $robocopyExit) }
  Write-UpdateLog ("new internal directory copied; robocopy exit=" + $robocopyExit)
  Write-UpdateLog "starting updated application"
  $newProcess = Start-Process -FilePath (Join-Path $AppRoot "LucimaTools.exe") -WorkingDirectory $AppRoot -PassThru
  for ($i = 0; $i -lt 120; $i++) {
    if (Test-Path -LiteralPath $success) { break }
    if ($newProcess.HasExited) { throw "Updated application exited during startup" }
    Start-Sleep -Milliseconds 500
    $newProcess.Refresh()
  }
  if (-not (Test-Path -LiteralPath $success)) { throw "Updated application did not report startup success" }
  Write-UpdateLog "updated application reported startup success"
  Remove-Item -LiteralPath $rollback -Recurse -Force
  Remove-Item -LiteralPath $StageRoot -Recurse -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $success -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $pending -Force -ErrorAction SilentlyContinue
} catch {
  Write-UpdateLog ("update failed: " + $_.Exception.Message)
  if ($newProcess -and -not $newProcess.HasExited) { Stop-Process -Id $newProcess.Id -Force -ErrorAction SilentlyContinue }
  if ($newExeMoved) {
    Remove-Item -LiteralPath (Join-Path $AppRoot "LucimaTools.exe") -Force -ErrorAction SilentlyContinue
  }
  if ($newInternalMoved) {
    Remove-Item -LiteralPath (Join-Path $AppRoot "_internal") -Recurse -Force -ErrorAction SilentlyContinue
  }
  if ($oldExeMoved -and (Test-Path -LiteralPath (Join-Path $rollback "LucimaTools.exe"))) {
    Move-Item -LiteralPath (Join-Path $rollback "LucimaTools.exe") -Destination $AppRoot
  }
  if ($oldInternalMoved -and (Test-Path -LiteralPath (Join-Path $rollback "_internal"))) {
    Move-Item -LiteralPath (Join-Path $rollback "_internal") -Destination $AppRoot
  }
  Remove-Item -LiteralPath $pending -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $rollback -Recurse -Force -ErrorAction SilentlyContinue
  Write-UpdateLog "rollback completed; restarting previous version"
  Start-Process -FilePath (Join-Path $AppRoot "LucimaTools.exe") -WorkingDirectory $AppRoot
  exit 30
}
'''


service = UpdateService()


def mark_launch_success() -> None:
    """Let the detached Windows helper know the new bundle started correctly."""
    if not getattr(sys, "frozen", False):
        return
    updates = config.data_dir() / "updates"
    pending = updates / "pending-launch.json"
    try:
        data = json.loads(pending.read_text(encoding="utf-8"))
        token = str(data.get("token") or "")
        if token and re.fullmatch(r"[0-9a-f]{32}", token):
            (updates / f"launch-success-{token}.flag").write_text("ok", encoding="ascii")
    except Exception:
        return
