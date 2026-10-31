"""Snapshot consistency, official catalog changes and interrupted update recovery."""
import base64
from collections import Counter
import io
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend import runtime_data
from backend.resource_update import ResourceUpdates, catalog_sources, localization
from backend.unity_text import BundleError, lz4, text_assets


def catalog_bytes(tag="a"):
    ids, entries, extra = [], [], bytearray()
    for index, path in enumerate(("staticdata", "staticdata/text", "localization/chs.asset")):
        content_hash = (tag + str(index)) * 16
        ids.append("http://PatchDomain/Android/firstbundlegroup_assets_assets/game/" + path + "_" + content_hash + ".bundle")
        offset = len(extra)
        raw = json.dumps({"m_BundleSize": 4}).encode("utf-16-le")
        extra.extend(b"\x07\x01A\x01B" + struct.pack("<i", len(raw)) + raw)
        entries.append(struct.pack("<7i", index, 0, -1, 0, offset, 0, 0))
    return runtime_data.encode({"m_InternalIds": ids,
        "m_EntryDataString": base64.b64encode(struct.pack("<i", 3) + b"".join(entries)).decode(),
        "m_ExtraDataString": base64.b64encode(extra).decode()})


def dataset(value):
    return {name: {"value": value} for name in runtime_data.DATA_FILES}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = runtime_data.Registry(self.temp.name)
        self.patch = patch.object(runtime_data, "registry", self.store)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_pin_keeps_all_loaders_on_old_data_until_task_ends(self):
        self.store.publish("old", dataset(1), {"catalog": "catalog_1"})
        with runtime_data.pin():
            self.store.publish("new", dataset(2), {"catalog": "catalog_2"})
            self.assertEqual(runtime_data.read_json("backend/item_names.json"), {"value": 1})
            with runtime_data.pin():
                self.assertEqual(runtime_data.read_json("assets/game_config.json"), {"value": 1})
        self.assertEqual(runtime_data.read_json("assets/game_config.json"), {"value": 2})

    def test_derived_cache_changes_with_snapshot(self):
        @runtime_data.cached
        def loader():
            return runtime_data.read_json("backend/item_rank.json")["value"]
        self.store.publish("old", dataset(1), {})
        self.assertEqual(loader(), 1)
        self.store.publish("new", dataset(2), {})
        self.assertEqual(loader(), 2)
        loader.cache_clear()
        self.assertEqual(loader(), 2)

    def test_restart_uses_saved_data_without_network(self):
        self.store.publish("v1", dataset(5), {})
        restored = runtime_data.Registry(self.temp.name).current()
        self.assertEqual(restored.version, "v1")
        self.assertEqual(restored.read("assets/game_config.json"), {"value": 5})

    def test_corrupt_active_recovers_previous(self):
        self.store.publish("old", dataset(1), {})
        self.store.publish("new", dataset(2), {})
        (Path(self.temp.name) / "versions/new/assets/game_config.json").write_bytes(b"{}")
        self.assertEqual(runtime_data.Registry(self.temp.name).current().version, "old")

    def test_incomplete_data_never_replaces_active(self):
        self.store.publish("old", dataset(1), {})
        with self.assertRaises(ValueError):
            self.store.publish("new", {"assets/game_config.json": {}}, {})
        self.assertEqual(self.store.current().version, "old")

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.restore("../outside")

    def test_invalid_pointer_shape_falls_back_to_bundled(self):
        (Path(self.temp.name) / "active.json").write_text("[]")
        self.assertEqual(runtime_data.Registry(self.temp.name).current().version, "bundled")

    def test_invalid_manifest_shape_recovers_previous(self):
        self.store.publish("old", dataset(1), {})
        self.store.publish("new", dataset(2), {})
        (Path(self.temp.name) / "versions/new/manifest.json").write_text("[]")
        self.assertEqual(runtime_data.Registry(self.temp.name).current().version, "old")

    def test_another_account_thread_can_use_new_data_while_old_task_runs(self):
        self.store.publish("old", dataset(1), {})
        other = []
        with runtime_data.pin():
            self.store.publish("new", dataset(2), {})
            thread = threading.Thread(target=lambda: other.append(runtime_data.read_json("assets/game_config.json")))
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(runtime_data.read_json("assets/game_config.json"), {"value": 1})
        self.assertEqual(other, [{"value": 2}])


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = runtime_data.Registry(self.temp.name)
        self.catalog = "catalog_115942"
        self.hash = "a" * 32
        self.server_open = True
        self.tag = "a"
        self.calls = Counter()
        self.clock = 1000
        self.builds = 0
        self.error = None
        self.after_build = None
        self.enterContext(patch('backend.updater.routine_check_delay', return_value=3600))
        def transport(method, url, payload):
            self.calls[method, url] += 1
            if self.error:
                raise OSError(self.error)
            if method == "PUT":
                return runtime_data.encode({"Info": {"NewCatalogName": self.catalog, "PathDomain": "https://patch.example/GamePatch", "IsOpen": self.server_open}})
            if url.endswith(".hash"):
                return self.hash.encode()
            if url.endswith(".json"):
                return catalog_bytes(self.tag)
            return b"data"
        def build(source, catalog, previous):
            self.builds += 1
            if self.after_build:
                self.after_build()
            return dataset(self.builds), {"heroes": 227}
        from backend.maintenance import Maintenance
        self.service = ResourceUpdates(self.store, transport, clock=lambda: self.clock, builder=build,
                                       maintenance=Maintenance())
        self.compile_patch = patch.object(self.service, "_compile", side_effect=lambda bundles, catalog: build(None, catalog, None))
        self.compile_patch.start()
        self.addCleanup(self.compile_patch.stop)

    def check(self):
        return self.service.check(force=True)

    def test_first_check_downloads_and_activates(self):
        state = self.check()
        self.assertEqual(state["phase"], "ready")
        self.assertEqual(state["version"], self.catalog)
        self.assertEqual(sum(count for (_, url), count in self.calls.items() if url.endswith(".bundle")), 3)

    def test_unchanged_catalog_only_checks_small_metadata(self):
        self.check()
        self.calls.clear()
        self.check()
        self.assertEqual(sum(self.calls.values()), 2)
        self.assertFalse(any(url.endswith((".json", ".bundle")) for _, url in self.calls))

    def test_normal_schedule_is_hourly_and_software_tick_sets_the_same_deadline(self):
        self.assertEqual(self.check()['nextCheckAt'], 4600)
        self.calls.clear()
        self.clock = 1300
        self.service.check()
        self.assertEqual(sum(self.calls.values()), 0)
        with patch.object(self.service, 'start'):
            self.service.on_software_check(3300)
        self.assertEqual(self.service.state['nextCheckAt'], 1300)
        self.assertTrue(self.service.wake.is_set())
        self.service.check()
        self.assertEqual(self.service.status()['nextCheckAt'], 4600)
        self.assertEqual(sum(self.calls.values()), 2)

    def test_startup_check_and_software_tick_are_coalesced(self):
        self.check()
        self.calls.clear()
        with patch.object(self.service, 'start'):
            self.service.on_software_check(4200)
        self.assertEqual(self.service.state['nextCheckAt'], 5200)
        self.service.check()
        self.assertEqual(sum(self.calls.values()), 0)

    def test_server_maintain_switches_to_five_minutes_without_software_tick_delaying_it(self):
        self.check()
        self.server_open = False
        self.service.maintenance.observe_response('Server Maintain_Server Maintain')
        self.assertEqual(self.service.state['nextCheckAt'], 1300)
        self.clock = 1100
        self.service.maintenance.observe_response('Server Maintain')
        with patch.object(self.service, 'start'):
            self.service.on_software_check(3600)
        self.assertEqual(self.service.state['nextCheckAt'], 1300)
        self.calls.clear()
        self.clock = 1299
        self.service.check()
        self.assertEqual(sum(self.calls.values()), 0)
        self.clock = 1300
        self.assertEqual(self.service.check()['phase'], 'waiting_server')
        self.assertEqual(self.service.status()['nextCheckAt'], 1600)
        self.assertEqual(sum(self.calls.values()), 2)
        self.clock = 1600
        self.server_open = True
        self.assertEqual(self.service.check()['phase'], 'ready')
        self.assertEqual(self.service.status()['nextCheckAt'], 4700)
        self.assertFalse(self.service.maintenance.active)

    def test_fast_checks_download_new_data_then_activate_after_reopening(self):
        self.check()
        self.server_open = False
        self.hash = 'b' * 32
        self.tag = 'b'
        self.service.maintenance.observe_response('Server Maintain')
        self.clock = 1300
        state = self.service.check()
        self.assertEqual(state['phase'], 'waiting_server')
        self.assertEqual(state['nextCheckAt'], 1600)
        self.assertEqual(self.builds, 2)
        self.assertEqual(self.store.current().manifest['catalogHash'], 'a' * 32)
        self.clock = 1600
        self.server_open = True
        self.service.check()
        self.assertEqual(self.store.current().manifest['catalogHash'], 'b' * 32)
        self.assertEqual(self.builds, 2)
        self.assertEqual(self.service.status()['nextCheckAt'], 4600)

    def test_maintenance_network_failures_keep_five_minute_cadence(self):
        self.check()
        self.service.maintenance.observe_response('Server Maintain')
        self.error = 'offline'
        for value in (1300, 1600, 1900, 2200):
            self.clock = value
            self.assertEqual(self.service.check()['nextCheckAt'], value + 300)
            self.assertTrue(self.service.maintenance.active)

    def test_reopening_from_a_login_finalizes_staged_data_immediately(self):
        self.check()
        self.hash = 'b' * 32
        self.server_open = False
        self.service.maintenance.observe_response('Server Maintain')
        self.clock = 1300
        self.service.check()
        self.assertEqual(self.store.current().manifest['catalogHash'], 'a' * 32)
        self.clock = 1400
        self.server_open = True
        self.service.observe({'Info': {'NewCatalogName': self.catalog, 'IsOpen': True}})
        self.assertEqual(self.service.state['nextCheckAt'], 1400)
        self.service.check()
        self.assertEqual(self.store.current().manifest['catalogHash'], 'b' * 32)
        self.assertEqual(self.service.status()['nextCheckAt'], 4600)

    def test_public_bulletin_can_itself_return_plain_server_maintain(self):
        with patch.object(self.service, '_http', return_value=b'Server Maintain_Server Maintain'):
            state = self.check()
        self.assertTrue(self.service.maintenance.active)
        self.assertEqual(state['nextCheckAt'], 1300)

    def test_routine_login_bulletins_do_not_increase_check_frequency(self):
        self.check()
        self.clock = 2000
        with patch.object(self.service, 'start') as start:
            self.service.observe({'Info': {'NewCatalogName': self.catalog, 'IsOpen': True}})
        start.assert_not_called()
        self.assertEqual(self.service.state['nextCheckAt'], 4600)

    def test_same_catalog_name_with_new_hash_is_updated(self):
        self.check()
        old = self.store.current().version
        self.hash = "b" * 32
        self.check()
        self.assertNotEqual(self.store.current().version, old)
        self.assertEqual(self.builds, 2)

    def test_unchanged_bundles_are_reused(self):
        self.check()
        self.calls.clear()
        self.hash = "b" * 32
        self.check()
        self.assertFalse(any(url.endswith(".bundle") for _, url in self.calls))

    def test_new_compiler_rebuilds_same_catalog_using_cached_bundles(self):
        self.check()
        old = self.store.current()
        self.store.publish(old.version, old.files,
                           {key: value for key, value in old.manifest.items() if key != 'buildVersion'})
        self.calls.clear()
        self.check()
        from backend.runtime_build import BUILD_VERSION
        self.assertEqual(self.builds, 2)
        self.assertEqual(self.store.current().manifest['buildVersion'], BUILD_VERSION)
        self.assertFalse(any(url.endswith('.bundle') for _, url in self.calls))

    def test_corrupt_bundle_is_downloaded_again(self):
        self.check()
        next((Path(self.temp.name) / "bundles").glob("*.bundle")).write_bytes(b"oops")
        self.calls.clear()
        self.hash = "b" * 32
        self.check()
        self.assertEqual(sum(count for (_, url), count in self.calls.items() if url.endswith(".bundle")), 1)

    def test_maintenance_stages_then_activates_without_rebuild(self):
        self.server_open = False
        state = self.check()
        self.assertEqual(state["phase"], "waiting_server")
        self.assertEqual(self.store.current().version, "bundled")
        self.server_open = True
        self.check()
        self.assertEqual(self.service.status()["phase"], "ready")
        self.assertEqual(self.builds, 1)

    def test_maintenance_is_detected_even_without_a_resource_update(self):
        self.check()
        self.server_open = False
        self.calls.clear()
        self.assertEqual(self.check()['phase'], 'waiting_server')
        self.assertTrue(self.service.maintenance.active)
        self.assertEqual(sum(self.calls.values()), 2)
        self.server_open = True
        self.assertEqual(self.check()['phase'], 'ready')
        self.assertFalse(self.service.maintenance.active)

    def test_maintenance_gate_updates_before_hash_download_and_survives_failure(self):
        def fail_hash(*args):
            self.assertTrue(self.service.maintenance.active)
            raise OSError('hash unavailable')
        self.server_open = False
        with patch.object(self.service, '_hash', side_effect=fail_hash):
            self.assertEqual(self.check()['phase'], 'error')
        self.assertTrue(self.service.maintenance.active)
        self.server_open = True
        with patch.object(self.service, '_hash', side_effect=OSError('still offline')):
            self.check()
        self.assertFalse(self.service.maintenance.active)

    def test_network_failure_retains_active_data_and_hourly_schedule(self):
        self.check()
        old = self.store.current().version
        self.error = "offline"
        state = self.check()
        self.assertEqual(state["phase"], "error")
        self.assertEqual(state["nextCheckAt"], 4600)
        self.assertEqual(self.store.current().version, old)

    def test_bad_build_retains_active_data(self):
        self.check()
        old = self.store.current().version
        self.hash = "b" * 32
        self.compile_patch.stop()
        with patch.object(self.service, "_compile", side_effect=ValueError("unknown source columns")):
            self.assertEqual(self.check()["phase"], "error")
        self.assertEqual(self.store.current().version, old)

    def test_version_changing_during_build_is_not_activated(self):
        self.after_build = lambda: setattr(self, "catalog", "catalog_115943")
        self.assertEqual(self.check()["phase"], "error")
        self.assertEqual(self.store.current().version, "bundled")

    def test_catalog_size_metadata_and_localization_priority(self):
        refs = catalog_sources(catalog_bytes())
        self.assertEqual(set(refs), {"staticdata", "text", "localization_CHS"})
        self.assertTrue(all(row["size"] == 4 for row in refs.values()))
        self.assertEqual(localization("Key@Value\nx@原文@保留\n".encode()), {"x": "原文@保留"})

    def test_missing_catalog_source_is_rejected(self):
        data = json.loads(catalog_bytes())
        data["m_InternalIds"][0] = "missing"
        with self.assertRaises(ValueError):
            catalog_sources(runtime_data.encode(data))

    def test_undecodable_cached_bundle_can_be_downloaded_again(self):
        self.check()
        self.hash = "b" * 32
        with patch.object(self.service, "_compile", side_effect=BundleError("corrupt LZ4")):
            self.assertEqual(self.check()["phase"], "error")
        self.calls.clear()
        self.assertEqual(self.check()["phase"], "ready")
        self.assertEqual(sum(count for (_, url), count in self.calls.items() if url.endswith(".bundle")), 3)


class BundleTests(unittest.TestCase):
    def test_lz4_overlapping_reference(self):
        self.assertEqual(lz4(b"\x11a\x01\x00", 6), b"aaaaaa")

    def test_lz4_invalid_offset_and_truncation(self):
        for data in (b"\x00\x00\x00", b"\xff", b"\x30ab"):
            with self.subTest(data=data), self.assertRaises(BundleError):
                lz4(data, 3)

    def test_lz4_output_size_is_bounded(self):
        with self.assertRaises(BundleError):
            lz4(b"\x11a\x01\x00", 5)

    def test_textasset_from_small_uncompressed_unity_bundle(self):
        def string(value):
            raw = value.encode()
            result = struct.pack("<i", len(raw)) + raw
            return result + b"\0" * (-len(result) % 4)
        obj = string("CHS") + string("Key@Value\nx@测试\n")
        meta = b"2022.3.62f2\0" + struct.pack("<i", 13) + b"\0" + struct.pack("<i", 1)
        meta += struct.pack("<iBh", 49, 0, -1) + b"\0" * 16 + struct.pack("<i", 1)
        meta += b"\0" * (-(48 + len(meta)) % 4)
        meta += struct.pack("<qqIi", 1, 0, len(obj), 0)
        offset = 48 + len(meta)
        header = struct.pack(">IIIIB3sIqqq", len(meta), 0, 22, 0, 0, b"\0" * 3, len(meta), offset + len(obj), offset, 0)
        serialized = header + meta + obj
        info = b"\0" * 16 + struct.pack(">iIIH", 1, len(serialized), len(serialized), 0)
        info += struct.pack(">iqqI", 1, 0, len(serialized), 4) + b"CAB-test\0"
        prefix = b"UnityFS\0" + struct.pack(">I", 8) + b"5.x.x\0" + b"2022.3.62f2\0"
        size = ((len(prefix) + 20 + 15) // 16) * 16 + len(info) + len(serialized)
        prefix += struct.pack(">qIII", size, len(info), len(info), 64)
        prefix += b"\0" * (-len(prefix) % 16)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.bundle"
            path.write_bytes(prefix + info + serialized)
            self.assertEqual(localization(text_assets(path, names={"CHS"})["CHS"]), {"x": "测试"})


if __name__ == "__main__":
    unittest.main()
