"""Android session recovery and scheduler health, independent of WebView timers."""
from __future__ import annotations
import threading
import time
from . import accounts, config
from .logutil import log

_guard = threading.Lock()
_thread = None
_retry_at = {}
_restoring = set()


def is_restoring(email):
    with _guard:return email in _restoring


def recover_once(now=None):
    if config.PLATFORM != 'android':
        return
    from . import appcore
    now = time.time() if now is None else now
    for email, saved in config.load_accounts().items():
        if not saved.get('automationActive') or not saved.get('auth') or saved.get('auth_error'):
            continue
        acc = accounts.get(email)
        if acc is None:
            if _retry_at.get(email, 0) > now:
                continue
            try:
                with _guard:_restoring.add(email)
                appcore.login_saved(email)
                acc = accounts.get(email)
                # An explicit logout during authentication wins over recovery.
                if not config.load_accounts().get(email, {}).get('automationActive'):
                    if acc is not None: accounts.remove(email)
                    continue
                _retry_at.pop(email, None)
            except Exception as error:
                _retry_at[email] = now + 300
                log.warning('[background] saved session recovery failed; retry in 300s: %s', type(error).__name__)
                continue
            finally:
                with _guard:_restoring.discard(email)
        if acc is not None and not acc._stop.is_set():
            acc.start_scheduler()  # idempotent; restarts an unexpectedly dead scheduler


def wake_all():
    for acc in accounts.all_accounts():
        acc._wake.set()


def start():
    global _thread
    if config.PLATFORM != 'android': return
    with _guard:
        if _thread is not None and _thread.is_alive(): return
        def work():
            while True:
                try:
                    recover_once()
                    from .equipment_notifications import retry_pending
                    retry_pending()
                except Exception as error: log.warning('[background] health check failed: %s', type(error).__name__)
                time.sleep(60)
        _thread = threading.Thread(target=work, name='android-automation-health', daemon=True)
        _thread.start()
