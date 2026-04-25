"""Build a single-file Windows .exe with PyInstaller.

Usage (from repo root):

    python scripts/build.py            # build
    python scripts/build.py --clean    # nuke build/ and dist/ first

Outputs ``build/dist/LilWin-Bros-agent.exe`` plus a default ``settings.json`` and a
``characters.json`` alongside the exe so first-run works without the repo.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
CONFIG = ROOT / "config"
DIST_DIR = ROOT / "build" / "dist"
WORK_DIR = ROOT / "build" / "work"
SPEC_DIR = ROOT / "build"
VERSION_FILE = ROOT / "build" / "version.txt"


def _read_version() -> str:
    ns: dict = {}
    exec((SRC / "__version__.py").read_text(encoding="utf-8"), ns)
    return str(ns.get("VERSION", "0.0.0"))


def _write_version_resource(version: str) -> None:
    """Generate a Windows VERSIONINFO resource PyInstaller can embed via --version-file."""
    parts = [int(p) for p in (version.split(".") + ["0", "0", "0", "0"])[:4]]
    while len(parts) < 4:
        parts.append(0)
    v_tuple = ", ".join(str(p) for p in parts)
    body = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({v_tuple}),
    prodvers=({v_tuple}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          u'040904B0',
          [
            StringStruct(u'CompanyName', u'LilWin'),
            StringStruct(u'FileDescription', u'LilWin'),
            StringStruct(u'FileVersion', u'{version}'),
            StringStruct(u'InternalName', u'LilWin'),
            StringStruct(u'OriginalFilename', u'LilWin.exe'),
            StringStruct(u'ProductName', u'LilWin'),
            StringStruct(u'ProductVersion', u'{version}'),
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"""
    VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    VERSION_FILE.write_text(body, encoding="utf-8")


def _pyinstaller_argv(version: str) -> list[str]:
    sep = ";" if os.name == "nt" else ":"
    assets = SRC / "assets"
    characters = CONFIG / "characters.json"
    settings = CONFIG / "settings.json"
    # Windows: use a real .ico for --icon. PNG breaks VERSIONINFO / manifest embedding and
    # causes "ordinal 380" (LoadIconMetric / comctl32 v6) at startup (PyInstaller #6845).
    icon = assets / "menuicon.ico"

    argv = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--noupx",
        "--name", "LilWin",
        "--distpath", str(DIST_DIR),
        "--workpath", str(WORK_DIR),
        "--specpath", str(SPEC_DIR),
        "--paths", str(SRC),
        "--add-data", f"{assets}{sep}assets",
        "--add-data", f"{characters}{sep}config",
        "--add-data", f"{settings}{sep}config",
    ]
    if icon.is_file() and icon.suffix.lower() == ".ico":
        argv += ["--icon", str(icon)]
    if os.name == "nt":
        _write_version_resource(version)
        argv += ["--version-file", str(VERSION_FILE)]
    # Hidden imports PyInstaller sometimes misses:
    for mod in ("websocket", "cryptography", "PyQt6.QtMultimedia"):
        argv += ["--hidden-import", mod]
    argv.append(str(SRC / "main.py"))
    return argv


def _copy_default_settings() -> None:
    """Drop a starter settings.json next to the exe so first launch has sane defaults."""
    src_settings = CONFIG / "settings.json"
    if not src_settings.is_file():
        return
    dest = DIST_DIR / "settings.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_settings, dest)


def _clean() -> None:
    for d in (DIST_DIR, WORK_DIR):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    for spec in SPEC_DIR.glob("*.spec"):
        spec.unlink()
    if VERSION_FILE.exists():
        VERSION_FILE.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build LilWin.exe")
    parser.add_argument("--clean", action="store_true", help="Wipe build outputs before building")
    parser.add_argument("--dry-run", action="store_true", help="Print the PyInstaller command and exit")
    args = parser.parse_args()

    version = _read_version()
    print(f"[build] LilWin v{version}")

    if args.clean:
        print("[build] cleaning build/ and dist/")
        _clean()

    argv = _pyinstaller_argv(version)
    print("[build] command:", " ".join(argv))
    if args.dry_run:
        return 0

    try:
        subprocess.run(argv, check=True, cwd=str(ROOT))
    except FileNotFoundError:
        print("[build] PyInstaller not found. Run `pip install -r requirements.txt` first.", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"[build] PyInstaller failed (exit {exc.returncode})", file=sys.stderr)
        return exc.returncode

    _copy_default_settings()
    exe = DIST_DIR / ("LilWin.exe" if os.name == "nt" else "LilWin")
    print(f"[build] done: {exe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
