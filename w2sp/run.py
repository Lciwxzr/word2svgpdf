# -*- coding: utf-8 -*-
"""Launcher: start the server on a free port and open the browser.

Why this exists
---------------
Windows reserves whole TCP ranges for Hyper-V / WSL / Docker. On this machine
8672-8771 is reserved, so the original fixed port 8770 failed to bind with

    [Errno 13] an attempt was made to access a socket in a way forbidden
    by its access permissions

A batch file cannot easily read a line the server prints, so this small
launcher runs app.py, watches its output for the VECTORFIG_URL line, and
opens the browser on the real port. Ctrl+C stops the server.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))


def copy_to_clipboard(text: str) -> bool:
    """Put text on the Windows clipboard without extra dependencies.

    Used when the browser cannot be launched: if Edge (or another browser) is
    already running with elevated privileges, a normal-privilege process
    cannot hand it a URL and Windows shows

        "Microsoft Edge isn't responding because an existing instance is
         running with elevated privileges"

    Copying the address means the user just presses Ctrl+V instead of
    selecting and copying it by hand.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # Return types matter: GlobalAlloc returns a HANDLE, and ctypes'
        # default (signed 32-bit int) truncates it -- GlobalLock then gets a
        # bogus pointer and the write faults. Declaring the prototypes fixes
        # that.
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE

        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not h:
                return False
            ptr = kernel32.GlobalLock(h)
            if not ptr:
                return False
            try:
                ctypes.memmove(ptr, ctypes.create_unicode_buffer(text), size)
            finally:
                kernel32.GlobalUnlock(h)
            # ownership of h passes to the clipboard on success
            return bool(user32.SetClipboardData(CF_UNICODETEXT, h))
        finally:
            user32.CloseClipboard()
    except Exception:
        return False


def is_elevated() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def open_browser(url: str) -> bool:
    """Open the URL in the user's real browser.

    `webbrowser.open` is not enough on Windows: when the default browser is
    a Store app or the association is broken, it silently does nothing. And
    when the browser is already running ELEVATED, a normal-privilege process
    cannot pass it a URL at all -- Windows pops up

        "Edge isn't responding because an existing instance is running with
         elevated privileges"

    So several strategies are tried, and the caller reports the real outcome.
    """
    import shutil

    if sys.platform == "win32":
        # 1) shell association, the closest thing to a double-click
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        except Exception:
            pass

        # 2) launch a browser binary directly, preferring one that is not
        #    the default (which may be the elevated Edge)
        for exe in ("chrome", "msedge", "firefox", "brave"):
            path = shutil.which(exe)
            if path:
                try:
                    subprocess.Popen([path, url], close_fds=True)
                    return True
                except Exception:
                    continue

        # 3) cmd's start
        try:
            subprocess.Popen(["cmd", "/c", "start", "", url],
                             shell=False, close_fds=True)
            return True
        except Exception:
            pass

    try:
        return webbrowser.open(url)
    except Exception:
        return False


def main() -> int:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")

    # Run the server as a module so the package's relative imports resolve.
    # cwd must be the directory that CONTAINS the package, not the package.
    project_root = os.path.dirname(HERE)
    proc = subprocess.Popen(
        [sys.executable, "-m", "w2sp"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        cwd=project_root,
    )

    opened = threading.Event()

    # Opening a browser automatically is opt-in. It kept causing trouble on
    # Windows (elevated Edge refuses a URL from a normal process, Store-app
    # associations silently do nothing), and a printed address is simpler to
    # rely on. Set VECTORFIG_OPEN_BROWSER=1 to bring the old behaviour back.
    want_browser = os.environ.get("VECTORFIG_OPEN_BROWSER", "").strip() in (
        "1", "true", "yes", "on",
    )

    def pump():
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("VECTORFIG_URL="):
                url = line.split("=", 1)[1].strip()
                print()
                print("   " + "-" * 54)
                print(f"   Open this address in your browser:")
                print(f"     {url}")
                print("   " + "-" * 54)
                if not opened.is_set():
                    opened.set()
                    if want_browser:
                        if open_browser(url):
                            print("   Browser opened.")
                        else:
                            copied = copy_to_clipboard(url)
                            print("   Could not open a browser automatically.")
                            if copied:
                                print("   The address is on your clipboard"
                                      " -- press Ctrl+V in the browser.")
                            else:
                                print("   Copy the address above by hand.")
                    else:
                        # no browser attempt; just make copying effortless
                        if copy_to_clipboard(url):
                            print("   (address copied to clipboard --"
                                  " press Ctrl+V in your browser)")
                print("   Close this window to stop.\n", flush=True)
            else:
                print(line, flush=True)

    t = threading.Thread(target=pump, daemon=True)
    t.start()

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    return proc.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
