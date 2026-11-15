"""Build-time packaging of the Duo manager and its portable Python only.

Inputs are software/runtime bundles only. Never reads a user Codex/CC Switch
home, credentials, signing passwords, or a live multi-instance workspace.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess

from local_signing import sign_app


MACHO = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
         b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return result.stdout


def package_python(source: Path, destination: Path, identity: str) -> None:
    source = source.resolve()
    if not (source / "bin/python3").is_file():
        raise ValueError("--python-runtime must contain a portable bin/python3 and lib directory")
    if destination.exists():
        shutil.rmtree(destination)
    # The application uses only stdlib. Do not distribute the build host's
    # third-party packages or mutable import caches.
    shutil.copytree(source, destination, symlinks=True,
                    ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc",
                                                 "include", "pkgconfig", "man"))
    for path in destination.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open("rb") as stream:
            magic = stream.read(4)
        if magic not in MACHO:
            continue
        libraries = run(["/usr/bin/otool", "-L", str(path)]).splitlines()[1:]
        for line in libraries:
            library = line.strip().split(" (", 1)[0]
            if library.startswith("/") and not library.startswith(("/usr/lib/", "/System/Library/")):
                raise ValueError("Portable Python depends on an external library: " + library)
        sign_app(path, deep=False, identity=identity)
    executable = destination / "bin/python3"
    probe = run([str(executable), "-I", "-B", "-c",
                 "import sys,sqlite3,ctypes,tomllib,ssl,zlib,hashlib;"
                 "assert sys.version_info >= (3,11);print(sys.prefix)"])
    if Path(probe.strip()).resolve() != destination.resolve():
        raise ValueError("Packaged Python is not relocatable")
    if not list(destination.glob("lib/python*/LICENSE.txt")):
        raise ValueError("Portable Python license is missing")


def package_launcher(app: Path, identity: str) -> None:
    resources = app / 'Contents/Resources'
    launcher = Path(__file__).with_name('duo-instance-launcher')
    sign_app(launcher, deep=False, identity=identity)
    shutil.copyfile(launcher, resources / 'source/duo-instance-launcher')
    # Rebuilding an old manager must remove its formerly bundled engine.
    desktop = resources / 'desktop'
    if desktop.exists():
        shutil.rmtree(desktop)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--python-runtime", type=Path, required=True)
    parser.add_argument("--sign-identity", default="-")
    args = parser.parse_args()
    package_python(args.python_runtime, args.app / "Contents/Resources/python", args.sign_identity)
    package_launcher(args.app, args.sign_identity)
    # All nested code is already signed. Seal the manager without any Codex engine.
    sign_app(args.app, deep=False, identity=args.sign_identity)
    print("Packaged Codex Duo manager and portable Python; uses the locally installed native Codex at runtime.")


if __name__ == "__main__":
    main()
