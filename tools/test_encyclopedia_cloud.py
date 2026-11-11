"""Cloud update boundaries: lazy objects, atomic switches, offline reuse and pacing."""
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.encyclopedia import Encyclopedia, EncyclopediaError, encoded


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = {}
        self.requests = []
        self.now = 10000
        self.offline = False
        self.entry = self.release('v1', 'first')
        self.service = self.instance()

    def instance(self):
        return Encyclopedia(self.root, 'https://example.test/encyclopedia/latest.json',
                            self.get, lambda: self.now)

    def put(self, raw, suffix='.json'):
        sha = hashlib.sha256(raw).hexdigest()
        path = 'objects/' + sha + suffix
        self.remote[path] = raw
        return {'path': path, 'sha256': sha, 'size': len(raw)}

    def release(self, version, text, heroes=('H001',), search=None):
        self.image = self.put(b'image', '.png')
        rows = []
        for hero in heroes:
            detail = self.put(encoded({'schema': 1, 'character': {'ID': hero, 'Description': text},
                                       'media': {'portraits/static/' + hero + '.png': self.image}}))
            rows.append({'id': hero, 'name': hero, 'detail': detail, 'avatar': self.image})
        index_data = {'schema': 1, 'characters': rows}
        if search is not None:
            index_data['search'] = search
        index = self.put(encoded(index_data))
        ui = self.put(encoded({'schema': 1, 'translations': {'status_filters': {}}, 'files': []}))
        entry = {'schema': 1, 'kind': 'lucima-encyclopedia', 'renderer': 1,
                 'version': version, 'index': index, 'ui': ui}
        self.remote['latest.json'] = encoded(entry)
        self.etag = version
        return entry

    def get(self, url, headers, limit):
        path = url.split('/encyclopedia/', 1)[1]
        self.requests.append((path, headers))
        if self.offline:
            raise httpx.ConnectError('offline')
        if path == 'latest.json' and headers.get('If-None-Match') == self.etag:
            return 304, {}, b''
        return 200, {'etag': self.etag}, self.remote[path]

    def test_first_check_fetches_only_entry_index_and_ui_then_one_detail_on_demand(self):
        self.assertFalse(self.service.catalog()['available'])
        self.assertEqual(self.requests, [])
        result = self.service.check()
        self.assertTrue(result['available'])
        self.assertIsNone(result['search'])
        self.assertEqual(len(self.requests), 3)
        self.assertFalse(any(path == self.image['path'] for path, _ in self.requests))
        self.assertEqual(self.service.detail('v1', 'H001')['character']['Description'], 'first')
        self.assertEqual(len(self.requests), 4)
        raw, _ = self.service.object(Path(self.image['path']).name)
        self.assertEqual(raw, b'image')
        self.assertEqual(len(self.requests), 5)

    def test_search_index_is_returned_with_catalog_and_validated(self):
        search = {
            'ids': {'h001': ['H001']}, 'names': {'shani': ['H001']},
            'aliases': {'夏妮': ['H001']}, 'pinyin': {'xiani': ['H001']},
        }
        self.release('v-search', 'first', search=search)
        result = self.service.check()
        self.assertEqual(result['search'], search)

        bad = dict(search)
        bad['pinyin'] = {'xiani': ['H999']}
        self.release('v-invalid-search', 'bad', search=bad)
        self.now += 3601
        self.assertEqual(self.service.check()['version'], 'v-search')

    def test_hourly_check_uses_etag_without_redownloading_any_data(self):
        self.service.check()
        self.service.check()
        self.assertEqual(len(self.requests), 3)
        self.now += 3601
        self.assertFalse(self.service.check()['updated'])
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.requests[-1][1]['If-None-Match'], 'v1')

    def test_entry_check_bypasses_hourly_interval_but_keeps_five_second_debounce(self):
        self.service.check(force=True)
        self.now += 4
        self.service.check(force=True)
        self.assertEqual(len(self.requests), 3)
        self.now += 2
        self.service.check(force=True)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.requests[-1][1]['If-None-Match'], 'v1')
        self.now += 6
        self.service.check()
        self.assertEqual(len(self.requests), 4)

    def test_restart_offline_reuses_only_previously_downloaded_data(self):
        self.service.check()
        self.service.detail('v1', 'H001')
        self.service.object(Path(self.image['path']).name)
        before = len(self.requests)
        self.offline = True
        restarted = self.instance()
        self.assertTrue(restarted.catalog()['available'])
        self.assertEqual(restarted.detail('v1', 'H001')['character']['Description'], 'first')
        self.assertEqual(restarted.object(Path(self.image['path']).name)[0], b'image')
        self.assertEqual(len(self.requests), before)

    def test_corrupt_candidate_does_not_change_active_snapshot_and_can_recover(self):
        self.service.check()
        candidate = self.release('v2', 'second')
        correct = self.remote[candidate['index']['path']]
        self.remote[candidate['index']['path']] = b'corrupt'
        self.now += 3601
        self.assertEqual(self.service.check()['version'], 'v1')
        self.assertEqual(json.loads((self.root / 'active.json').read_bytes())['version'], 'v1')
        self.remote[candidate['index']['path']] = correct
        self.now += 121
        self.assertEqual(self.service.check()['version'], 'v2')

    def test_update_adds_hero_and_old_version_remains_available_without_prefetch(self):
        self.service.check()
        before = len(self.requests)
        self.release('v2', 'second', ('H001', 'H002'))
        self.now += 3601
        result = self.service.check()
        self.assertEqual(len(result['characters']), 2)
        self.assertEqual(len(self.requests) - before, 2)  # shared UI hash reused
        self.assertEqual(self.service.detail('v1', 'H001')['character']['Description'], 'first')
        self.assertEqual(self.service.detail('v2', 'H001')['character']['Description'], 'second')
        restarted = self.instance()
        self.assertEqual(restarted.detail('v1', 'H001')['character']['Description'], 'first')

    def test_failed_first_download_retries_with_backoff_and_manual_check_can_bypass(self):
        self.offline = True
        self.assertFalse(self.service.check()['available'])
        self.service.check()
        self.assertEqual(len(self.requests), 1)
        self.now += 6
        self.offline = False
        self.assertTrue(self.service.check(force=True)['available'])

    def test_incompatible_renderer_and_unsafe_references_keep_old_data(self):
        self.service.check()
        entry = self.release('v2', 'second')
        entry['renderer'] = 99
        self.remote['latest.json'] = encoded(entry)
        self.now += 3601
        self.assertEqual(self.service.check()['version'], 'v1')
        entry['renderer'] = 1
        entry['index']['path'] = '../settings.json'
        self.remote['latest.json'] = encoded(entry)
        self.now += 121
        self.assertEqual(self.service.check()['version'], 'v1')
        with self.assertRaises(EncyclopediaError):
            self.service.object('../settings.json')

    def test_computed_camera_renderer_keeps_lightweight_check_and_lazy_media(self):
        entry = dict(self.entry, renderer=2, media={
            'path': 'manifests/v1.json', 'sha256': '0' * 64, 'size': 1000})
        self.remote['latest.json'] = encoded(entry)
        self.assertTrue(self.service.check()['available'])
        self.assertEqual(len(self.requests), 3)
        self.assertFalse(any(path.endswith('.framing.json') or path.startswith('manifests/')
                             for path, _ in self.requests))
        self.assertEqual(self.service.detail('v1', 'H001')['character']['Description'], 'first')

    def test_concurrent_checks_and_media_downloads_are_coalesced(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: self.service.check(), range(5)))
        self.assertTrue(all(result['available'] for result in results))
        self.assertEqual(len(self.requests), 3)
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: self.service.object(Path(self.image['path']).name), range(5)))
        self.assertEqual(len(self.requests), 4)

    def test_cached_catalog_remains_immediate_while_a_check_is_waiting_on_network(self):
        self.service.check()
        entered, released = threading.Event(), threading.Event()
        original = self.service.client
        def waiting(url, headers, limit):
            entered.set()
            released.wait(3)
            return original(url, headers, limit)
        self.service.client = waiting
        self.now += 3601
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(self.service.check)
            self.assertTrue(entered.wait(2))
            try:
                self.assertEqual(pool.submit(self.service.catalog).result(timeout=1)['version'], 'v1')
            finally:
                released.set()
            future.result()

    def test_reusing_a_historical_version_for_different_content_is_rejected(self):
        self.service.check()
        self.release('v2', 'second')
        self.now += 3601
        self.service.check()
        self.release('v1', 'changed historical content')
        self.now += 3601
        result = self.service.check()
        self.assertEqual(result['version'], 'v2')
        self.assertTrue(result['error'])

    def test_malformed_entry_and_lost_cache_can_be_recovered(self):
        self.service.check()
        self.remote['latest.json'] = b'[]'
        self.now += 3601
        self.assertEqual(self.service.check()['version'], 'v1')
        self.remote['latest.json'] = encoded(self.entry)
        self.now += 121
        self.service.check()
        (self.root / self.entry['index']['path']).unlink()
        restarted = self.instance()
        self.assertFalse(restarted.catalog()['available'])
        self.assertTrue(restarted.check()['available'])


if __name__ == '__main__':
    unittest.main()
