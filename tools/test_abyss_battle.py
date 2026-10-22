"""Native contract and protocol lifecycle regression tests; no live network."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import ssl
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend import abyss_battle as ai
from backend.abyss_state import activity_open, fantasy_config, initialize_units, sync_units, status_properties
from backend.game_client import GameClient, GameError
from backend.tasks import abyss_progress, abyss_scenes, abyss_scene_effects, normalize_abyss_config
from backend.abyss_properties import (artifact_properties, role_properties, formation_properties,
                                      final_literal_values)


def unit(uid, **fields):
    return {"id": uid, "NowHP": 100, "MaxHP": 100, "NowStatusList": [], **fields}


def event(item):
    return {"ActionResult": {"SkillEventList": [{"RoleEventList": [item]}]}}


class ScenePickerContracts(unittest.TestCase):
    def snapshot(self, ids):
        return {"SceneDataContainer": {"Scenes": [
            {"StaticID": sid, "Stars": [1, 0, 0], "PassCount": 2} for sid in ids]},
            "ActivityDBSettingContainer": {"SettingList": [
                {"ActivityID": identifier, "IsIgnoreTime": True}
                for identifier in ("Version_Fantasy", "Version_Fantasy01_1-6", "Fantasy_H189")]},
            "RoleDataContainer": {"Roles": [
                {"_id": {"$oid": "one"}, "StaticID": "H001"},
                {"_id": {"$oid": "two"}, "StaticID": "H002"}]}}

    def test_mixed_snapshot_only_exposes_ten_fictional_abyss_stages(self):
        state = self.snapshot([f"Abyss_{n}" for n in range(1, 54)]
                              + [f"MysteriousRealm_{n}" for n in range(10, 0, -1)])
        scenes = abyss_scenes(state)
        self.assertEqual([row["id"] for row in scenes], [f"MysteriousRealm_{n}" for n in range(1, 11)])
        self.assertEqual([row["floor"] for row in scenes], list(range(1, 11)))
        self.assertEqual(scenes[-1]["name"], "第 10 关")
        self.assertTrue(all(row["cleared"] and row["passCount"] == 2 for row in scenes))

    def test_virtual_realm_records_do_not_invent_unopened_fictional_stages(self):
        state = self.snapshot(["Abyss_1", "Abyss_53", "Abyss_80"])
        self.assertEqual(abyss_scenes(state), [])
        self.assertEqual(abyss_progress(state)["floor"], 80)
        self.assertEqual(abyss_scenes(None), [])

    def test_unknown_and_duplicate_records_are_excluded_and_next_stage_unlocks(self):
        state = self.snapshot(["MysteriousRealm_2", "MysteriousRealm_1", "MysteriousRealm_2",
                               "MysteriousRealm_99", "MysteriousRealm_0", "MysteriousRealm_01", "Other_2"])
        state["SceneDataContainer"]["Scenes"].extend([None, "invalid"])
        self.assertEqual([row["id"] for row in abyss_scenes(state)],
                         ["MysteriousRealm_1", "MysteriousRealm_2", "MysteriousRealm_3"])

    def test_first_attempt_uses_cleared_story_prerequisite_without_saved_scene(self):
        state = self.snapshot(["1Ext_3_10", "Abyss_53"])
        before = deepcopy(state)
        self.assertEqual(abyss_scenes(state), [{"id": "MysteriousRealm_1", "floor": 1,
                         "name": "第 1 关", "stars": [0, 0, 0], "passCount": 0, "cleared": False}])
        normalized = normalize_abyss_config(state, "MysteriousRealm_1", {"upper": ["one"], "lower": ["two"]})
        start = ai._start_info(state, normalized["sceneId"], {"upper": [], "lower": []})
        # The exact SceneData sent by the real client's first attempt in the capture.
        self.assertEqual(start["SceneData"], {"StaticID": "MysteriousRealm_1", "Stars": [0, 0, 0], "PassCount": 0})
        self.assertEqual(state, before)
        with self.assertRaisesRegex(GameError, "未解锁"):
            ai._scene(state, "MysteriousRealm_2")

    def test_missing_or_failed_story_prerequisite_keeps_first_stage_locked(self):
        state = self.snapshot(["1Ext_3_10", "Abyss_80"])
        state["SceneDataContainer"]["Scenes"][0]["Stars"] = [0, 1, 1]
        self.assertEqual(abyss_scenes(state), [])
        with self.assertRaisesRegex(GameError, "未解锁"):
            ai._scene(state, "MysteriousRealm_1")

    def test_entered_but_uncleared_stage_does_not_unlock_next_stage(self):
        state = self.snapshot(["MysteriousRealm_1"])
        state["SceneDataContainer"]["Scenes"][0]["Stars"] = [0, 0, 0]
        self.assertEqual([row["id"] for row in abyss_scenes(state)], ["MysteriousRealm_1"])

    def test_inactive_scene_and_fantasy_schedules_exclude_first_attempts(self):
        for inactive in ("Version_Fantasy", "Version_Fantasy01_1-6"):
            state = self.snapshot(["1Ext_3_10"])
            state["ActivityDBSettingContainer"]["SettingList"] = [
                row for row in state["ActivityDBSettingContainer"]["SettingList"]
                if row["ActivityID"] != inactive]
            self.assertEqual(abyss_scenes(state), [])

    def test_confirmed_first_clear_unlocks_only_next_stage_and_preserves_record(self):
        client = GameClient.__new__(GameClient)
        client.account_state = self.snapshot(["1Ext_3_10"])
        reward = {"SceneData": {"_id": "confirmed-first-clear", "StaticID": "MysteriousRealm_1",
                                "Stars": [1, 1, 1, 1], "PassCount": 0}}
        client.apply_scene_reward(reward)
        self.assertEqual([row["id"] for row in abyss_scenes(client.account_state)],
                         ["MysteriousRealm_1", "MysteriousRealm_2"])
        self.assertEqual(ai._scene(client.account_state, "MysteriousRealm_1"), reward["SceneData"])

    def test_saved_virtual_realm_id_is_rejected_but_valid_teams_still_normalize(self):
        state = self.snapshot(["Abyss_1", "MysteriousRealm_1"])
        waves = {"upper": ["one"], "lower": ["two"]}
        with self.assertRaisesRegex(GameError, "虚构深渊关卡"):
            normalize_abyss_config(state, "Abyss_1", waves)
        normalized = normalize_abyss_config(state, "MysteriousRealm_1", waves)
        self.assertEqual(normalized, {"sceneId": "MysteriousRealm_1", "waves": {
            "upper": ["one", None, None, None], "lower": ["two", None, None, None]}})

    def test_options_save_and_run_share_the_stage_boundary(self):
        from backend import appcore
        acc = SimpleNamespace(email="offline@example.invalid",
                              client=SimpleNamespace(account_state=self.snapshot(["Abyss_1", "MysteriousRealm_1"])),
                              params={"abyss_auto": {"sceneId": "Abyss_1", "waves": {"upper": ["one"], "lower": ["two"]}}},
                              state_revision=1, state_synced_at=0, state_sync_error=None)
        with patch.object(appcore, "_require", return_value=acc), \
             patch.object(appcore, "run_task") as run, patch.object(appcore.config, "save_account") as save:
            options = appcore.abyss_options(acc.email)
            self.assertEqual([scene["id"] for scene in options["scenes"]], ["MysteriousRealm_1", "MysteriousRealm_2"])
            self.assertEqual(options["config"]["sceneId"], "")
            for action in (appcore.abyss_set, appcore.abyss_run):
                with self.assertRaises(appcore.ApiError) as raised:
                    action(acc.email, "Abyss_1", acc.params["abyss_auto"]["waves"])
                self.assertEqual(raised.exception.status, 400)
            run.assert_not_called()
            save.assert_not_called()

    def test_effects_follow_active_account_schedule_and_source_order(self):
        state = self.snapshot(["MysteriousRealm_7"])
        before = deepcopy(state)
        effects = abyss_scene_effects(state, "MysteriousRealm_7")
        # The helpful effect is applied to an enemy; classify by Bs, not Target.
        bonus = effects["upper"]["bonuses"][1]
        self.assertEqual(bonus["id"], "FG_Special_031")
        self.assertIn("不屈", bonus["description"])
        self.assertEqual(effects["upper"]["restrictions"][0]["id"], "FB_Job_005")
        self.assertEqual(state, before)
        other = deepcopy(state)
        other["ActivityDBSettingContainer"]["SettingList"][-1]["ActivityID"] = "Fantasy_H610"
        self.assertEqual(abyss_scene_effects(other, "MysteriousRealm_7")["upper"]["bonuses"][0]["id"],
                         "FG_Job_003")
        state["ActivityDBSettingContainer"]["SettingList"].extend(other["ActivityDBSettingContainer"]["SettingList"][-1:])
        self.assertEqual(abyss_scene_effects(state, "MysteriousRealm_7"), effects)

    def test_missing_effect_description_is_explicit_and_never_inferred(self):
        from backend import tasks
        state = self.snapshot(["MysteriousRealm_7"])
        state["ActivityDBSettingContainer"]["SettingList"][-1]["ActivityID"] = "Fantasy_H612"
        missing = next(row for row in abyss_scene_effects(state, "MysteriousRealm_7")["upper"]["bonuses"]
                       if row["id"] == "FG_Special_021")
        self.assertEqual(missing, {"id": "FG_Special_021", "description": "效果说明暂缺", "descriptionMissing": True})
        with patch.object(tasks, "_LOCALIZATION", {}):
            unavailable = abyss_scene_effects(self.snapshot([]), "MysteriousRealm_1")
        self.assertTrue(all(effect["descriptionMissing"] for wave in unavailable.values()
                            for group in wave.values() for effect in group))

    def test_options_expose_localized_effects_for_unlocked_stages_without_requests(self):
        from backend import appcore
        client = SimpleNamespace(account_state=self.snapshot(["1Ext_3_10"]), call=Mock())
        acc = SimpleNamespace(email="offline@example.invalid", client=client, params={},
                              state_revision=1, state_synced_at=0, state_sync_error=None)
        before = deepcopy(client.account_state)
        with patch.object(appcore, "_require", return_value=acc):
            scenes = appcore.abyss_options(acc.email)["scenes"]
        self.assertEqual([scene["id"] for scene in scenes], ["MysteriousRealm_1"])
        effects = scenes[0]["effects"]
        self.assertEqual(effects["upper"]["bonuses"][0]["description"],
                         "配置「狙击」职业的团员时，全体成员造成的伤害量提升 25%")
        self.assertEqual(effects["lower"]["restrictions"][0]["description"], "禁止团员造成「追加回合」")
        self.assertEqual(client.account_state, before)
        client.call.assert_not_called()


class StateContracts(unittest.TestCase):
    def test_android_asset_root_reads_the_bundled_battle_table(self):
        from backend.abyss_state import metadata
        original = {**metadata(), "version": "android-test-root"}
        self.addCleanup(metadata.cache_clear)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "assets/abyss_battle_static.json"
            path.parent.mkdir()
            path.write_text(json.dumps(original), encoding="utf-8")
            metadata.cache_clear()
            with patch.dict("os.environ", {"ARK_ASSET_ROOT": temporary}):
                self.assertEqual(metadata()["version"], "android-test-root")

    def test_trusted_empty_collections_and_scalar_defaults_replace_old_state(self):
        units = {"1-0-0": unit("1-0-0", NowIsDieOut=True, NowSP=5, NowStatusList=[{"StaticID": "Stun"}], NowSkillCooldownMap={"S": 4})}
        sync_units(event({"TargetRoleID": "1-0-0", "IsCanSyncRole": 1, "NowHP": 12}), units)
        self.assertEqual(units["1-0-0"]["NowStatusList"], [])
        self.assertEqual(units["1-0-0"]["NowSkillCooldownMap"], {})
        self.assertEqual(units["1-0-0"]["NowSP"], 0)
        self.assertFalse(units["1-0-0"]["NowIsDieOut"])

    def test_display_only_event_and_heartbeat_leave_state_intact(self):
        units = {"1-0-0": unit("1-0-0", NowSkillCooldownMap={"S": 4})}
        before = deepcopy(units)
        for msg in (event({"TargetRoleID": "1-0-0", "IsCanSyncRole": 0, "NowHP": 0}), {"HB": 0}):
            sync_units(msg, units)
        self.assertEqual(units, before)

    def test_pre_enemy_count_never_creates_an_opposing_role(self):
        units = {"1-0-0": unit("1-0-0")}
        sync_units({"ActionResult": {"SkillEventList": [{"PreEnemyCount": 4}]}}, units)
        self.assertEqual(list(units), ["1-0-0"])

    def test_time_map_updates_real_roles_without_creating_unknown_identities(self):
        units = {"2-1-5": unit("2-1-5")}
        sync_units({"RoundResult": {"RoleTimeMap": {"2-1-5": 0.9, "2-1-6": 0.1}}}, units)
        self.assertEqual(units["2-1-5"]["RoleTimeInfo"], {"NowTimeLength": 0.9})
        self.assertNotIn("2-1-6", units)

    def test_roster_uses_static_monster_identity_position_and_boss_flag(self):
        start = {"WaveCampDatas": [{"PositionRoleMap": {"0": {"StaticID": "H", "Skills": {"Skills": []}}}}, {"PositionRoleMap": {}}]}
        fantasy = {"Waves": [{"Monsters": [{"M": "FH", "Pos": 5, "Boss": True}]}, {"Monsters": [{"M": "FH", "Pos": 0}]}]}
        tables = {"Role": {"H": {"RolePropertyID": "Hero"}, "FH": {"Element": "Ice", "SkillIDs": "S1~S2:Hide", "RolePropertyID": "Hero"}},
                  "RoleProperty": {"Hero": {"1": {"HP": "100", "Attack": "10", "Defence": "10", "Speed": "10"}}},
                  "Status": {}, "RoleAwaken": {}, "RoleImprint": {}, "EquipmentSet": {}, "Artifact": {}, "Skill": {}, "SkillLevel": {}}
        units = initialize_units(start, fantasy, tables)
        self.assertEqual(set(units), {"1-0-0", "2-0-5", "2-1-0"})
        self.assertEqual(units["2-0-5"]["StaticID"], "FH")
        self.assertTrue(units["2-0-5"]["IsBoss"])
        self.assertEqual(units["2-0-5"]["Skills"]["Skills"][1]["StaticID"], "S2")
        self.assertGreater(units["2-0-5"]["MaxHP"], 0)
        self.assertEqual(units["2-0-5"]["NowHP"], units["2-0-5"]["MaxHP"])

    def test_status_properties_use_real_layers_and_static_flags(self):
        tables = {"Status": {"HiddenID": {"Dynamic": {"DynamicField1": {"__type__": "hash", "name": "Dizzy", "value": 1}}, "IsMergeProperty": "TRUE", "IsStackMultiplyProp": "TRUE"}}}
        self.assertEqual(status_properties(unit("1", NowStatusList=[{"StaticID": "HiddenID", "NowStack": 3}]), tables)["Dizzy"], 3)
        tables["Status"]["HiddenID"]["IsMergeProperty"] = "FALSE"
        self.assertEqual(status_properties(unit("1", NowStatusList=[{"StaticID": "HiddenID", "NowStack": 3}]), tables)["Dizzy"], 1)

    def test_trusted_role_event_overrides_round_result_in_the_same_frame(self):
        units = {"1-0-0": unit("1-0-0")}
        msg = event({"TargetRoleID": "1-0-0", "IsCanSyncRole": 1, "NowTime": 0.2})
        msg["RoundResult"] = {"RoleTimeMap": {"1-0-0": 0.9}, "SkillCooldownMap": {"1-0-0": {"S": 4}}}
        sync_units(msg, units)
        self.assertEqual(units["1-0-0"]["NowSkillCooldownMap"], {})
        self.assertEqual(units["1-0-0"]["RoleTimeInfo"]["NowTimeLength"], 0.2)


class ActivityContracts(unittest.TestCase):
    def setUp(self):
        self.tables = {"Activity": {}, "Fantasy": {"S": [
            {"ActivityIDs": ["A", "B"], "tag": "first"}, {"ActivityIDs": ["A"], "tag": "second"}]}}
        self.state = {"ActivityDBSettingContainer": {"SettingList": [
            {"ActivityID": "A", "StartTime": {"$date": 0}, "EndTime": {"$date": 7200000}}]}}

    def test_all_activity_ids_must_be_open_and_source_order_is_preserved(self):
        self.assertEqual(fantasy_config("S", self.state, now=100, tables=self.tables)["tag"], "second")
        self.tables["Activity"]["B"] = {"IsUnlimitTime": "1"}
        self.assertEqual(fantasy_config("S", self.state, now=100, tables=self.tables)["tag"], "first")

    def test_nonrepeat_endpoints_are_inclusive(self):
        for now, expected in ((-1, False), (0, True), (7200, True), (7201, False)):
            self.assertEqual(activity_open("A", self.state, now=now, tables=self.tables), expected)

    def test_repeat_open_hours_and_off_days_match_native_truncation(self):
        self.state["ActivityDBSettingContainer"]["SettingList"][0].update(IsRepeat=True, OffDays=1)
        for hours, expected in ((1, True), (3, False), (25, False), (26, True), (28, True), (29, False)):
            self.assertEqual(activity_open("A", self.state, now=hours * 3600, tables=self.tables), expected)

    def test_no_active_configuration_is_an_explicit_error(self):
        with self.assertRaises(GameError):
            fantasy_config("S", self.state, now=9000, tables=self.tables)


class LiteralPropertyContracts(unittest.TestCase):
    def setUp(self):
        self.tables = {"Role": {"H": {"RolePropertyID": "Hero", "HP": "2", "Attack": "1.5", "Defence": "1", "Speed": "1",
                                      "SelfImprint": "Self", "TeamImprint": "Team", "ImprintPositions": "0~1"}},
                       "RoleProperty": {"Hero": {"60": {"HP": "1000", "Attack": "100", "Defence": "100", "Speed": "100"}}},
                       "RoleAwaken": {"H": {"1": {"HPValue": "50"}, "2": {"AttackRate": "0.1"}}},
                       "RoleImprint": {key: {"Dynamic": {"Base.DynamicField1": {"__type__": "hash", "name": "HPRate", "value": 0},
                                                            "LevelAdd.DynamicField1": {"__type__": "hash", "name": "HPRate", "value": value}}}
                                       for key, value in (("Self", 0.05), ("Team", 0.1))},
                       "EquipmentSet": {"Health": {"Count": "2", "HPRate": "0.2"}},
                       "Artifact": {"A": {"Base.HPValue": "32", "Max.HPValue": "416", "Base.AttackValue": "21", "Max.AttackValue": "273"}},
                       "PublicVariable": {}}
        self.role = {"_id": "one", "StaticID": "H", "LV": 60, "Star": 6}

    def test_native_star_multiplier_and_public_variable_override(self):
        self.role["Star"] = 5
        values = final_literal_values(role_properties(self.role, self.tables).values)
        self.assertEqual((values["HP"], values["Attack"], values["Speed"]), (1888, 141, 100))
        self.tables["PublicVariable"]["RoleStarRate"] = {"VALUE": "0.1"}
        self.assertEqual(final_literal_values(role_properties(self.role, self.tables).values)["HP"], 1800)

    def test_equipment_repeated_sets_self_imprint_and_cumulative_awaken(self):
        self.role.update(IsSelfImprint=True, ImprintLV=2, AwakenLV=2,
                         EquipmentMap={str(i): {"Set": "Health", "MainProp": {"PropertyType": "HPValue", "Value": 100}} for i in range(4)})
        values = final_literal_values(role_properties(self.role, self.tables).values)
        self.assertEqual(values["HP"], 3350)
        self.assertEqual(values["Attack"], 165)

    def test_artifact_endpoints_and_level_bonus_count(self):
        self.assertEqual(artifact_properties({"StaticID": "A", "LV": 1}, self.tables).values["HPValue"], 32)
        self.assertEqual(artifact_properties({"StaticID": "A", "LV": 30}, self.tables).values["HPValue"], 416)
        self.tables["Artifact"]["A"]["Dynamic"] = {"LVProp.DynamicField1": {"__type__": "hash", "name": "CriticalRate", "value": 0.1}}
        self.assertAlmostEqual(artifact_properties({"StaticID": "A", "LV": 8}, self.tables).values["CriticalRate"], 0.2)

    def test_formation_excludes_self_and_applies_only_to_imprint_positions(self):
        self.role.update(ImprintLV=2, IsSelfImprint=False)
        teammate = {**self.role, "_id": "other"}
        team = {"0": self.role, "1": teammate}
        self.assertAlmostEqual(formation_properties(self.role, 0, team, self.tables).values["HPRate"], 0.1)
        self.assertEqual(formation_properties(self.role, 3, team, self.tables).values, {})
        teammate["IsSelfImprint"] = True
        self.assertEqual(formation_properties(self.role, 0, team, self.tables).values, {})

    def test_final_rounding_and_injury_preserve_native_numeric_paths(self):
        values = final_literal_values({"HP": 100.5, "Attack": 100.9, "Defence": 99.9, "Speed": 101.5}, injury_rate=0.25)
        self.assertEqual(tuple(values[k] for k in ("HP", "InjuredHP", "Attack", "Defence", "Speed")), (100, 75, 100, 99, 102))

    def test_missing_source_is_explicit_and_functions_are_deferred(self):
        self.tables["Artifact"]["A"]["Dynamic"] = {"Base.DynamicField1": {"__type__": "hash", "name": "Fun", "value": {"Property": "HP", "Value": 99}}}
        props = artifact_properties({"StaticID": "A", "LV": 1}, self.tables)
        self.assertEqual(props.values["HPValue"], 32)
        self.assertEqual(props.deferred, ["Artifact.A.Base.DynamicField1"])
        self.role["LV"] = 61
        with self.assertRaisesRegex(GameError, "等级属性"):
            role_properties(self.role, self.tables)


class ExecutorRegistryContracts(unittest.TestCase):
    def setUp(self):
        from backend import appcore
        self.core = appcore
        self.acc = SimpleNamespace(email="offline@example.invalid", client=SimpleNamespace(account_state={}),
                                   params={}, toggles={}, lock=threading.Lock(), notify_config_changed=Mock())
        self.normalized = {"sceneId": "S", "waves": {}}
        self.require = patch.object(appcore, "_require", return_value=self.acc).start()
        self.normalize = patch.object(appcore, "normalize_abyss_config", return_value=self.normalized).start()
        self.save = patch.object(appcore.config, "save_account").start()
        self.addCleanup(patch.stopall)
        self.addCleanup(appcore._abyss_sessions.pop, self.acc.email, None)

    def test_duplicate_run_rejects_before_persisting_another_config(self):
        self.core._abyss_sessions[self.acc.email] = threading.Event()
        with self.assertRaises(self.core.ApiError) as raised:
            self.core.abyss_run(self.acc.email, "S", {})
        self.assertEqual(raised.exception.status, 409)
        self.save.assert_not_called()

    def test_cancel_does_not_wait_on_account_lock_and_failure_releases_session(self):
        def run(client, task, params):
            self.assertTrue(self.acc.lock.locked())
            self.assertTrue(self.core.abyss_cancel(self.acc.email)["running"])
            self.assertTrue(params["_cancel_event"].is_set())
            raise GameError("cancelled")
        with patch.object(self.core, "run_task", side_effect=run):
            with self.assertRaises(self.core.ApiError):
                self.core.abyss_run(self.acc.email, "S", {})
        self.assertNotIn(self.acc.email, self.core._abyss_sessions)
        self.assertFalse(self.acc.lock.locked())

    def test_cancel_while_queued_prevents_task_from_starting(self):
        core, email = self.core, self.acc.email
        class QueueLock:
            def __enter__(self): core.abyss_cancel(email)
            def __exit__(self, *args): pass
        self.acc.lock = QueueLock()
        with patch.object(core, "run_task") as run:
            with self.assertRaises(core.ApiError):
                core.abyss_run(email, "S", {})
        run.assert_not_called()
        self.assertNotIn(email, core._abyss_sessions)

    def test_config_write_failure_also_releases_session(self):
        self.save.side_effect = OSError("offline fixture")
        with self.assertRaises(OSError):
            self.core.abyss_run(self.acc.email, "S", {})
        self.assertNotIn(self.acc.email, self.core._abyss_sessions)

    def test_finished_run_returns_settlement_status_and_can_run_again(self):
        with patch.object(self.core, "run_task", return_value={"ok": True, "detail": "finished"}), \
             patch.object(self.core, "_status_snapshot", return_value={"status": {"gold": 123}}):
            for _ in range(2):
                result = self.core.abyss_run(self.acc.email, "S", {})
                self.assertEqual(result["status"], {"gold": 123})
                self.assertNotIn(self.acc.email, self.core._abyss_sessions)

    def test_cancel_http_dispatch_uses_account_without_running_a_battle(self):
        from backend import server
        with patch.object(self.core, "abyss_cancel", return_value={"ok": True}) as cancel:
            self.assertEqual(server._route_api("POST", "/api/abyss/cancel", {"account": self.acc.email}, {}), {"ok": True})
        cancel.assert_called_once_with(self.acc.email)

    def test_run_http_dispatch_reaches_the_client_derived_executor(self):
        from backend import server
        with patch.object(ai, "run_abyss_battle", return_value={"ok": True, "detail": "finished"}) as run, \
             patch.object(self.core, "_status_snapshot", return_value={"status": {"gold": 123}}):
            result = server._route_api("POST", "/api/abyss/run", {
                "account": self.acc.email, "sceneId": "S", "waves": {}}, {})
        client, params = run.call_args.args
        self.assertIs(client, self.acc.client)
        self.assertEqual(params["sceneId"], "S")
        self.assertEqual(params["waves"], {})
        self.assertIsInstance(params["_cancel_event"], threading.Event)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], {"gold": 123})
        self.assertNotIn(self.acc.email, self.core._abyss_sessions)

    def test_executor_failure_is_reported_without_starting_another_engine(self):
        with patch.object(ai, "run_abyss_battle", side_effect=GameError("缺少深渊战斗静态资料")) as run:
            with self.assertRaisesRegex(self.core.ApiError, "静态资料"):
                self.core.abyss_run(self.acc.email, "S", {})
        run.assert_called_once()
        self.assertNotIn(self.acc.email, self.core._abyss_sessions)


class DecisionContracts(unittest.TestCase):
    def setUp(self):
        ai._AI_RNG.seed(3)

    def test_comparison_boundaries_and_value2(self):
        for comparer, expected in (("Eq", True), ("Ne", False), ("Lt", True), ("Gt", True), ("Ltn", False), ("Gtn", False)):
            self.assertEqual(ai._compare(2, {"Comparer": comparer, "Value": 2}), expected, comparer)
        self.assertFalse(ai._compare(3, {"Comparer": "Eq", "Value": 2, "Value2": 4}))
        self.assertTrue(ai._compare(3, {"Comparer": "Ne", "Value": 2, "Value2": 4}))
        self.assertTrue(ai._compare(0, {"Comparer": "Between", "Value": 2, "Value2": 0}))

    def test_target_collection_any_all_and_empty(self):
        source, other = unit("1-0-0"), unit("1-0-1", NowStatusList=[{"StaticID": "Stun"}])
        condition = {"TargetInfo": {"TargetType": "All", "TargetCamp": "My"}, "Property": "DeBuffCount", "Comparer": "Eq", "Value": 0}
        self.assertTrue(ai._condition_ok(condition, source, [source, other], []))
        self.assertFalse(ai._condition_ok({**condition, "IsAllWhoPass": True}, source, [source, other], []))
        condition["TargetInfo"]["NoSelf"] = True
        self.assertFalse(ai._condition_ok(condition, source, [source], []))

    def test_ally_count_includes_self_and_debuff_names_are_case_insensitive(self):
        source = unit("1-0-0", NowStatusList=[{"StaticID": "Stun"}])
        allies = [source, unit("1-0-1", NowHP=0)]
        self.assertTrue(ai._condition_ok({"Property": "AllyCount", "Comparer": "Eq", "Value": 1}, source, allies, []))
        for prop in ("DeBuffCount", "DebuffCount"):
            self.assertTrue(ai._condition_ok({"Property": prop, "Comparer": "Eq", "Value": 1}, source, allies, []))

    def test_none_status_is_not_a_debuff(self):
        self.assertFalse(ai._has_status(unit("1", NowStatusList=[{"StaticID": "FG_Special_031"}]), debuff=True))
        self.assertTrue(ai._has_status(unit("1", NowStatusList=[{"StaticID": "Stun"}]), debuff=True))

    def test_three_target_passes_do_not_accumulate_flags(self):
        source = unit("1-0-0")
        both = unit("2-0-0", IsStealth=True, IsInvincible=True)
        rule = {"Targets": [{"Type": "Boss"}, {"Type": "LessHPRate"}]}
        self.assertIsNone(ai._target(rule, source["id"], source, [source], [both]))
        self.assertEqual(ai._target(rule, source["id"], source, [source], [unit("2-0-1", IsStealth=True)]), "2-0-1")

    def test_ally_selectors_include_stealth_and_invincible_by_default(self):
        source, ally = unit("1-0-0"), unit("1-0-1", IsStealth=True, IsInvincible=True, NowHP=20)
        self.assertEqual(ai._target({"TargetCamp": "My", "Targets": [{"Type": "LessHPRate"}]}, source["id"], source, [source, ally], []), ally["id"])

    def test_conquer_ok_skips_only_reverse_conquered_pool(self):
        source = unit("1-0-0", Element="Fire")
        self.assertIsNone(ai._target({"Targets": [{"Type": "ConquerOKTarget"}]}, source["id"], source, [source], [unit("2-0-0", Element="Earth")]))

    def test_silence_normal_attack_uses_separate_gate(self):
        source = unit("1-0-0", StaticID="H001", NowSP=0, NowStatusList=[{"StaticID": "Silence"}], NowSkillCooldownMap={"H001S1": 5},
                      Skills={"Skills": [{"StaticID": "H001S1"}, {"StaticID": "H001S2"}]})
        self.assertTrue(ai._skill_can_use(source, {"StaticID": "H001S1", "SP": -99}, [source], []))
        self.assertFalse(ai._skill_can_use(source, {"StaticID": "H001S2"}, [source], []))

    def test_form_replacement_preserves_both_ids_levels_and_normal_attack(self):
        source = unit("1", StaticID="H003", NowStatusList=[{"StaticID": "H003S3"}, {"StaticID": "Silence"}],
                      Skills={"Skills": [{"StaticID": sid, "Level": i + 2} for i, sid in enumerate(["H003S1", "H003S2", "H003S3", "H003S4", "H003S5"])]})
        self.assertEqual([skill["StaticID"] for skill in ai._live_skills(source)], ["H003S4", "H003S5"])
        self.assertEqual([skill["Level"] for skill in ai._live_skills(source)], [5, 6])
        self.assertTrue(ai._skill_can_use(source, {"StaticID": "H003S4"}, [source], []))
        self.assertFalse(ai._skill_can_use(source, {"StaticID": "H003S1"}, [source], []))

    def test_will_pass_round_and_silence_are_different_gates(self):
        self.assertFalse(ai._can_act(unit("1", NowStatusList=[{"StaticID": "Stun"}])))
        self.assertTrue(ai._can_act(unit("1", NowStatusList=[{"StaticID": "Silence"}])))

    def test_negative_index_uses_last_live_skill_and_runtime_ai_wins(self):
        tables = {"tables": {"Role": {"H": {"AIs": "Wrong"}}, "Status": {}, "Skill": {},
                             "AIAction": {"Runtime": [{"Priority": 1, "SkillIndex": -7, "Targets": [{"Type": "One"}]}]}}}
        source = unit("1", StaticID="H", Skills={"Skills": [{"StaticID": "S1"}, {"StaticID": "FormS2", "Level": 4}]})
        with patch.object(ai, "_battle_metadata", return_value=tables), patch.object(ai, "status_properties", return_value={"AI": "Runtime"}):
            self.assertEqual(ai._choose_action("H", "1", source, [source], [unit("2")]), ("FormS2", 4, "2"))

    def test_normal_fallback_score_is_hp_and_native_weights(self):
        source = unit("1", StaticID="H001", Element="Fire", Skills={"Skills": [{"StaticID": "H001S1", "Level": 1}]})
        enemies = [unit("2a", Element="Fire", NowHP=5), unit("2b", Element="Ice", NowHP=500)]
        self.assertEqual(ai._fallback_action(source, [source], enemies)[2], "2b")

    def test_soul_requires_selected_skill_cost_and_camp_balance(self):
        source = unit("1", StaticID="H001")
        with patch.object(ai, "_skill_static", return_value={"SkillSoulCost": "10"}):
            self.assertFalse(ai._can_use_soul(source, "S", 9, [source], [unit("2")]))
            self.assertTrue(ai._can_use_soul(source, "S", 10, [source], [unit("2")]))


class SettlementContracts(unittest.TestCase):
    def test_reward_merges_confirmed_balances_and_preserves_unreturned_data(self):
        client = GameClient.__new__(GameClient)
        client.account_state = {"Info": {"LV": 1, "LoginTime": "keep"}, "AccountSaveData": {"keep": 1},
                                "ItemContainer": {"Items": [{"StaticID": "1", "Count": 10}]},
                                "RoleDataContainer": {"Roles": [{"_id": {"$oid": "r"}, "StaticID": "H", "IsFavorite": True}]},
                                "SceneDataContainer": {"Scenes": [{"StaticID": "S", "Stars": [0], "keep": 1}]},
                                "LineRoleDataContainer": {"RoleList": [{"Info": {"StaticID": "H", "Intimacy": 0, "keep": 1}}]}}
        body = {"AccountInfo": {"LV": 2}, "AccountSaveData": {"LastPassScene": "S"}, "GotStars": 4,
                "CostItems": [{"NowItem": {"StaticID": "1", "Count": 5}}],
                "Drop": {"Items": [{"Item": {"Count": 100}, "NowItem": {"StaticID": "1", "Count": 8}}]},
                "SceneData": {"StaticID": "S", "Stars": [1, 1, 1, 1]},
                "Roles": [{"_id": "r", "Exp": 3}], "Quests": [{"StaticID": "Q", "Count": 1}],
                "LineRoleDatas": [{"Info": {"StaticID": "H", "Intimacy": 5}}]}
        client.apply_scene_reward(body)
        client.apply_scene_reward(body)  # Absolute balances don't double-award.
        state = client.account_state
        self.assertEqual(state["ItemContainer"]["Items"][0]["Count"], 8)
        self.assertEqual(state["Info"], {"LV": 2, "LoginTime": "keep"})
        self.assertEqual(len(state["RoleDataContainer"]["Roles"]), 1)
        self.assertTrue(state["RoleDataContainer"]["Roles"][0]["IsFavorite"])
        self.assertEqual(state["SceneDataContainer"]["Scenes"][0]["keep"], 1)
        self.assertEqual(state["LineRoleDataContainer"]["RoleList"][0]["Info"]["keep"], 1)


class ProgressContracts(unittest.TestCase):
    def test_health_projection_keeps_positions_waves_and_death_without_mutating_units(self):
        units = {
            "1-0-2": {"Position":2,"StaticID":"H002","NowHP":25,"MaxHP":200},
            "1-0-0": {"Position":0,"StaticID":"H001","NowHP":0,"MaxHP":100},
            "2-0-0": {"Position":0,"StaticID":"FH057","NowHP":500,"MaxHP":1000},
            "1-1-0": {"Position":0,"StaticID":"H003","NowHP":300,"MaxHP":200},
            "2-1-0": {"Position":0,"StaticID":"FH029","NowHP":10,"MaxHP":100,"NowIsDieOut":True},
        }
        before = deepcopy(units)
        upper = ai._progress_snapshot("S", 0, units)
        self.assertEqual([member["id"] for member in upper["allies"]], ["1-0-0", "1-0-2"])
        self.assertEqual([member["hpPercent"] for member in upper["allies"]], [0, 12.5])
        self.assertTrue(upper["allies"][0]["dead"])
        self.assertEqual(upper["enemies"][0]["hpPercent"], 50)
        lower = ai._progress_snapshot("S", 1, units)
        self.assertEqual(lower["wave"], 1)
        self.assertEqual(lower["allies"][0]["hpPercent"], 100)
        self.assertEqual(lower["enemies"][0]["hpPercent"], 0)
        self.assertTrue(lower["enemies"][0]["dead"])
        self.assertEqual(units, before)

    def test_session_snapshot_is_isolated_readable_and_cleared_on_every_exit(self):
        from backend import appcore
        for outcome in ({"ok":True}, {"ok":False}, GameError("cancelled"), RuntimeError("offline")):
            with self.subTest(outcome=outcome):
                acc = SimpleNamespace(email="progress@example.invalid", client=SimpleNamespace(account_state={}), params={}, toggles={},
                                      lock=threading.Lock(), notify_config_changed=Mock(), logs_since=lambda since: [],
                                      state_revision=1, state_synced_at=0, state_sync_error=None,
                                      guild_snapshot=lambda: {})
                normalized = {"sceneId":"MysteriousRealm_1","waves":{"upper":["one"],"lower":["two"]}}
                callbacks = []
                def run(client, task_id, params):
                    self.assertTrue(acc.lock.locked())
                    callbacks.append(params["_on_progress"])
                    snapshot = {"sceneId":"MysteriousRealm_1","wave":0,"allies":[],"enemies":[
                        {"id":"2-0-0","staticId":"FH057_H145","hpPercent":50,"dead":False}]}
                    params["_on_progress"](snapshot)
                    state = appcore._abyss_snapshot(acc.email)
                    self.assertTrue(state["running"])
                    self.assertEqual(state["progress"]["enemies"][0]["avatar"], "/assets/avatars/H057.png")
                    with patch.object(appcore.accounts, "get", return_value=acc), patch.object(appcore, "_inventory_meta", return_value={}):
                        self.assertEqual(appcore.auto_logs(acc.email)["abyssBattle"], state)
                    self.assertEqual(appcore._abyss_snapshot("other@example.invalid"), {"running":False,"progress":None})
                    self.assertEqual(acc.params["abyss_auto"], normalized)
                    self.assertNotIn("_on_progress", acc.params["abyss_auto"])
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
                with patch.object(appcore,"_require",return_value=acc), \
                     patch.object(appcore,"normalize_abyss_config",return_value=normalized), \
                     patch.object(appcore.config,"save_account"), \
                     patch.object(appcore,"_status_snapshot",return_value={}), \
                     patch.object(appcore,"run_task",side_effect=run):
                    if isinstance(outcome, Exception):
                        with self.assertRaises((appcore.ApiError, RuntimeError)):
                            appcore.abyss_run(acc.email, normalized["sceneId"], normalized["waves"])
                    else:
                        self.assertEqual(appcore.abyss_run(acc.email, normalized["sceneId"], normalized["waves"])["ok"], outcome["ok"])
                self.assertEqual(appcore._abyss_snapshot(acc.email), {"running":False,"progress":None})
                callbacks[0]({"wave":0,"allies":[],"enemies":[]})
                self.assertEqual(appcore._abyss_snapshot(acc.email), {"running":False,"progress":None})


class SocketLifecycle(unittest.IsolatedAsyncioTestCase):
    async def run_socket(self, messages, *, clock=None, cancel=None, cancel_on_recv=False,
                         domain="wss://offline.invalid/", ca_file=None, check_tls=False, on_progress=None):
        class Client:
            account_state = {}
            aid = "account"
            applied = []
            def _auth_data(self, value): return value
            def call(self, *args): return {"_id": "room", "AID": self.aid, "ServerID": "server", "Domain": domain}
            def apply_scene_reward(self, body): self.applied.append(body)
        class Socket:
            closed = False
            sent = []
            async def __aenter__(self): return self
            async def __aexit__(self, *args): self.closed = True
            async def send(self, raw): self.sent.append(json.loads(raw))
            async def recv(self):
                if cancel_on_recv:
                    cancel.set()
                return json.dumps(messages.pop(0)) if messages else None
        client, socket = Client(), Socket()
        start = {"WaveCampDatas": [{"PositionRoleMap": {}}, {"PositionRoleMap": {}}]}
        fantasy = {"Waves": [{"Monsters": []}, {"Monsters": []}]}
        with patch("websockets.legacy.client.connect", return_value=socket) as connect, patch.object(ai, "_start_info", return_value=start), \
             patch.dict("os.environ", {"SSL_CERT_FILE": ca_file} if ca_file else {}), \
             patch.object(ai, "fantasy_config", return_value=fantasy), patch.object(ai, "monotonic", side_effect=clock or (lambda: 0)):
            try:
                result = await ai._run(client, "S", {}, cancel_event=cancel, on_progress=on_progress)
            except GameError as error:
                result = error
        self.assertTrue(socket.closed)
        if check_tls and domain.startswith("wss://"):
            context = connect.call_args.kwargs["ssl"]
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            expected = ssl.create_default_context(cafile=ca_file) if ca_file else ai.httpx.create_ssl_context()
            self.assertEqual(context.get_ca_certs(binary_form=True), expected.get_ca_certs(binary_form=True))
        elif check_tls:
            self.assertNotIn("ssl", connect.call_args.kwargs)
        return result, socket.sent, client.applied

    async def test_progress_uses_server_wave_index_and_does_not_repeat_on_heartbeats(self):
        snapshots = []
        result, _, _ = await self.run_socket([
            {"HB":0}, {"RoundResult":{"NowWaveIndex":0}},
            {"Step":"NextWave", "RoundResult":{"NowWaveIndex":1,"NowRoundRoleID":"1-0-2"}},
            {"HB":0}, {"BattleEndData":{"Result":"Win"}},
        ], on_progress=snapshots.append)
        self.assertTrue(result["ok"])
        self.assertEqual([snapshot["wave"] for snapshot in snapshots], [0, 1])

    async def test_secure_connection_uses_http_trust_store_and_preserves_validation(self):
        result, _, _ = await self.run_socket([{"BattleEndData": {"Result": "Win"}}], check_tls=True)
        self.assertTrue(result["ok"])

    async def test_configured_certificate_file_is_used_for_secure_connection(self):
        import certifi
        text = Path(certifi.where()).read_text(encoding="ascii")
        first_certificate = text[text.index("-----BEGIN CERTIFICATE-----"):text.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom-ca.pem"
            path.write_text(first_certificate, encoding="ascii")
            result, _, _ = await self.run_socket([{"BattleEndData": {"Result": "Win"}}], ca_file=str(path), check_tls=True)
        self.assertTrue(result["ok"])

    async def test_plain_connection_does_not_receive_tls_arguments(self):
        result, _, _ = await self.run_socket([{"BattleEndData": {"Result": "Win"}}], domain="ws://offline.invalid/", check_tls=True)
        self.assertTrue(result["ok"])

    async def test_zero_client_index_never_reauthenticates_and_settlement_is_applied(self):
        with self.assertLogs("lucima", level="INFO") as trace:
            result, sent, rewards = await self.run_socket([{"NetID": 1}, {"CType": 0, "Index": 0, "OID": "o"}, {"NetID": 1},
                                                         {"BattleEndData": {"Result": "Win"}, "SceneRewardData": {"GotStars": 1}}])
        self.assertTrue(result["ok"])
        self.assertEqual(sum("RoomDBInfo" in msg for msg in sent), 1)
        self.assertEqual(rewards, [{"GotStars": 1}])
        self.assertTrue(any("[abyss] start scene=S data_version=" in line for line in trace.output))
        self.assertTrue(any("[abyss] end scene=S result=Win" in line for line in trace.output))
        self.assertFalse(any("account" in line or "ServerID" in line for line in trace.output))

    async def test_heartbeat_does_not_extend_progress_deadline(self):
        ticks = iter(range(0, 1000, 10))
        result, _, _ = await self.run_socket([{"NetID": 1}, {"CType": 0, "Index": 0}] + [{"HB": 0}] * 100, clock=lambda: next(ticks))
        self.assertIsInstance(result, GameError)
        self.assertIn("未推进", str(result))

    async def test_game_over_command_without_settlement_is_an_error(self):
        result, sent, _ = await self.run_socket([{"NetID": 1}, {"CType": 0, "Index": 0}, {"NetBattleGameOverCmd": {}}])
        self.assertIsInstance(result, GameError)
        self.assertIn("结算", str(result))
        self.assertIn({"NetBattleGameOverCmd": {}}, sent)

    async def test_cancellation_before_start_does_not_create_a_room(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(ai, "_start_info") as start:
            with self.assertRaisesRegex(GameError, "中止"):
                await ai._run(None, "S", {}, cancel_event=cancel)
        start.assert_not_called()

    async def test_cancellation_during_receive_closes_socket(self):
        result, _, _ = await self.run_socket([{"HB": 0}], cancel=threading.Event(), cancel_on_recv=True)
        self.assertIsInstance(result, GameError)
        self.assertIn("中止", str(result))


class TransportErrorContracts(unittest.TestCase):
    def test_certificate_failure_is_reported_as_game_error_without_retrying(self):
        error = ssl.SSLCertVerificationError(1, "certificate verify failed")
        error.verify_message = "unable to get local issuer certificate"
        error.verify_code = 20
        with patch.object(ai, "_run", side_effect=error) as run:
            with self.assertRaisesRegex(GameError, "证书验证失败"):
                ai.run_abyss_battle(None, {"sceneId": "MysteriousRealm_1", "waves": {}})
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
