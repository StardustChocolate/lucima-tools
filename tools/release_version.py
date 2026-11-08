"""Android advances release versions; iOS and Windows follow its last verified APK."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import uuid

VERSION = re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?')


def parts(value):
    match = VERSION.fullmatch(str(value))
    if not match:
        raise ValueError('版本必须是三段基准或四段完整版本，例如 1.3.5 / 1.3.5.1')
    return '.'.join(match.group(i) for i in (1, 2, 3)), int(match.group(4)) if match.group(4) else None


def _constant(text, name, default=None):
    match = re.search(rf'^{name}\s*=\s*(["\'])(.*?)\1\s*$', text, re.M)
    return match.group(2) if match else default


def _number(text):
    match = re.search(r'^ANDROID_VERSION_CODE\s*=\s*(\d+)\s*$', text, re.M)
    return int(match.group(1)) if match else 0


def _atomic(path, text):
    temporary = path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _lock(project):
    import fcntl  # All three local build skills run on this macOS host.
    directory = project / 'build'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.release-version.lock').open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _release(record):
    version = record['version']
    parts(version)
    return {'version': version, 'androidCode': int(record.get('androidCode', record.get('version_code', 0))),
            'artifact': str(record.get('artifact', record.get('apk', '')))}


def _state(project):
    path = project / 'build/release-version.json'
    state = json.loads(path.read_text()) if path.exists() else {}
    if state.get('schema_version') == 2:
        return state
    # Old counters included desktop/iOS builds. Recover the Android sequence
    # from its last successful report, rather than the largest artifact label.
    migrated = {'schema_version': 2, 'numbering': 'android-led', 'counters': {},
                'reservations': {}, 'releases': {}}
    report = project / 'build/android-skill/last-success.json'
    if report.is_file():
        android = json.loads(report.read_text())
        if android.get('status') != 'ok':
            raise ValueError('安卓 last-success.json 不是成功记录')
        migrated['releases']['android'] = _release(android)
    old = state.get('reservation', {})
    platform = old.get('platform')
    if old.get('status') == 'complete' and platform:
        migrated['releases'].setdefault(platform, _release(old))
    elif old.get('status') == 'reserved' and platform == 'android':
        migrated['reservations']['android'] = old
    for record in [migrated['releases'].get('android'), migrated['reservations'].get('android')]:
        if record:
            base, revision = parts(record['version'])
            migrated['counters'][base] = max(revision or 0, migrated['counters'].get(base, 0))
    return migrated


def _plan(project, state, platform, requested, reuse):
    if platform not in ('android', 'ios', 'windows', 'macos'):
        raise ValueError('不支持的打包平台')
    path = project / 'backend/version.py'
    text = path.read_text(encoding='utf-8')
    current = _constant(text, 'APP_VERSION')
    current_base, current_revision = parts(current)
    old = state.get('reservations', {}).get(platform, {})
    android = state.get('releases', {}).get('android')
    if platform != 'android':
        if not android or not Path(android['artifact']).is_file():
            raise ValueError('找不到最近成功核验的安卓包；请先生成安卓 APK，再打电脑版或苹果版')
        version = android['version']
        base, revision = parts(version)
        if requested:
            requested_base, exact = parts(requested)
            if requested_base != base or (exact is not None and exact != revision):
                raise ValueError(f'电脑版和苹果版必须跟随当前安卓版本 {version}；切换基准请先打安卓包')
        if reuse and old.get('version') != version:
            raise ValueError('没有与当前安卓版本一致的未完成打包可续跑')
        return text, version, max(_number(text), android['androidCode'])
    base, exact = parts(requested) if requested else parts(old['version']) if old else (current_base, None)
    if reuse:
        if not old or parts(old['version'])[0] != base or (requested and exact is not None and old['version'] != requested):
            raise ValueError('没有该平台可续跑的未完成打包版本')
        return text, old['version'], max(_number(text), old['androidCode'])
    if old and parts(old['version'])[0] == base and (not requested or exact is None or old['version'] == requested):
        return text, old['version'], max(_number(text), old['androidCode'])
    observed_version = android['version'] if android else current
    observed_base, observed_revision = parts(observed_version)
    observed = (observed_revision or 0) if base == observed_base else 0
    revision = max(state.get('counters', {}).get(base, 0), observed) + 1 if exact is None else exact
    if revision < 1:
        raise ValueError('第四段打包序号必须从 1 开始')
    major, minor, patch = map(int, base.split('.'))
    code = max(_number(text) + 1, major * 10000 + minor * 100 + patch)
    if code >= 2100000000:
        raise ValueError('Android 安装版本号超出范围')
    return text, f'{base}.{revision}', code


def prepare_version(project, platform, *, requested=None, reuse=False, preview=False):
    project = Path(project).resolve()
    state_path = project / 'build/release-version.json'
    if preview:
        return _plan(project, _state(project), platform, requested, reuse)[1]
    with _lock(project):
        state = _state(project)
        text, version, code = _plan(project, state, platform, requested, reuse)
        base, revision = parts(version)
        if platform == 'android':
            state.setdefault('counters', {})[base] = max(revision, state.get('counters', {}).get(base, 0))
        state.setdefault('reservations', {})[platform] = {
            'platform': platform, 'version': version, 'androidCode': code, 'status': 'reserved'}
        # A failed source write can safely resume this reserved number.
        _atomic(state_path, json.dumps(state, ensure_ascii=False, indent=2)+'\n')
        for name, value in (('APP_VERSION', json.dumps(version)), ('APP_VERSION_BASE', json.dumps(base)), ('ANDROID_VERSION_CODE', str(code))):
            text, count = re.subn(rf'^{name}\s*=.*$', f'{name} = {value}', text, flags=re.M)
            if not count: text += f'\n{name} = {value}\n'
        _atomic(project/'backend/version.py', text)
        return version


def finish_release(project, version, artifact, *, platform=None):
    project = Path(project).resolve()
    if not Path(artifact).is_file(): raise ValueError('产物不存在，不能完成版本预留')
    with _lock(project):
        path = project/'build/release-version.json'
        state = json.loads(path.read_text())
        reservations = state.get('reservations', {})
        if platform is None:
            candidates = [name for name, record in reservations.items() if record['version'] == version]
            if len(candidates) != 1:
                raise ValueError('请明确指定完成打包的平台')
            platform = candidates[0]
        record = reservations.get(platform, {})
        if record.get('version') != version:
            raise ValueError('打包期间版本预留发生变化，请按顺序打包')
        state.setdefault('releases', {})[platform] = {
            'version': version, 'androidCode': record['androidCode'], 'artifact': str(artifact)}
        del reservations[platform]
        _atomic(path, json.dumps(state, ensure_ascii=False, indent=2)+'\n')
