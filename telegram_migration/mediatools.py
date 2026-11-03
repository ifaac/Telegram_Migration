"""Optional external tooling: hachoir metadata, ffmpeg/ffprobe discovery and probing."""

import json.
import os
import shutil
import subprocess
import sys


# These paths are set by ensure_ffmpeg_tools(). Using explicit paths avoids relying
# on an updated PATH in notebook kernels that were started before installation.
_FFMPEG_PATH = shutil.which("ffmpeg")


_FFPROBE_PATH = shutil.which("ffprobe")


_FFMPEG_SETUP_ATTEMPTED = False


_FFMPEG_SETUP_OK = False


def _tool_works(path, timeout=12):
    """Return True if an FFmpeg executable starts successfully."""
    if not path or not os.path.isfile(path) and not shutil.which(path):
        return False
    try:
        result = subprocess.run(
            [path, "-version"], capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _quiet_pip_install(package, timeout=300):
    """Install a Python package non-interactively, capturing noisy output."""
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", package]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "pip returned a non-zero exit code").strip()
        raise RuntimeError(detail[-1200:])


def enable_hachoir():
    """Make sure Telethon can read duration/dimensions of videos, voice notes and round videos.

    Telethon uses hachoir for that, but only if it was importable when Telethon
    itself was imported. After installing it here, hand the module to Telethon.
    """
    try:
        import hachoir  # noqa: F401
        import hachoir.metadata  # noqa: F401
        import hachoir.parser  # noqa: F401
    except ImportError:
        try:
            print("Installing hachoir (media duration/size metadata)...")
            _quiet_pip_install("hachoir")
            import hachoir  # noqa: F401
            import hachoir.metadata  # noqa: F401
            import hachoir.parser  # noqa: F401
        except Exception as exc:
            print(f"WARNING: hachoir is unavailable ({type(exc).__name__}: {exc}).")
            print("  Voice notes / videos may show no duration. Try: pip install hachoir, then restart the runtime.")
            return False
    try:
        import telethon.utils as _tu
        if getattr(_tu, "hachoir", None) is None:
            _tu.hachoir = sys.modules["hachoir"]
    except Exception:
        pass
    return True


def ensure_ffmpeg_tools():
    """Ensure both ffmpeg and ffprobe are available, installing them quietly if needed.

    First uses working system binaries. Otherwise installs the `static-ffmpeg`
    Python package, which fetches platform-specific ffmpeg/ffprobe binaries without
    requiring administrator rights on supported platforms. This is suitable for
    Google Colab and common Windows/macOS/Linux Python environments.
    """
    global _FFMPEG_PATH, _FFPROBE_PATH, _FFMPEG_SETUP_ATTEMPTED, _FFMPEG_SETUP_OK
    if _FFMPEG_SETUP_ATTEMPTED:
        return _FFMPEG_SETUP_OK
    _FFMPEG_SETUP_ATTEMPTED = True

    candidate_ffmpeg = shutil.which("ffmpeg") or _FFMPEG_PATH
    candidate_ffprobe = shutil.which("ffprobe") or _FFPROBE_PATH
    if _tool_works(candidate_ffmpeg) and _tool_works(candidate_ffprobe):
        _FFMPEG_PATH, _FFPROBE_PATH = candidate_ffmpeg, candidate_ffprobe
        _FFMPEG_SETUP_OK = True
        print("FFmpeg/ffprobe ready (existing installation).")
        return True

    print("FFmpeg/ffprobe not found; setting them up automatically in the background...")
    errors = []
    try:
        _quiet_pip_install("static-ffmpeg")
        import static_ffmpeg

        # The package downloads its platform binaries on first use. This call is
        # intentionally non-interactive and can take a little while on first run.
        try:
            ffmpeg_path, ffprobe_path = static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()
        except AttributeError:
            # Compatibility with package releases that expose add_paths but not
            # the path-returning helper documented by current releases.
            static_ffmpeg.add_paths()
            ffmpeg_path, ffprobe_path = shutil.which("ffmpeg"), shutil.which("ffprobe")

        if not (_tool_works(ffmpeg_path) and _tool_works(ffprobe_path)):
            raise RuntimeError("the downloaded FFmpeg executables did not pass their version checks")

        _FFMPEG_PATH, _FFPROBE_PATH = ffmpeg_path, ffprobe_path
        # Also expose the tools to child processes and any helper code that uses PATH.
        tool_dir = os.path.dirname(ffmpeg_path)
        if tool_dir and tool_dir not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = tool_dir + os.pathsep + os.environ.get("PATH", "")
        _FFMPEG_SETUP_OK = True
        print("FFmpeg/ffprobe ready (automatic setup succeeded).")
        return True
    except Exception as exc:
        errors.append(f"static-ffmpeg setup: {type(exc).__name__}: {exc}")

    # Colab/Linux fallback: when running with sufficient privilege, use the OS
    # package manager. The command is suppressed unless it fails; it never asks
    # for an interactive password.
    apt_get = shutil.which("apt-get")
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    if apt_get and is_root:
        try:
            update = subprocess.run(
                [apt_get, "update", "-qq"], capture_output=True, text=True,
                timeout=240, check=False,
            )
            install = subprocess.run(
                [apt_get, "install", "-y", "-qq", "ffmpeg"], capture_output=True,
                text=True, timeout=300, check=False,
            ) if update.returncode == 0 else update
            candidate_ffmpeg = shutil.which("ffmpeg")
            candidate_ffprobe = shutil.which("ffprobe")
            if install.returncode == 0 and _tool_works(candidate_ffmpeg) and _tool_works(candidate_ffprobe):
                _FFMPEG_PATH, _FFPROBE_PATH = candidate_ffmpeg, candidate_ffprobe
                _FFMPEG_SETUP_OK = True
                print("FFmpeg/ffprobe ready (system package setup succeeded).")
                return True
            detail = (install.stderr or install.stdout or update.stderr or update.stdout or "package-manager installation failed").strip()
            errors.append(f"apt-get setup: {detail[-700:]}")
        except Exception as exc:
            errors.append(f"apt-get setup: {type(exc).__name__}: {exc}")

    _FFMPEG_SETUP_OK = False
    print("WARNING: automatic FFmpeg/ffprobe setup failed. Video stickers may use fallback metadata.")
    for error in errors:
        print("  " + error)
    print("You can install FFmpeg manually from https://ffmpeg.org/download.html and restart the runtime.")
    return False


def probe_video_metadata(path):
    """Return width, height and duration from ffprobe, or None if probing fails.

    FFmpeg setup is lazy: this helper is only called for video-sticker metadata,
    so text/photo-only migrations never install FFmpeg packages or binaries.
    """
    ffprobe_path = shutil.which("ffprobe") or _FFPROBE_PATH
    if not ffprobe_path:
        ensure_ffmpeg_tools()
        ffprobe_path = shutil.which("ffprobe") or _FFPROBE_PATH
    if not ffprobe_path:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe_path, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration:format=duration",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if result.returncode != 0:
            return None
        info = json.loads(result.stdout or "{}")
        streams = info.get("streams") or []
        stream = streams[0] if streams else {}
        fmt = info.get("format") or {}
        duration = stream.get("duration", fmt.get("duration"))
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        seconds = float(duration) if duration not in (None, "N/A", "") else 0.0
        if width <= 0 or height <= 0 or seconds <= 0:
            return None
        return {"width": width, "height": height, "duration": seconds}
    except Exception:
        return None


def video_has_audio(path):
    """True / False when an already-installed ffprobe can tell, None when it cannot.

    Never installs anything: ordinary videos must not trigger an FFmpeg download.
    """
    ffprobe_path = shutil.which("ffprobe") or _FFPROBE_PATH
    if not ffprobe_path:
        return None
    try:
        result = subprocess.run(
            [ffprobe_path, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if result.returncode != 0:
            return None
        return bool(result.stdout.strip())
    except Exception:
        return None
