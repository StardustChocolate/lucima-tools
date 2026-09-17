"""Generate the offline RSA key used to sign LucimaTools update manifests."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError as exc:  # pragma: no cover - release workstation setup
    raise SystemExit("Install release dependencies: pip install -r requirements-release.txt") from exc


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PRIVATE_KEY = Path.home() / ".lucimatools" / "update-signing" / "private.pem"
DEFAULT_PUBLIC_KEY = ROOT / "backend" / "update_public_key.json"


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def restrict_private_key(path: Path) -> None:
    if os.name == "nt":
        user = os.environ.get("USERNAME")
        if user:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    else:
        path.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-key", type=Path, default=DEFAULT_PRIVATE_KEY)
    parser.add_argument("--public-key", type=Path, default=DEFAULT_PUBLIC_KEY)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    private_path = args.private_key.expanduser().resolve()
    public_path = args.public_key.expanduser().resolve()
    if private_path.exists() and not args.force:
        raise SystemExit(f"Refusing to replace existing private key: {private_path}")

    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    numbers = key.public_key().public_numbers()
    key_id = "lucimatools-rsa-" + hashlib.sha256(public_der).hexdigest()[:16]
    public_data = {
        "algorithm": "RS256",
        "keyId": key_id,
        "modulus": b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "exponent": b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }

    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(private_pem)
    restrict_private_key(private_path)
    public_path.write_text(
        json.dumps(public_data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Private key: {private_path}")
    print(f"Public key:  {public_path}")
    print(f"Key ID:      {key_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
