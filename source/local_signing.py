"""Sign generated local wrappers without a certificate or private key.

Only the build-time packager may supply a publisher identity. Installed users
ad-hoc sign their generated copies of the local native app; signing never
creates a keychain or changes the user trust/search list.
"""
from __future__ import annotations
from pathlib import Path
import subprocess
import sys

class LocalSigningError(RuntimeError):
    pass

def _run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise LocalSigningError(result.stderr.strip() or f"{args[0]} failed")
    return result.stdout

def sign_app(app: Path, *, deep: bool = True, identifier: str | None = None,
             identity: str = "-") -> None:
    """Local callers use '-'; an explicit publisher identity is build-only."""
    _run(["/usr/bin/codesign", "--force", *(["--deep"] if deep else []),
          "--sign", identity, *(["--timestamp"] if identity != "-" else []),
          *(["--identifier", identifier] if identifier else []), str(app)])
    _run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])

if __name__ == "__main__":
    try:
        sign_app(Path(sys.argv[1]), identity=sys.argv[2] if len(sys.argv) > 2 else "-")
    except (IndexError, OSError, ValueError, subprocess.TimeoutExpired, LocalSigningError) as exc:
        print("Codex Duo signing failed: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
