#!/usr/bin/env python3
"""voice-automute

While Claude (the Claude Code CLI or the Claude desktop app) is recording from the
microphone, mute every OTHER application's audio session so that PC sound does not
leak into voice dictation.  Everything is restored when the recording ends.

Scope is deliberately narrow:
  * It reacts ONLY to an ACTIVE microphone-capture session owned by a process whose
    image name is on the allow-list (default: claude.exe).  Discord, Chrome, OBS,
    games, ... can never trigger it.
  * It never touches the audio sessions of the allow-listed processes themselves
    (so Claude's own spoken replies keep playing).
  * It never reads, records or transmits audio data, opens no network connection and
    spawns no subprocess.

Crash safety: the names of the apps it muted are written to a small state file
*before* muting, so the next start (or `--restore`) can undo a mute left behind by a
crash or a forced kill.
"""
from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import time
from ctypes import POINTER, cast
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

import comtypes
import psutil
from comtypes import CLSCTX_ALL, COMError, CoCreateInstance
from pycaw.constants import CLSID_MMDeviceEnumerator
from pycaw.pycaw import (
    EDataFlow,
    IAudioSessionControl2,
    IAudioSessionManager2,
    IMMDeviceEnumerator,
    ISimpleAudioVolume,
)

APP_NAME = "voice-automute"
APP_DIR = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / APP_NAME
LOG_PATH = APP_DIR / "automute.log"
STATE_PATH = APP_DIR / "state.json"

DEFAULT_TARGETS = ("claude.exe",)
DEVICE_STATE_ACTIVE = 0x00000001
AUDIO_SESSION_STATE_ACTIVE = 1
ERROR_ALREADY_EXISTS = 183
EXE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,100}\.exe")  # --target only (use fullmatch)
MAX_STATE_BYTES = 64 * 1024
WARN_EVERY_S = 30.0

log = logging.getLogger(APP_NAME)


# --------------------------------------------------------------------------- helpers
def setup_logging(verbose: bool) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:  # log file locked / unwritable: keep running without file logging
        pass
    if sys.stderr is not None:  # pythonw.exe has no console
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def acquire_single_instance():
    """Named mutex so only one watcher runs per login session."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    handle = k32.CreateMutexW(None, False, "Local\\voice-automute-single-instance")
    if not handle or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return None
    return handle


def proc_name(pid: int) -> str:
    """Lower-case image name of a pid ('' for system sounds / vanished processes)."""
    if pid <= 0:
        return ""
    try:
        return psutil.Process(pid).name().lower()
    except (psutil.Error, OSError):
        return ""


def clean_names(raw) -> Set[str]:
    """Image names read back from the state file.  They are only ever compared with
    psutil names, never executed, so accept any plain string (incl. non-ASCII names)
    but nothing that looks like a path or contains control characters."""
    out: Set[str] = set()
    if isinstance(raw, list):
        for e in raw[:256]:
            if isinstance(e, str) and 0 < len(e) <= 260 and not any(c in e for c in "\\/:") \
                    and e.isprintable():
                out.add(e.lower())
    return out


def proc_path(pid: int) -> str:
    try:
        return psutil.Process(pid).exe()
    except (psutil.Error, OSError):
        return "?"


def iter_sessions(mgr) -> Iterator:
    enum = mgr.GetSessionEnumerator()
    for i in range(enum.GetCount()):
        yield enum.GetSession(i)


class Endpoints:
    """Caches the IAudioSessionManager2 of every active endpoint of one data flow."""

    def __init__(self, flow: int, refresh_s: float = 2.0) -> None:
        self.flow = flow
        self.refresh_s = refresh_s
        self._mgrs: Optional[List[Tuple[str, object]]] = None
        self._stamp = 0.0

    def invalidate(self) -> None:
        self._mgrs = None

    def managers(self) -> List[Tuple[str, object]]:
        now = time.monotonic()
        if self._mgrs is None or now - self._stamp >= self.refresh_s:
            self._mgrs = self._refresh()
            self._stamp = now
        return self._mgrs

    def _refresh(self) -> List[Tuple[str, object]]:
        out: List[Tuple[str, object]] = []
        enum = CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
        coll = enum.EnumAudioEndpoints(self.flow, DEVICE_STATE_ACTIVE)
        for i in range(coll.GetCount()):
            dev = coll.Item(i)
            try:
                iface = dev.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
                out.append((dev.GetId(), cast(iface, POINTER(IAudioSessionManager2))))
            except COMError as exc:
                log.debug("skip endpoint: %s", exc)
        return out


# --------------------------------------------------------------------------- watcher
class Watcher:
    def __init__(self, targets: Set[str], poll_s: float, release_s: float, max_mute_s: float, dry_run: bool) -> None:
        self.targets = targets
        self.poll_s = poll_s
        self.release_s = release_s
        self.max_mute_s = max_mute_s
        self.dry_run = dry_run
        self.capture = Endpoints(EDataFlow.eCapture.value)
        self.render = Endpoints(EDataFlow.eRender.value)
        # (endpoint id, session instance id) -> (ISimpleAudioVolume or None, label)
        self.tracked: Dict[Tuple[str, str], Tuple[object, str]] = {}
        self.skipped: Set[Tuple[str, str]] = set()  # sessions we must not touch (target / already muted)
        self.muted_names: Set[str] = set()
        self.muted_system = False  # the "System Sounds" session (pid 0) has no image name
        self.muted_since: Optional[float] = None
        self.capped = False
        self.stop = False
        self.recovery_pending = False
        self._last_warn: Dict[str, float] = {}

    # ---- detection ---------------------------------------------------------------
    def _target_recording(self) -> bool:
        for _dev_id, mgr in self.capture.managers():
            for ctl in iter_sessions(mgr):
                c2 = ctl.QueryInterface(IAudioSessionControl2)
                if c2.GetState() != AUDIO_SESSION_STATE_ACTIVE:
                    continue
                pid = c2.GetProcessId()
                if proc_name(pid) in self.targets:
                    if self.muted_since is None:
                        log.info("mic capture ACTIVE: pid=%d %s", pid, proc_path(pid))
                    return True
        return False

    # ---- muting ------------------------------------------------------------------
    def _mute_pass(self) -> None:
        newly: List[str] = []
        self._mute_sessions(newly)
        if newly:
            counts: Dict[str, int] = {}
            for label in newly:
                counts[label] = counts.get(label, 0) + 1
            log.info("%s %s", "would mute" if self.dry_run else "muted",
                     ", ".join(f"{k} x{v}" if v > 1 else k for k, v in sorted(counts.items())))

    def _mute_sessions(self, newly: List[str]) -> None:
        for dev_id, mgr in self.render.managers():
            try:
                sessions = list(iter_sessions(mgr))
            except COMError:
                self.render.invalidate()
                continue
            for ctl in sessions:
                try:
                    c2 = ctl.QueryInterface(IAudioSessionControl2)
                    key = (dev_id, c2.GetSessionInstanceIdentifier())
                    if key in self.tracked or key in self.skipped:
                        continue
                    pid = c2.GetProcessId()
                    name = proc_name(pid)
                    if name in self.targets or (pid > 0 and not name):
                        # target process, or a process we cannot identify (gone / no access).
                        # Never mute what crash recovery could not name again.
                        self.skipped.add(key)
                        continue
                    vol = ctl.QueryInterface(ISimpleAudioVolume)
                    if vol.GetMute():  # user already muted it: leave it exactly as it is
                        self.skipped.add(key)
                        continue
                    label = name or ("system-sounds" if pid == 0 else f"pid{pid}")
                    if self.dry_run:
                        self.tracked[key] = (None, label)
                        newly.append(label)
                        continue
                    changed = False
                    if name:
                        if name not in self.muted_names:
                            self.muted_names.add(name)
                            changed = True
                    elif pid == 0 and not self.muted_system:
                        self.muted_system = True
                        changed = True
                    if changed:
                        self._save_state()  # persist BEFORE muting (crash safety)
                    vol.SetMute(1, None)
                    self.tracked[key] = (vol, label)
                    newly.append(label)
                except COMError as exc:
                    log.debug("session skipped: %s", exc)

    def _restore(self, reason: str) -> None:
        count = 0
        for vol, _label in self.tracked.values():
            if vol is None:
                continue
            try:
                vol.SetMute(0, None)
                count += 1
            except COMError:
                pass  # the session is already gone
        held = time.monotonic() - self.muted_since if self.muted_since is not None else 0.0
        if self.tracked:
            log.info("restored %d session(s) after %.1fs (%s)", count, held, reason)
        self.tracked.clear()
        self.skipped.clear()
        self.muted_names.clear()
        self.muted_system = False
        self.muted_since = None
        self._clear_state()

    # ---- state file --------------------------------------------------------------
    def _save_state(self) -> None:
        """Atomically persist what we are about to mute.  Raises on failure, which makes the
        caller skip the mute (fail safe: never mute something we could not record)."""
        APP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        payload = json.dumps({"exes": sorted(self.muted_names), "system_sounds": self.muted_system})
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, STATE_PATH)

    @staticmethod
    def _clear_state() -> None:
        try:
            STATE_PATH.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # locked by AV / another process: try again at the next start
            log.warning("could not remove state file: %s", exc)

    def recover(self) -> bool:
        """Undo a mute that a previous (crashed / killed) run left behind.
        Returns False when it must be retried (audio service not reachable yet)."""
        try:
            if STATE_PATH.stat().st_size > MAX_STATE_BYTES:
                raise ValueError("state file too large")
            data = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))  # tolerate a BOM
            exes = clean_names(data["exes"])
            system_sounds = data.get("system_sounds") is True
        except FileNotFoundError:
            return True
        except Exception:  # corrupt / hostile content (incl. RecursionError, MemoryError)
            log.warning("ignoring unreadable state file")
            self._clear_state()
            return True
        restored = 0
        try:
            self.render.invalidate()
            for _dev_id, mgr in self.render.managers():
                for ctl in iter_sessions(mgr):
                    try:
                        c2 = ctl.QueryInterface(IAudioSessionControl2)
                        pid = c2.GetProcessId()
                        if proc_name(pid) in exes or (system_sounds and pid == 0):
                            vol = ctl.QueryInterface(ISimpleAudioVolume)
                            if vol.GetMute():
                                vol.SetMute(0, None)
                                restored += 1
                    except COMError:
                        continue
        except (COMError, OSError) as exc:
            self._warn("recovery postponed (audio service not ready)", exc)
            return False  # keep the state file; run() retries
        log.info("recovery: unmuted %d leftover session(s) of %s%s", restored, sorted(exes),
                 " + system sounds" if system_sounds else "")
        self._clear_state()
        return True

    def _warn(self, what: str, exc: BaseException) -> None:
        """Rate-limited warning so a persistent device problem cannot flood the log."""
        now = time.monotonic()
        if now - self._last_warn.get(what, -1e9) >= WARN_EVERY_S:
            self._last_warn[what] = now
            log.warning("%s: %s: %s", what, type(exc).__name__, exc)

    # ---- main loop ---------------------------------------------------------------
    def run(self) -> None:
        log.info("watching for mic capture by %s (poll %.0fms, release %.0fms, cap %.0fs%s)",
                 sorted(self.targets), self.poll_s * 1000, self.release_s * 1000, self.max_mute_s,
                 ", DRY-RUN" if self.dry_run else "")
        last_active = 0.0
        next_recovery = 0.0
        while not self.stop:
            now = time.monotonic()
            # Any failure below (device unplugged, Bluetooth profile switch, audio service
            # restart, locked file...) must never kill the watcher: log, reset, back off.
            try:
                recording = self._target_recording()
            except Exception as exc:
                self._warn("capture poll failed", exc)
                self.capture.invalidate()
                recording = False
            try:
                if recording:
                    last_active = now
                    if not self.capped:
                        if self.muted_since is None:
                            self.muted_since = now
                        self._mute_pass()
                        if now - self.muted_since > self.max_mute_s:
                            log.warning("mic still active after %.0fs: restoring audio until the next recording",
                                        self.max_mute_s)
                            self._restore("safety cap")
                            self.capped = True
                else:
                    self.capped = False
                    if self.muted_since is not None and now - last_active >= self.release_s:
                        self._restore("recording ended")
                    elif self.recovery_pending and now >= next_recovery:
                        next_recovery = now + 5.0
                        self.recovery_pending = not self.recover()
            except Exception as exc:
                self._warn("mute/restore pass failed", exc)
                self.render.invalidate()
                time.sleep(0.25)
            time.sleep(self.poll_s)

    def shutdown(self) -> None:
        if self.muted_since is not None or self.tracked:
            self._restore("shutdown")


# --------------------------------------------------------------------------- entry point
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mute other apps while Claude is recording from the microphone.")
    p.add_argument("--target", action="append", metavar="EXE",
                   help="image name that may trigger muting (repeatable; default: claude.exe)")
    p.add_argument("--poll-ms", type=int, default=40, help="polling interval (default 40)")
    p.add_argument("--release-ms", type=int, default=300, help="wait this long after recording stops before unmuting (default 300)")
    p.add_argument("--max-mute-sec", type=int, default=300, help="safety cap for one continuous mute (default 300)")
    p.add_argument("--dry-run", action="store_true", help="log what would be muted but change nothing")
    p.add_argument("--restore", action="store_true", help="undo a leftover mute from a previous crash and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    targets = args.target or list(DEFAULT_TARGETS)
    for t in targets:
        if not EXE_RE.fullmatch(t):
            p.error(f"--target must be a plain executable name like claude.exe, got {t!r}")
    args.targets = {t.lower() for t in targets}
    args.poll_ms = min(max(args.poll_ms, 10), 1000)
    args.release_ms = min(max(args.release_ms, 0), 5000)
    args.max_mute_sec = min(max(args.max_mute_sec, 5), 3600)
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    # --restore is a one-shot "unmute what a dead run left behind"; it must work even if the
    # single-instance mutex is held (by a live watcher, or squatted by another process).
    mutex = None if args.restore else acquire_single_instance()
    if mutex is None and not args.restore:
        log.info("another instance is already running; exiting")
        return 0
    comtypes.CoInitialize()
    watcher = Watcher(args.targets, args.poll_ms / 1000, args.release_ms / 1000, args.max_mute_sec, args.dry_run)

    def _stop(_signum, _frame):
        watcher.stop = True

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop)
    atexit.register(watcher.shutdown)

    try:
        recovered = watcher.recover()
        if args.restore:
            return 0 if recovered else 1
        watcher.recovery_pending = not recovered
        watcher.run()
    except Exception:  # keep the reason in the log of a windowless process
        log.exception("fatal error")
        return 1
    finally:
        watcher.shutdown()
        # drop COM references before uninitialising COM
        watcher.capture.invalidate()
        watcher.render.invalidate()
        watcher.tracked.clear()
    return 0


if __name__ == "__main__":
    sys.exit(main())
