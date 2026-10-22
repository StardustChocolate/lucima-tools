"""Create the local Android release keystore and its ignored Gradle config."""
from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ANDROID_ROOT = ROOT / "android"
DEFAULT_KEYSTORE = ANDROID_ROOT / "signing" / "lucima.keystore"
DEFAULT_PROPERTIES = ANDROID_ROOT / "keystore.properties"


def find_keytool() -> str:
    candidates = []
    java_home = os.environ.get("JAVA_HOME", "").strip()
    if java_home:
        candidates.append(Path(java_home) / "bin" / "keytool.exe")
        candidates.append(Path(java_home) / "bin" / "keytool")
    candidates.extend((
        Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        / "Android" / "Android Studio" / "jbr" / "bin" / "keytool.exe",
    ))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    discovered = shutil.which("keytool")
    if discovered:
        return discovered
    raise SystemExit("keytool was not found; configure JAVA_HOME with JDK 17 or newer")


def certificate_sha256(keytool: str, keystore: Path, password: str) -> str:
    result = subprocess.run(
        [
            keytool, "-exportcert", "-keystore", str(keystore),
            "-storepass", password, "-alias", "lucima",
        ],
        check=True,
        capture_output=True,
    )
    if not result.stdout:
        raise SystemExit("keytool did not export the generated certificate")
    return hashlib.sha256(result.stdout).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keystore", type=Path, default=DEFAULT_KEYSTORE)
    parser.add_argument("--properties", type=Path, default=DEFAULT_PROPERTIES)
    args = parser.parse_args()

    keystore = args.keystore.resolve()
    properties = args.properties.resolve()
    if keystore.exists() or properties.exists():
        raise SystemExit(
            "Signing files already exist. Refusing to replace the release identity."
        )

    password = secrets.token_urlsafe(48)
    keytool = find_keytool()
    keystore.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                keytool, "-genkeypair", "-v", "-keystore", str(keystore),
                "-storetype", "PKCS12", "-storepass", password,
                "-keypass", password, "-alias", "lucima",
                "-keyalg", "RSA", "-keysize", "4096", "-validity", "36500",
                "-dname", "CN=LucimaTools, OU=Release, O=LucimaTools, C=CN",
                "-noprompt",
            ],
            check=True,
        )
        fingerprint = certificate_sha256(keytool, keystore, password)
        relative_store = keystore.relative_to(ANDROID_ROOT).as_posix()
        properties.write_text(
            "\n".join((
                f"storeFile={relative_store}",
                f"storePassword={password}",
                "keyAlias=lucima",
                f"keyPassword={password}",
                f"certificateSha256={fingerprint}",
                "",
            )),
            encoding="utf-8",
        )
    except BaseException:
        keystore.unlink(missing_ok=True)
        properties.unlink(missing_ok=True)
        raise

    print(f"Created Android release keystore: {keystore}")
    print(f"Created ignored signing config: {properties}")
    print(f"Certificate SHA-256: {fingerprint}")
    print("Back up both files outside the repository before publishing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
