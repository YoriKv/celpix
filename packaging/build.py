#!/usr/bin/env python3
"""Freeze celPix into a standalone app with PyInstaller.

This is the project's single build recipe: ``.github/workflows/release.yml``
runs this same script on each runner, so a local build is exactly what a
release ships — there is no second copy of the flags to drift out of sync.

    python3 packaging/build.py             # -> dist/celpix (or dist/celpix.app)
    python3 packaging/build.py --archive   # ... plus the release archive
    python3 packaging/build.py --appimage  # ... plus the AppImage (Linux only)
    python3 packaging/build.py --deb       # ... plus the native Debian package

PyInstaller freezes the interpreter it runs under, so the build always targets
the OS it runs on and there is no cross-compiling: build the Windows app from
Windows, the Linux app from Linux/WSL, the macOS app from macOS.

The script imports only the stdlib and drives ``uv run --with pyinstaller``, so
it needs no environment of its own — any Python 3.9+ plus ``uv`` on PATH will
do, including a bare system interpreter.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST = REPO_ROOT / "dist"
BUILD = REPO_ROOT / "build"
VERSION_FILE = REPO_ROOT / "src" / "celpix" / "__init__.py"
ENTRY_POINT = REPO_ROOT / "src" / "celpix" / "__main__.py"
APP_NAME = "celpix"
APP_ICON = REPO_ROOT / "src" / "celpix" / "resources" / "icons" / "app.png"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# The AppImage's desktop file and AppStream metadata live in packaging/linux/,
# both named after the reverse-DNS id — the name software centers key them by.
LINUX_PACKAGING = REPO_ROOT / "packaging" / "linux"
APP_ID = "io.github.yorikv.celpix"
# Pinned rather than "continuous", so a release is never built by an
# appimagetool nobody has tried yet.
APPIMAGETOOL_VERSION = "1.9.1"
APPIMAGETOOL_URL = (
    "https://github.com/AppImage/appimagetool/releases/download/"
    "{version}/appimagetool-{arch}.AppImage"
)

# The native Debian package runs the plain sources on the system's Python and
# PySide6, so its dependencies are pyproject's, spelled as Debian packages. Only
# the three Qt modules the app imports: Debian splits PySide6 per module.
# python3-pyside6 6.8 is Debian 13 (trixie)'s, which sets the floor; Ubuntu LTS
# releases up to 24.04 ship no PySide6 at all.
DEB_MAINTAINER = "Yori Kvitchko <yori.kvitchko@gmail.com>"
DEB_DEPENDS = ", ".join(
    [
        "python3 (>= 3.10)",
        # tomllib is stdlib from 3.11; tomli is its backport, as in pyproject.
        "python3 (>= 3.11) | python3-tomli",
        *(
            f"python3-pyside6.{mod} (>= 6.8)"
            for mod in ("qtcore", "qtgui", "qtwidgets")
        ),
    ]
)
# Without it Qt still runs on a Wayland session, through XWayland.
DEB_RECOMMENDS = "qt6-wayland"
DEB_DESCRIPTION = """\
Graphics viewer and editor for romhacking and research
 celPix finds, views and edits the graphics inside retro-game ROMs and dumps.
 It reads a wide range of pixel and palette formats, decompresses the common
 console compression schemes, and writes edited pixels and palettes back to
 the original file."""
DEB_PYTHON_DIR = f"usr/lib/python3/dist-packages/{APP_NAME}"

# PyInstaller stamps LSMinimumSystemVersion from the build Python's own
# deployment target (11.0 on arm64), but the app's real floor is Qt's: PySide6
# 6.10+ wheels are macosx_13_0. Stamping the honest minimum gets older systems a
# clear "requires macOS 13" dialog instead of a launch crash.
MACOS_MIN_VERSION = "13.0"


# ── Environment ──────────────────────────────────────────────────────────────
def build_env() -> dict:
    """The environment for ``uv``, with the WSL venv footgun defused.

    This checkout keeps two virtualenvs: ``.venv`` for Windows (the default
    name, so PyCharm auto-detects it) and ``.venv-linux`` for WSL. A bare ``uv
    run`` on the Linux side targets ``.venv`` and overwrites the Windows env
    with Linux binaries, so point uv at the Linux one unless the caller (or CI,
    where neither exists) has already chosen.
    """
    env = os.environ.copy()
    if sys.platform.startswith("linux") and "UV_PROJECT_ENVIRONMENT" not in env:
        if (REPO_ROOT / ".venv-linux").is_dir():
            env["UV_PROJECT_ENVIRONMENT"] = ".venv-linux"
    return env


def read_version() -> str:
    """``__version__`` from the package — the single source of truth."""
    match = re.search(
        r'^__version__\s*=\s*"([^"]+)"', VERSION_FILE.read_text(encoding="utf-8"), re.M
    )
    if not match:
        die(f"could not read __version__ from {VERSION_FILE}")
    return match.group(1)


# ── Target description ───────────────────────────────────────────────────────
def target_slug() -> str:
    """The name this platform's release asset is filed under."""
    if sys.platform == "win32":
        return "win"
    if sys.platform == "darwin":
        # uv's Python is single-arch, so a build is native-only; the two macOS
        # assets come from two runners, told apart by the machine they ran on.
        return "mac-arm64" if platform.machine() == "arm64" else "mac-intel"
    if sys.platform.startswith("linux"):
        return "linux"
    die(f"unsupported platform: {sys.platform}")


def icon_option() -> list:
    """``--icon`` arguments, if this platform has a build-time icon."""
    if sys.platform == "win32":
        # Embedded into the .exe and shown in Explorer/taskbar.
        return ["--icon", str(REPO_ROOT / "packaging" / "celpix.ico")]
    if sys.platform == "darwin":
        # Baked into celpix.app's Info.plist and shown in the Dock/Finder.
        return ["--icon", str(REPO_ROOT / "packaging" / "celpix.icns")]
    # Linux has no build-time icon; the app sets its window icon at runtime.
    return []


def app_path() -> Path:
    """What PyInstaller leaves in ``dist/`` for this platform."""
    if sys.platform == "darwin":
        return DIST / f"{APP_NAME}.app"
    return DIST / APP_NAME


def appimage_path() -> Path:
    """The AppImage asset. No version in the name: the embedded update
    information points at the latest release's asset of this exact name."""
    return DIST / f"{APP_NAME}-{platform.machine()}.AppImage"


def deb_path() -> Path:
    """The Debian package, under Debian's own ``name_version_arch`` scheme."""
    return DIST / f"{APP_NAME}_{read_version()}_all.deb"


def launcher_path() -> Path:
    """The executable a user actually double-clicks / runs."""
    if sys.platform == "darwin":
        return app_path() / "Contents" / "MacOS" / APP_NAME
    if sys.platform == "win32":
        return app_path() / f"{APP_NAME}.exe"
    return app_path() / APP_NAME


# ── Shell helpers ────────────────────────────────────────────────────────────
def info(message: str = "") -> None:
    print(message, flush=True)


def die(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def show(cmd: list) -> str:
    parts = [str(part) for part in cmd]
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def run(
    cmd: list,
    *,
    dry_run: bool,
    extra_env: dict | None = None,
    cwd: Path = REPO_ROOT,
) -> None:
    info(f"  $ {show(cmd)}")
    if dry_run:
        return
    env = build_env() | (extra_env or {})
    result = subprocess.run([str(part) for part in cmd], cwd=cwd, env=env)
    if result.returncode != 0:
        die(f"command failed with exit code {result.returncode}: {show(cmd)}")


# ── Build steps ──────────────────────────────────────────────────────────────
def pyinstaller_command() -> list:
    """The freeze command — identical on every platform bar the icon.

    ``--windowed`` suppresses the console window on Windows/macOS (a no-op on
    Linux). ``--collect-data`` is required because PyInstaller only follows
    imports: the package's data files (shipped presets, plugin examples, icons
    under ``celpix/resources``) would otherwise be left out and the frozen app
    would start without them. PySide6's bundled hooks pull in the Qt plugins.

    pyinstaller comes in ephemerally via ``--with``, so it need not live in the
    dev dependency group or uv.lock.
    """
    return [
        "uv",
        "run",
        "--with",
        "pyinstaller",
        "pyinstaller",
        "--name",
        APP_NAME,
        "--windowed",
        "--noconfirm",
        "--collect-data",
        "celpix",
        *icon_option(),
        str(ENTRY_POINT),
    ]


def stamp_macos_minimum(*, dry_run: bool) -> None:
    """Write the honest LSMinimumSystemVersion into the bundle, then re-sign.

    Editing Info.plist invalidates the ad-hoc signature PyInstaller applied, and
    arm64 refuses to launch an unsigned binary — so the two always go together.
    """
    plist_path = app_path() / "Contents" / "Info.plist"
    info(f"  Info.plist: LSMinimumSystemVersion = {MACOS_MIN_VERSION}")
    if not dry_run:
        with plist_path.open("rb") as handle:
            plist = plistlib.load(handle)
        plist["LSMinimumSystemVersion"] = MACOS_MIN_VERSION
        with plist_path.open("wb") as handle:
            plistlib.dump(plist, handle)
    run(["codesign", "--force", "--deep", "--sign", "-", app_path()], dry_run=dry_run)


def make_archive(*, dry_run: bool) -> Path:
    """Pack the built app into this platform's release asset."""
    slug = target_slug()
    if sys.platform == "darwin":
        archive = DIST / f"{APP_NAME}-{slug}.zip"
        # ditto, not zipfile: a .app is full of symlinks and executable bits
        # that a plain zip writer silently flattens, leaving an unlaunchable
        # bundle. --keepParent keeps celpix.app as the archive's top level.
        run(
            ["ditto", "-c", "-k", "--keepParent", app_path(), archive],
            dry_run=dry_run,
        )
        return archive

    if sys.platform == "win32":
        archive = DIST / f"{APP_NAME}-{slug}.zip"
        info(f"  zip {app_path().name}/* -> {archive.name}")
        if not dry_run:
            # Contents at the archive root, no wrapping folder — how the
            # published Windows asset has always unpacked.
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted(app_path().rglob("*")):
                    if path.is_file():
                        bundle.write(path, path.relative_to(app_path()).as_posix())
        return archive

    archive = DIST / f"{APP_NAME}-{slug}.tar.gz"
    info(f"  tar {app_path().name}/ -> {archive.name}")
    if not dry_run:
        # tarfile keeps symlinks as symlinks and preserves the exec bit, both of
        # which the Qt libraries in the onedir output need.
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(app_path(), arcname=APP_NAME)
    return archive


def release_date(version: str) -> str:
    """The date ``CHANGELOG.md`` gives this version, else today.

    ``release.sh`` stamps the heading before it tags, so a release build reads
    the release's own date; a build of unreleased work falls back to today.
    """
    match = re.search(
        rf"^## v{re.escape(version)} - (\d{{4}}-\d{{2}}-\d{{2}})\s*$",
        CHANGELOG.read_text(encoding="utf-8"),
        re.M,
    )
    return match.group(1) if match else datetime.date.today().isoformat()


def stamped_appdata() -> str:
    """The AppStream metadata with this version's ``<releases>`` entry added.

    Stamped at build time rather than kept in the XML, so a release never has
    to edit the metadata to keep it honest.
    """
    version = read_version()
    appdata = (LINUX_PACKAGING / f"{APP_ID}.appdata.xml").read_text(encoding="utf-8")
    releases = (
        f'  <releases>\n    <release version="{version}" '
        f'date="{release_date(version)}"/>\n  </releases>\n'
    )
    return appdata.replace("</component>", releases + "</component>")


def assemble_appdir(*, dry_run: bool) -> Path:
    """Lay the frozen app out as an AppDir for appimagetool.

    The top-level desktop file, icon, ``.DirIcon`` and executable ``AppRun`` are
    what the AppImage format requires; the copies under ``usr/share`` are where
    desktop integration and software catalogs look for them.
    """
    appdir = BUILD / "AppDir"
    info(f"  {app_path().relative_to(REPO_ROOT)} -> {appdir.relative_to(REPO_ROOT)}")
    if dry_run:
        return appdir
    if appdir.exists():
        shutil.rmtree(appdir)
    share = appdir / "usr" / "share"
    # Symlinks as symlinks: the onedir output relies on them, as for the tarball.
    shutil.copytree(app_path(), appdir / "usr" / "lib" / APP_NAME, symlinks=True)

    desktop = LINUX_PACKAGING / f"{APP_ID}.desktop"
    for target in (appdir, share / "applications"):
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(desktop, target / desktop.name)

    icon_dir = share / "icons" / "hicolor" / "256x256" / "apps"
    icon_dir.mkdir(parents=True)
    for target in (
        appdir / f"{APP_NAME}.png",
        appdir / ".DirIcon",
        icon_dir / f"{APP_NAME}.png",
    ):
        shutil.copy2(APP_ICON, target)

    # The legacy .appdata.xml name, not .metainfo.xml: it is the only one
    # appimagetool finds and the AppImage catalog's lint validates.
    (share / "metainfo").mkdir()
    (share / "metainfo" / f"{APP_ID}.appdata.xml").write_text(
        stamped_appdata(), encoding="utf-8"
    )

    apprun = appdir / "AppRun"
    # The AppImage runtime sets APPDIR; the fallback lets an extracted AppDir
    # run on its own too.
    apprun.write_text(
        "#!/bin/sh\n"
        'HERE="${APPDIR:-$(dirname "$(readlink -f "$0")")}"\n'
        f'exec "$HERE/usr/lib/{APP_NAME}/{APP_NAME}" "$@"\n',
        encoding="utf-8",
    )
    apprun.chmod(0o755)
    return appdir


def appimagetool(*, dry_run: bool) -> Path:
    """An ``appimagetool`` from PATH, else the pinned release, fetched once."""
    found = shutil.which("appimagetool")
    if found:
        return Path(found)
    arch = platform.machine()
    tool = BUILD / "tools" / f"appimagetool-{APPIMAGETOOL_VERSION}-{arch}.AppImage"
    if tool.exists():
        return tool
    url = APPIMAGETOOL_URL.format(version=APPIMAGETOOL_VERSION, arch=arch)
    info(f"  fetch {url}")
    if not dry_run:
        tool.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, tool)
        tool.chmod(0o755)
    return tool


def make_appimage(*, dry_run: bool) -> Path:
    """Pack the frozen app into a type-2 (SquashFS) AppImage, plus its .zsync."""
    appdir = assemble_appdir(dry_run=dry_run)
    output = appimage_path()
    run(
        [
            appimagetool(dry_run=dry_run),
            "--updateinformation",
            f"gh-releases-zsync|YoriKv|celpix|latest|{output.name}.zsync",
            appdir,
            output,
        ],
        dry_run=dry_run,
        # appimagetool is an AppImage itself, and neither CI runners nor WSL
        # can be counted on for FUSE to mount it; ARCH saves it guessing from
        # the bundled libraries.
        extra_env={"APPIMAGE_EXTRACT_AND_RUN": "1", "ARCH": platform.machine()},
        # The .zsync is written to the working directory, not beside the output.
        cwd=DIST,
    )
    return output


def deb_copyright() -> str:
    """``/usr/share/doc/celpix/copyright`` in Debian's machine-readable format.

    The bundled icon font is Apache-2.0, which Debian keeps in common-licenses;
    MIT is not there, so its text goes in whole.
    """
    license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    holder = re.search(r"^Copyright \(c\) (.+)$", license_text, re.M)
    if not holder:
        die("could not read the copyright line from LICENSE")
    # The body after the copyright line, as a control-file continuation: every
    # line indented one space, blank lines spelled " .".
    body = license_text[holder.end() :].strip("\n").splitlines()
    mit = "\n".join(f" {line}" if line.strip() else " ." for line in body)
    return (
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        "Upstream-Name: celPix\n"
        "Source: https://github.com/YoriKv/celpix\n"
        "\n"
        "Files: *\n"
        f"Copyright: {holder.group(1)}\n"
        "License: MIT\n"
        f"{mit}\n"
        "\n"
        f"Files: {DEB_PYTHON_DIR}/resources/fonts/*\n"
        "Copyright: Google LLC\n"
        "License: Apache-2.0\n"
        " On Debian systems, the full text of the Apache License 2.0 is in\n"
        " /usr/share/common-licenses/Apache-2.0.\n"
    )


def deb_payload() -> dict:
    """Everything the package installs, as ``{path: (bytes, mode)}``.

    The sources go into the system's dist-packages as an ordinary Python
    package, run by ``/usr/bin/celpix`` on the system interpreter. Modes are set
    here rather than read from disk, which on a mounted Windows drive reports
    every file as 0777.
    """
    payload = {}
    package = REPO_ROOT / "src" / APP_NAME
    for path in sorted(package.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            arcname = f"{DEB_PYTHON_DIR}/{path.relative_to(package).as_posix()}"
            payload[arcname] = (path.read_bytes(), 0o644)
    launcher = (
        "#!/usr/bin/python3\n"
        f"from {APP_NAME}.app import main\n"
        "\n"
        "raise SystemExit(main())\n"
    )
    payload[f"usr/bin/{APP_NAME}"] = (launcher.encode(), 0o755)
    desktop = LINUX_PACKAGING / f"{APP_ID}.desktop"
    payload[f"usr/share/applications/{desktop.name}"] = (desktop.read_bytes(), 0o644)
    payload[f"usr/share/icons/hicolor/256x256/apps/{APP_NAME}.png"] = (
        APP_ICON.read_bytes(),
        0o644,
    )
    payload[f"usr/share/metainfo/{APP_ID}.metainfo.xml"] = (
        stamped_appdata().encode(),
        0o644,
    )
    payload[f"usr/share/doc/{APP_NAME}/copyright"] = (deb_copyright().encode(), 0o644)
    return payload


def deb_control_files(payload: dict) -> dict:
    """The ``DEBIAN/`` side: control, md5sums and the maintainer scripts.

    The scripts do what dh_python3 would generate: byte-compile the package on
    install, since users cannot write ``__pycache__`` under /usr, and remove
    those caches before removal, or dpkg leaves the directories behind.
    """
    installed_kib = sum(-(-len(data) // 1024) for data, _mode in payload.values())
    control = (
        f"Package: {APP_NAME}\n"
        f"Version: {read_version()}\n"
        "Architecture: all\n"
        f"Maintainer: {DEB_MAINTAINER}\n"
        f"Installed-Size: {installed_kib}\n"
        f"Depends: {DEB_DEPENDS}\n"
        f"Recommends: {DEB_RECOMMENDS}\n"
        "Section: graphics\n"
        "Priority: optional\n"
        "Homepage: https://github.com/YoriKv/celpix\n"
        f"Description: {DEB_DESCRIPTION}\n"
    )
    md5sums = "".join(
        f"{hashlib.md5(data).hexdigest()}  {name}\n"
        for name, (data, _mode) in sorted(payload.items())
    )
    postinst = (
        "#!/bin/sh\nset -e\n"
        'if [ "$1" = configure ] && command -v py3compile >/dev/null 2>&1; then\n'
        f"    py3compile -p {APP_NAME}\n"
        "fi\n"
    )
    prerm = (
        "#!/bin/sh\nset -e\n"
        "if command -v py3clean >/dev/null 2>&1; then\n"
        f"    py3clean -p {APP_NAME}\n"
        "fi\n"
    )
    return {
        "control": (control.encode(), 0o644),
        "md5sums": (md5sums.encode(), 0o644),
        "postinst": (postinst.encode(), 0o755),
        "prerm": (prerm.encode(), 0o755),
    }


def tar_members(files: dict, mode: str, mtime: int) -> bytes:
    """A tarball of ``files`` as dpkg expects one: ``./``-relative paths, every
    parent directory listed before its contents, all owned by root."""
    dirs = {"."}
    for name in files:
        parts = name.split("/")[:-1]
        dirs.update("/".join(parts[: i + 1]) for i in range(len(parts)))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode, format=tarfile.GNU_FORMAT) as tar:
        # Sorting puts a directory ahead of everything under it.
        for name in sorted(dirs | files.keys()):
            member = tarfile.TarInfo("./" if name == "." else f"./{name}")
            member.uid = member.gid = 0
            member.uname = member.gname = "root"
            member.mtime = mtime
            if name in files:
                data, member.mode = files[name]
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
            else:
                member.type = tarfile.DIRTYPE
                member.mode = 0o755
                tar.addfile(member)
    return buffer.getvalue()


def ar_archive(members: list, mtime: int) -> bytes:
    """The ``ar`` container a .deb is: a magic line, then 60-byte headers each
    followed by its data, padded to an even length."""
    out = bytearray(b"!<arch>\n")
    for name, data in members:
        header = f"{name:<16}{mtime:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n"
        out += header.encode("ascii") + data
        if len(data) % 2:
            out += b"\n"
    return bytes(out)


def make_deb(*, dry_run: bool) -> Path:
    """Pack the sources into a native, architecture-independent Debian package.

    Written with the stdlib rather than dpkg-deb, so it builds the same on any
    host and filesystem. Unlike the AppImage it uses nothing PyInstaller made.
    """
    output = deb_path()
    info(f"  src/{APP_NAME} -> {output.relative_to(REPO_ROOT)}")
    if dry_run:
        return output
    # Every timestamp is the release date's, so one version's package does not
    # differ by when it was built. SOURCE_DATE_EPOCH, the reproducible-builds
    # convention, wins when set.
    mtime = int(
        os.environ.get("SOURCE_DATE_EPOCH")
        or datetime.datetime.fromisoformat(release_date(read_version()))
        .replace(tzinfo=datetime.timezone.utc)
        .timestamp()
    )
    payload = deb_payload()
    control = deb_control_files(payload)
    DIST.mkdir(parents=True, exist_ok=True)
    output.write_bytes(
        ar_archive(
            [
                ("debian-binary", b"2.0\n"),
                ("control.tar.gz", tar_members(control, "w:gz", mtime)),
                ("data.tar.xz", tar_members(payload, "w:xz", mtime)),
            ],
            mtime,
        )
    )
    return output


def clean(*, dry_run: bool) -> None:
    for path in (BUILD, DIST, REPO_ROOT / f"{APP_NAME}.spec"):
        if not path.exists():
            continue
        info(f"  rm -rf {path.relative_to(REPO_ROOT)}")
        if not dry_run:
            shutil.rmtree(path) if path.is_dir() else path.unlink()


# ── Entry point ──────────────────────────────────────────────────────────────
def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build.py",
        description=(
            "Freeze celPix into a standalone app with PyInstaller, the same way "
            "the release workflow does. Builds for the OS it runs on."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python3 packaging/build.py            build dist/celpix\n"
            "  python3 packaging/build.py -a         ... and the release archive\n"
            "  python3 packaging/build.py -a -i      ... and the AppImage (Linux)\n"
            "  python3 packaging/build.py -d         ... and the Debian package\n"
            "  python3 packaging/build.py -c -n      preview a from-scratch build\n"
        ),
    )
    parser.add_argument(
        "-a",
        "--archive",
        action="store_true",
        help="also pack the app into the release archive (zip / tar.gz) in dist/",
    )
    parser.add_argument(
        "-i",
        "--appimage",
        action="store_true",
        help="also pack the app into an AppImage in dist/ (Linux only)",
    )
    parser.add_argument(
        "-d",
        "--deb",
        action="store_true",
        help="also pack the sources into a native .deb in dist/ (system Python + Qt)",
    )
    parser.add_argument(
        "-c",
        "--clean",
        action="store_true",
        help="delete build/, dist/ and celpix.spec first, for a from-scratch build",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="show what would run; change nothing",
    )
    args = parser.parse_args(argv)

    if not shutil.which("uv"):
        die("uv not found in PATH — see https://docs.astral.sh/uv/ to install it")
    if not ENTRY_POINT.is_file():
        die(f"{ENTRY_POINT} not found; is this a full checkout?")
    if args.appimage and not sys.platform.startswith("linux"):
        die("--appimage builds on Linux only")

    slug = target_slug()
    env = build_env()
    info("Build plan:")
    info(f"  repo:     {REPO_ROOT}")
    info(f"  version:  {read_version()}")
    info(f"  target:   {slug} ({platform.platform()})")
    info(f"  output:   {app_path().relative_to(REPO_ROOT)}")
    if "UV_PROJECT_ENVIRONMENT" in env:
        info(f"  uv env:   {env['UV_PROJECT_ENVIRONMENT']}")
    info()

    if args.clean:
        info("Cleaning previous build output ...")
        clean(dry_run=args.dry_run)

    info("Freezing with PyInstaller ...")
    run(pyinstaller_command(), dry_run=args.dry_run)

    if sys.platform == "darwin":
        info("Stamping the minimum macOS version ...")
        stamp_macos_minimum(dry_run=args.dry_run)

    archive = None
    if args.archive:
        info("Packing the release archive ...")
        archive = make_archive(dry_run=args.dry_run)

    appimage = None
    if args.appimage:
        info("Packing the AppImage ...")
        appimage = make_appimage(dry_run=args.dry_run)

    deb = None
    if args.deb:
        info("Packing the Debian package ...")
        deb = make_deb(dry_run=args.dry_run)

    info()
    if args.dry_run:
        info("(dry run) nothing was built.")
        return 0
    info(f"Built {app_path().relative_to(REPO_ROOT)}")
    info(f"  run it: {launcher_path().relative_to(REPO_ROOT)}")
    if archive is not None:
        size_mb = archive.stat().st_size / 1_000_000
        info(f"  archive: {archive.relative_to(REPO_ROOT)} ({size_mb:.1f} MB)")
    if appimage is not None:
        size_mb = appimage.stat().st_size / 1_000_000
        info(f"  appimage: {appimage.relative_to(REPO_ROOT)} ({size_mb:.1f} MB)")
    if deb is not None:
        size_mb = deb.stat().st_size / 1_000_000
        info(f"  deb: {deb.relative_to(REPO_ROOT)} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
