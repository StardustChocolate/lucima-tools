"""Bounded, streamed public media downloads in the app's private files directory.

No game credentials are sent. Only the configured manifest and its declared
resource pool are reachable; the WebView receives local file URLs afterwards.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urljoin, urlsplit

import httpx

from . import config
from .encyclopedia_resources import resource_config, _http_url

LIMIT = 64 * 1024 * 1024
DISK_LIMIT = 512 * 1024 * 1024
MANIFEST_TTL = 15 * 60
_downloads = threading.BoundedSemaphore(2)
_prune_lock = threading.Lock()


def root():
    return config.data_dir() / 'resources' / 'downloads'


@lru_cache(maxsize=128)
def _key_lock(key):
    return threading.Lock()


def _read(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=2)
def _manifest(path, stamp, size):
    value = _read(Path(path))
    if value.get('schema', 1) not in (1, 2):
        raise ValueError('不支持的资源清单版本')
    objects = {}
    if value.get('schema') == 2:
        if not isinstance(value.get('files'), dict):
            raise ValueError('资源文件表无效')
        for logical, row in value['files'].items():
            match = re.fullmatch(r'[A-Za-z0-9_./-]+\.(png|jpg|jpeg|webp|skel|atlas|js)', logical)
            if not match or any(part in ('', '.', '..') for part in logical.split('/')) \
                    or not isinstance(row, dict) or not re.fullmatch(r'[a-f0-9]{64}', str(row.get('sha256'))) \
                    or type(row.get('size')) is not int or not 0 < row['size'] <= LIMIT:
                raise ValueError('资源文件表无效')
            objects['objects/' + row['sha256'] + '.' + match[1]] = row
        if not objects:
            raise ValueError('资源文件表为空')
    return value, objects


def _current_manifest():
    endpoint = resource_config()
    url = endpoint.get('manifestUrl')
    if not url:
        raise ValueError('未配置有效的资源地址')
    path, info = _download(url, ttl=MANIFEST_TTL, limit=8 * 1024 * 1024)
    stat = path.stat()
    value, objects = _manifest(str(path), stat.st_mtime_ns, stat.st_size)
    base = _http_url(urljoin(url, value.get('baseUrl') or endpoint.get('baseUrl') or '.'))
    if not base:
        raise ValueError('资源目录无效')
    return endpoint, value, objects, base, path, info


def local_config():
    endpoint, value, _, base, _, _ = _current_manifest()
    # Android resolves the file index on disk. Do not duplicate thousands of
    # hash/size records in the WebView's JavaScript heap.
    return {**endpoint, **{key:item for key,item in value.items() if key != 'files'},
            'baseUrl':base, 'privateFileIndex':True}


def cached_file(key):
    if not re.fullmatch(r'[a-f0-9]{64}', key or ''):
        raise ValueError('无效的资源文件标识')
    folder = root()
    path, info = folder / (key + '.data'), _read(folder / (key + '.json'))
    if not path.is_file() or path.stat().st_size != info.get('size'):
        raise FileNotFoundError('本地资源不存在或不完整')
    return path, info


def _prune(keep):
    folder = root()
    with _prune_lock:
        files = [(path.stat().st_mtime, path, path.stat().st_size)
                 for path in folder.glob('*.data') if path.is_file()]
        total = sum(row[2] for row in files)
        for _, path, size in sorted(files):
            if total <= DISK_LIMIT:
                break
            if path.stem == keep or _read(path.with_suffix('.json')).get('manifest'):
                continue
            path.unlink(missing_ok=True)
            path.with_suffix('.json').unlink(missing_ok=True)
            total -= size


def _download(url, *, version='', digest=None, expected_size=None, ttl=None, limit=LIMIT):
    key = hashlib.sha256((url + '\n' + version).encode()).hexdigest()
    folder = root()
    folder.mkdir(parents=True, exist_ok=True)
    with _key_lock(str(folder) + key):
        old = None
        try:
            old = cached_file(key)
            if ttl is None or time.time() - old[1].get('checkedAt', 0) < ttl:
                if ttl is None and time.time() - old[0].stat().st_mtime > 3600:
                    os.utime(old[0], None)
                return old
        except (OSError, ValueError):
            pass
        temporary = folder / (key + '.part')
        try:
            hasher, count = hashlib.sha256(), 0
            with _downloads, httpx.Client(timeout=30, follow_redirects=False) as client:
                with client.stream('GET', url) as response:
                    response.raise_for_status()
                    if int(response.headers.get('Content-Length') or 0) > limit:
                        raise ValueError('资源文件超过大小上限')
                    with temporary.open('wb') as output:
                        for block in response.iter_bytes(64 * 1024):
                            count += len(block)
                            if count > limit:
                                raise ValueError('资源文件超过大小上限')
                            output.write(block)
                            hasher.update(block)
            if count <= 0 or (expected_size is not None and count != expected_size):
                raise ValueError('资源文件大小校验失败')
            if digest and hasher.hexdigest() != digest:
                raise ValueError('资源文件哈希校验失败')
            if ttl is not None:
                manifest = _read(temporary)
                if not isinstance(manifest.get('version'), str) or not manifest['version'].strip():
                    raise ValueError('资源清单格式无效')
                stat = temporary.stat()
                _manifest(str(temporary), stat.st_mtime_ns, stat.st_size)
            info = {'url':url, 'size':count, 'sha256':hasher.hexdigest(),
                    'checkedAt':time.time(), 'manifest':ttl is not None,
                    'type':mimetypes.guess_type(urlsplit(url).path)[0] or 'application/octet-stream'}
            metadata = folder / (key + '.json.tmp')
            metadata.write_text(json.dumps(info), encoding='utf-8')
            path = folder / (key + '.data')
            os.replace(temporary, path)
            os.replace(metadata, folder / (key + '.json'))
            _prune(key)
            return path, info
        except Exception:
            # A failed refresh may reuse a complete prior manifest. Hashed
            # object failures never replace a verified file or leave a .part.
            if old is not None and ttl is not None:
                return old
            raise
        finally:
            temporary.unlink(missing_ok=True)
            (folder / (key + '.json.tmp')).unlink(missing_ok=True)


def prepare(url):
    """Resolve a public resource to a local file, without holding account locks."""
    url = _http_url(url)
    if not url:
        raise ValueError('未配置有效的资源地址')
    endpoint, manifest, objects, base, manifest_path, manifest_info = _current_manifest()
    manifest_url = endpoint['manifestUrl']
    if url == manifest_url:
        path, info = manifest_path, manifest_info
    else:
        parsed, prefix = urlsplit(url), urlsplit(base)
        if not base or parsed.scheme != prefix.scheme or parsed.netloc != prefix.netloc \
                or not parsed.path.startswith(prefix.path.rstrip('/') + '/') or parsed.query or parsed.fragment:
            raise ValueError('资源地址不在配置的目录内')
        relative = parsed.path[len(prefix.path.rstrip('/')) + 1:]
        if not re.fullmatch(r'[A-Za-z0-9_./-]+\.(?:png|jpg|jpeg|webp|skel|atlas|js|json)',relative) \
                or any(part in ('', '.', '..') for part in relative.split('/')):
            raise ValueError('资源路径无效')
        digest = size = None
        if manifest.get('schema') == 2:
            declaration = objects.get(relative) or manifest['files'].get(relative)
            if declaration is None:
                raise ValueError('资源未列入清单或大小无效')
            digest, size = declaration['sha256'], declaration['size']
            extension = relative.rsplit('.',1)[-1]
            url = urljoin(base.rstrip('/') + '/', 'objects/' + digest + '.' + extension)
        path, info = _download(url, version=str(manifest['version']) if digest is None else '',
                               digest=digest, expected_size=size)
    return {'url':'/resource-cache/' + path.stem,
            'size':info['size'], 'sha256':info['sha256']}
