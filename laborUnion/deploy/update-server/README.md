# LucimaTools Update Server

The update endpoint, publishing host, and remote directory are supplied only
through local environment variables. Do not put those values in this repository.

The manifest is an RS256-signed envelope. The signing private key stays only on
the release workstation at:

```text
%USERPROFILE%\.lucimatools\update-signing\private.pem
```

Only `backend/update_public_key.json` is bundled into the clients. Do not copy
the private key to the update server or repository. Do not regenerate it after
a client release unless a planned key rotation is implemented first.

## Publish a release

1. Change `APP_VERSION` in `backend/version.py`.
2. Build Windows and the signed Android release APK.
3. Run from the repository root:

```powershell
python tools/publish_update.py `
  --windows-dir dist/LucimaTools `
  --android-apk android/app/build/outputs/apk/release/app-release.apk `
  --base-url "https://<update-host>" `
  --server "<ssh-user>@<update-host>" `
  --remote-root "<remote-update-root>" `
  --min-supported 1.1.6 `
  --note "本次更新说明" `
  --publish
```

Artifacts are written under `build/update-release/<version>/`. The command
uploads artifacts to a temporary remote name, verifies their SHA-256 hashes,
renames them into place, and atomically publishes `stable.json` last.

`minSupportedVersion` controls forced updates. Keep it at the oldest usable
version for optional releases; raise it only when older clients must stop.

## Development manifest

Publish a signed manifest without artifacts, while keeping the current client
on the same version:

```powershell
python tools/publish_update.py --development --allow-empty --publish
```

This is safe for connectivity and signature tests because it cannot trigger an
installation.

## Server layout

```text
<remote-update-root>/
├── health.json
├── stable.json
└── releases/
    └── <version>/
```

The Nginx configuration template is `nginx-ip.conf`; replace its placeholders
only in the private server deployment. Use HTTPS in production while keeping
manifest signatures and artifact hashes enabled.
