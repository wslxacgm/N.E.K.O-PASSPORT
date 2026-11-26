"""把 N.E.K.O 前端的 TTS 音频镜像到本地桥接插件。

插件启动时自动给 `static/app/app-audio-playback.js` 打补丁：
1. 在 `handleAudioBlob` 里镜像一份原始音频；
2. 在文件末尾追加一段 WebSocket 转发代码。

补丁是幂等的：已存在标记就跳过，N.E.K.O 升级覆盖后下次启动会重新写入。
正常停止/退出时保留补丁（N.E.K.O 页面比插件先加载，删掉补丁会导致下次启动拿不到钩子）；
卸载时由退出后启动的独立清理进程移除补丁，恢复成原始前端文件。
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

BEGIN_MARK = "// NEKO_PASSPORT_TAP_BEGIN"
END_MARK = "// NEKO_PASSPORT_TAP_END"
HOOK_MARK = "nekoPassportTapAudio(arrayBuffer"
TAIL_MARK = "window.nekoPassportTapAudio = function"

HOOK_COMMENT = "// NEKO Passport: 镜像 TTS 原始音频到本地桥接插件"
HOOK_LINE = """        // NEKO Passport: 镜像 TTS 原始音频到本地桥接插件
        try { if (window.nekoPassportTapAudio) window.nekoPassportTapAudio(arrayBuffer.slice(0)); } catch (_) { }
"""

# 早期手工注入的补丁（没有 BEGIN/END 标记），启动时自动归一化或移除。
LEGACY_HOOK_COMMENT = "// NEKO Passport: 把 TTS 原始音频镜像到本地桥接插件；未装插件时静默忽略。"
LEGACY_HOOK_END = "} catch (_) { /* noop */ }"
LEGACY_TAP_HEADER = "// ---- NEKO Passport: 将 N.E.K.O 正在播放的 TTS 原始音频镜像到本地桥接插件 ----"

TAP_BLOCK = """
// NEKO_PASSPORT_TAP_BEGIN
// NEKO_PASSPORT_TAP_VERSION: 2
// 将 N.E.K.O 正在播放的 TTS 原始音频镜像到本地桥接插件；未装插件时静默忽略。
(function () {
    'use strict';
    var TAP_URL = 'ws://127.0.0.1:8765/audio';
    var MAX_QUEUE = 64;
    var MAX_RETRY = 10;
    var tap = { ws: null, ready: false, queue: [], retry: 0 };

    function connect() {
        if (tap.ws && (tap.ws.readyState === WebSocket.OPEN || tap.ws.readyState === WebSocket.CONNECTING)) {
            return;
        }
        var ws;
        try {
            ws = new WebSocket(TAP_URL);
        } catch (_) {
            schedule();
            return;
        }
        ws.binaryType = 'arraybuffer';
        tap.ws = ws;
        ws.onopen = function () {
            tap.ready = true;
            tap.retry = 0;
            while (tap.queue.length > 0 && ws.readyState === WebSocket.OPEN) {
                try { ws.send(tap.queue.shift()); } catch (_) { break; }
            }
        };
        ws.onclose = function () {
            tap.ready = false;
            if (tap.ws === ws) tap.ws = null;
            schedule();
        };
        ws.onerror = function () {
            try { ws.close(); } catch (_) { }
        };
    }

    function schedule() {
        tap.retry += 1;
        // 插件未运行时不无限重连，最多尝试 10 次后静默停止。
        if (tap.retry > MAX_RETRY) return;
        setTimeout(connect, Math.min(3000 + tap.retry * 1000, 15000));
    }

    connect();

    window.nekoPassportTapAudio = function (arrayBuffer) {
        if (!arrayBuffer || !arrayBuffer.byteLength) return;
        if (tap.ready && tap.ws && tap.ws.readyState === WebSocket.OPEN) {
            try { tap.ws.send(arrayBuffer); return; } catch (_) { /* fallthrough */ }
        }
        if (!tap.ws && tap.retry > MAX_RETRY) {
            // 有新的语音时再试一次，插件重新启动后无需刷新页面。
            tap.retry = 0;
            connect();
        }
        if (tap.queue.length >= MAX_QUEUE) tap.queue.shift();
        tap.queue.push(arrayBuffer);
    };
})();
// NEKO_PASSPORT_TAP_END
"""


def _candidate_paths(extra: str | None) -> list[Path]:
    paths: list[Path] = []
    if extra:
        paths.append(Path(extra))
    relative = [
        Path("portable/resources/bin/static/app/app-audio-playback.js"),
        Path("resources/bin/static/app/app-audio-playback.js"),
        Path("static/app/app-audio-playback.js"),
        Path("bin/static/app/app-audio-playback.js"),
        Path("N.E.K.O/portable/resources/bin/static/app/app-audio-playback.js"),
        Path("N.E.K.O/resources/bin/static/app/app-audio-playback.js"),
    ]
    bases = [Path.cwd()]
    here = Path(__file__).resolve()
    bases.extend(here.parents[:6])
    for base in bases:
        for rel in relative:
            paths.append(base / rel)
    return paths


def find_playback_js(extra: str | None = None) -> Path | None:
    seen: set[Path] = set()
    for path in _candidate_paths(extra):
        try:
            key = path.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        if key.is_file():
            return key
    return None


def _strip_legacy(text: str) -> str:
    """移除早期无标记的手工补丁，便于统一改写为新版带标记的补丁。"""
    start = text.find(LEGACY_HOOK_COMMENT)
    if start >= 0:
        line_start = text.rfind("\n", 0, start) + 1
        end = text.find(LEGACY_HOOK_END, start)
        if end >= 0:
            end_line = text.find("\n", end)
            end_line = len(text) if end_line < 0 else end_line + 1
            text = text[:line_start] + text[end_line:]

    header = text.find(LEGACY_TAP_HEADER)
    if header >= 0:
        line_start = text.rfind("\n", 0, header) + 1
        close = text.find("\n})();", header)
        if close >= 0:
            end_line = text.find("\n", close + 1)
            end_line = len(text) if end_line < 0 else end_line + 1
            text = text[:line_start] + text[end_line:]
    return text


def ensure_patch(path: Path) -> str:
    """返回 patched / already / hook-missing / failed:<reason>。"""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return f"failed:read:{exc}"
    try:
        text = raw.decode("utf-8-sig")
        had_bom = raw.startswith(b"\xef\xbb\xbf")
    except UnicodeDecodeError:
        return "failed:decode"

    changed = False
    stripped = _strip_legacy(text)
    if stripped != text:
        text = stripped
        changed = True
    if HOOK_MARK not in text:
        anchor = "var arrayBuffer = await blob.arrayBuffer();"
        index = text.find(anchor)
        if index < 0:
            return "hook-missing"
        insert_at = index + len(anchor)
        # 保留该行结尾换行
        newline = text.find("\n", insert_at)
        insert_at = newline + 1 if newline >= 0 else len(text)
        text = text[:insert_at] + HOOK_LINE + text[insert_at:]
        changed = True

    if BEGIN_MARK in text and END_MARK in text:
        start = text.find(BEGIN_MARK)
        end = text.find(END_MARK, start)
        line_start = text.rfind("\n", 0, start) + 1
        end_line = text.find("\n", end)
        end_line = len(text) if end_line < 0 else end_line + 1
        if text[line_start:end_line].strip() != TAP_BLOCK.strip():
            # 旧版本补丁：整块替换为新版本
            text = text[:line_start] + TAP_BLOCK + text[end_line:]
            changed = True
    elif TAIL_MARK not in text:
        text = text.rstrip() + "\n" + TAP_BLOCK
        changed = True

    if not changed:
        return "already"

    backup = path.with_name(path.name + ".neko-passport.bak")
    if not backup.exists():
        try:
            backup.write_bytes(raw)
        except OSError:
            pass
    data = text.encode("utf-8")
    if had_bom:
        data = b"\xef\xbb\xbf" + data
    try:
        path.write_bytes(data)
    except OSError as exc:
        return f"failed:write:{exc}"
    return "patched"


def patch_frontend(extra_path: str | None = None) -> str:
    path = find_playback_js(extra_path)
    if path is None:
        logger.warning("NEKO Passport: N.E.K.O 前端音频文件未找到，需手动配置 frontend_js_path")
        return "not-found"
    result = ensure_patch(path)
    if result == "patched":
        logger.info("NEKO Passport: 已注入音频镜像钩子 -> %s", path)
    elif result == "already":
        logger.info("NEKO Passport: 音频镜像钩子已存在 -> %s", path)
    else:
        logger.warning("NEKO Passport: 音频镜像钩子注入失败(%s) -> %s", result, path)
    return result


def _clean_text(text: str) -> str:
    """返回移除全部 NEKO Passport 注入内容后的文本。"""
    text = _strip_legacy(text)
    while BEGIN_MARK in text and END_MARK in text:
        start = text.find(BEGIN_MARK)
        end = text.find(END_MARK, start)
        line_start = text.rfind("\n", 0, start) + 1
        end_line = text.find("\n", end)
        end_line = len(text) if end_line < 0 else end_line + 1
        text = text[:line_start] + text[end_line:]

    kept = [
        line
        for line in text.splitlines(keepends=True)
        if HOOK_MARK not in line and line.strip() != HOOK_COMMENT
    ]
    return "".join(kept)


def _cleaned_bytes(path: Path) -> tuple[bytes | None, str]:
    """读取并清理前端文件；未注入时返回 (None, 'clean')。"""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"failed:read:{exc}"
    try:
        text = raw.decode("utf-8-sig")
        had_bom = raw.startswith(b"\xef\xbb\xbf")
    except UnicodeDecodeError:
        return None, "failed:decode"

    cleaned = _clean_text(text)
    if cleaned == text:
        return None, "clean"
    data = cleaned.encode("utf-8")
    if had_bom:
        data = b"\xef\xbb\xbf" + data
    return data, "unpatched"


def remove_patch(path: Path) -> str:
    """移除插件注入的钩子；返回 unpatched / clean / failed:<reason>。"""
    data, status = _cleaned_bytes(path)
    if data is None:
        return status
    try:
        path.write_bytes(data)
    except OSError as exc:
        return f"failed:write:{exc}"
    return status


def unpatch_frontend(extra_path: str | None = None) -> str:
    path = find_playback_js(extra_path)
    if path is None:
        return "not-found"
    result = remove_patch(path)
    if result == "unpatched":
        logger.info("NEKO Passport: 已移除音频镜像钩子 -> %s", path)
    return result


# 卸载清理由独立进程执行：插件退出后，只有插件目录被删除（卸载）才还原前端文件。
# 目录仍在（正常退出/重启/禁用）则保留钩子，避免下次启动时页面先于插件加载而拿不到钩子。
_WATCHER_PS1 = """
param(
    [string]$PluginDir,
    [string]$JsPath,
    [string]$CleanPath
)
Start-Sleep -Seconds 5
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    if (Test-Path -LiteralPath $PluginDir) { break }
    Start-Sleep -Seconds 3
}
if (-not (Test-Path -LiteralPath $PluginDir)) {
    try { Copy-Item -LiteralPath $CleanPath -Destination $JsPath -Force } catch {}
}
try { Remove-Item -LiteralPath $CleanPath -Force -ErrorAction SilentlyContinue } catch {}
try { Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue } catch {}
"""


_LAUNCHER_PS1 = """
param(
    [string]$WatcherPath,
    [string]$PluginDir,
    [string]$JsPath,
    [string]$CleanPath
)
$cmd = 'powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $WatcherPath + '"'
$cmd += ' -PluginDir "' + $PluginDir + '"'
$cmd += ' -JsPath "' + $JsPath + '"'
$cmd += ' -CleanPath "' + $CleanPath + '"'
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
if ($r.ReturnValue -ne 0) { exit 1 }
"""


def _launch_watcher(plugin_dir: Path, js_path: Path) -> str:
    data, status = _cleaned_bytes(js_path)
    if data is None:
        return status

    stem = f"neko_passport_uninstall_{os.getpid()}"
    tmp = Path(tempfile.gettempdir())
    clean_path = tmp / f"{stem}.js"
    script_path = tmp / f"{stem}.ps1"
    launcher_path = tmp / f"{stem}_launch.ps1"
    try:
        clean_path.write_bytes(data)
        script_path.write_text(_WATCHER_PS1, encoding="ascii")
        launcher_path.write_text(_LAUNCHER_PS1, encoding="ascii")
        # N.E.K.O 用 Job Object 管理插件进程，插件退出时其子进程会被一并结束。
        # 通过 WMI(Win32_Process.Create) 启动清理进程，使其由 WMI 服务派生，脱离插件 Job。
        completed = subprocess.run(
            [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-WindowStyle", "Hidden", "-File", str(launcher_path),
                "-WatcherPath", str(script_path),
                "-PluginDir", str(plugin_dir),
                "-JsPath", str(js_path),
                "-CleanPath", str(clean_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        if completed.returncode != 0:
            return f"failed:launcher-exit-{completed.returncode}"
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("NEKO Passport: 卸载清理进程启动失败: %s", exc)
        return f"failed:{exc}"
    finally:
        try:
            launcher_path.unlink(missing_ok=True)
        except OSError:
            pass
    logger.info("NEKO Passport: 卸载清理进程已启动，仅在插件目录被删除后还原前端文件")
    return "armed"


def spawn_uninstall_watcher(extra_path: str | None = None) -> str:
    """插件退出时调用：启动独立清理进程，仅在确认插件被卸载后还原前端文件。"""
    path = find_playback_js(extra_path)
    if path is None:
        return "not-found"
    return _launch_watcher(Path(__file__).resolve().parent, path)


def patch_status(extra_path: str | None = None) -> dict[str, object]:
    path = find_playback_js(extra_path)
    if path is None:
        return {"found": False, "path": None, "patched": False}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {"found": True, "path": str(path), "patched": False}
    return {
        "found": True,
        "path": str(path),
        "patched": HOOK_MARK in text and TAIL_MARK in text,
    }


__all__ = [
    "ensure_patch",
    "find_playback_js",
    "patch_frontend",
    "patch_status",
    "remove_patch",
    "spawn_uninstall_watcher",
    "unpatch_frontend",
]
