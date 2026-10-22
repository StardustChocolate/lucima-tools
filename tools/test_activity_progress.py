"""Offline activity first-clear coverage; never sends game requests."""
from copy import deepcopy
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend import activity_progress as ap, abyss_battle, appcore, config, server, tasks
from backend.abyss_state import metadata, initialize_units, sync_activity_support
from backend.game_client import GameClient, GameError, GameMessage


def client_fixture():
    client = GameClient.__new__(GameClient)
    client.account_state = {
        "ActivityDBSettingContainer": {"SettingList": [
            {"ActivityID": aid, "IsIgnoreTime": True} for aid in ("BranchH610", "Version_Elite")]},
        "SceneDataContainer": {"Scenes": []},
        "Teams": {"Settings": [{"ID": "T", "TeamSetting": {"RolePosMap": {"hero": 0}}}]},
        "RoleDataContainer": {"Roles": [{"_id": "hero", "StaticID": "H017", "LV": 60, "Star": 6,
                                         "Skills": {"Skills": [{"StaticID": "H017S1", "Level": 1}]}}]},
        "ItemContainer": {"Items": [{"StaticID": "3", "Count": 9999}]},
    }
    client._auth_data = lambda data: data
    return client


class ActivityProgressTests(unittest.TestCase):
    def setUp(self):
        self.client = client_fixture()
        self.cancel = threading.Event()
        self.sent = []
        self.enterContext(patch.object(config, "get_request_delay_config", return_value={"enabled": False}))
        self.client.call = Mock(side_effect=self.finish)
        self.elite = self.enterContext(patch.object(abyss_battle, "run_activity_battle", side_effect=self.battle))

    def finish(self, route, payload):
        self.assertEqual(route, "SceneHandler.FinishScene")
        info = payload["BattleEndData"]["StartBattleInfo"]
        self.sent.append(deepcopy(payload))
        return {"SceneData": {"StaticID": info["SceneData"]["StaticID"], "Stars": [1, 0, 0], "PassCount": 0},
                "AccountSaveData": {"fixture": len(self.sent)}, "Roles": [{"_id": "hero", "Exp": 123}]}

    def battle(self, client, info, **kwargs):
        self.assertEqual(info["SceneData"]["StaticID"], "BH610_1_13")
        self.assertNotIn("WaveCampDatas", info)
        client.apply_scene_reward({"SceneData": {"StaticID": "BH610_1_13", "Stars": [1, 0, 0]}})
        return {"ok": True, "confirmed": True}

    def run_activity(self):
        return ap.run(self.client, "BH610", "T", cancel_event=self.cancel)

    def clear(self, *stages):
        for stage in stages:
            self.client.apply_scene_reward({"SceneData": {"StaticID": f"BH610_1_{stage}", "Stars": [1, 0, 0]}})

    def test_fresh_activity_includes_all_open_stages_but_not_unopened_hell(self):
        before = deepcopy(self.client.account_state)
        groups = ap.options(self.client.account_state)
        self.assertEqual(len(groups), 1)
        self.assertEqual((groups[0]["total"], groups[0]["completed"]), (13, 0))
        self.assertEqual([s["stage"] for s in groups[0]["scenes"]], list(range(1, 14)))
        self.assertEqual(self.client.account_state, before)

    def test_full_first_clear_merges_each_result_and_accepts_one_star_pass_count_zero(self):
        result = self.run_activity()
        self.assertTrue(result["ok"])
        self.assertEqual(result["completedScenes"], [f"BH610_1_{n}" for n in range(1, 14)])
        self.assertEqual(len(self.sent), 12)
        self.elite.assert_called_once()
        self.assertEqual(self.client.account_state["AccountSaveData"]["fixture"], 12)
        for payload in self.sent:
            end = payload["BattleEndData"]
            start = end["StartBattleInfo"]
            self.assertEqual(start["SceneData"]["Stars"], [0, 0, 0])
            self.assertNotIn("_id", start["SceneData"])
            self.assertEqual((start["IsRestart"], start["IsRepeatAuto"], payload["IsQuickBattle"], end["FinishWave"]), (0, 0, 0, 2))
            expected = metadata()["tables"]["Scene"][start["SceneData"]["StaticID"]]["Waves"][-1]["Ms"]
            self.assertEqual(len(end["Camp2DeadList"]), len(expected))
        self.assertEqual(self.sent[1]["BattleEndData"]["StartBattleInfo"]["CampData1"]["PositionRoleMap"]["1"]["StaticID"], "AcStoryH610")

    def test_each_stage_streams_one_drop_before_the_next_request_without_final_duplicates(self):
        emitted = []

        def finish(*args):
            self.assertEqual(len(emitted), len(self.sent))
            reward = self.finish(*args)
            reward["Drop"] = {"Items": [{"Item": {"StaticID": "1", "Count": 250}}]}
            return reward

        def battle(*args, **kwargs):
            self.assertEqual(len(emitted), 12)
            return {**self.battle(*args, **kwargs), "lines": ["获得 700金币"]}

        self.client.call.side_effect = finish
        self.elite.side_effect = battle
        result = ap.run(self.client, "BH610", "T", cancel_event=self.cancel, on_partial=emitted.append)
        self.assertTrue(result["ok"])
        self.assertEqual(len(emitted), 13)
        for entry, stage in zip(emitted, ap.scenes(self.client.account_state, "BH610")):
            self.assertTrue(entry["ok"])
            self.assertEqual(entry["name"], f"活动自动推图·{stage['name']}")
            self.assertEqual(len(entry["lines"]), 1)
        self.assertEqual(emitted[0]["lines"], ["获得 250金币"])
        self.assertEqual(emitted[-1]["lines"], ["获得 700金币"])
        self.assertEqual(result["lines"], [])

    def test_confirmed_clear_without_drop_still_logs_once(self):
        emitted = []

        def on_partial(entry):
            emitted.append(entry)
            self.cancel.set()

        result = ap.run(self.client, "BH610", "T", cancel_event=self.cancel, on_partial=on_partial)
        self.assertEqual(result["completedScenes"], ["BH610_1_1"])
        self.assertEqual([entry["lines"] for entry in emitted], [["通关成功（结算未返回掉落）"]])
        self.assertEqual(result["lines"], [])

    def test_unconfirmed_or_rejected_result_does_not_log_a_drop(self):
        for outcome in ({"Drop": {"Items": [{"Item": {"StaticID": "1", "Count": 250}}]}},
                        GameMessage("EnergyNotEnough")):
            with self.subTest(outcome=outcome):
                client = client_fixture()
                client.call = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
                emitted = []
                result = ap.run(client, "BH610", "T", cancel_event=self.cancel, on_partial=emitted.append)
                self.assertFalse(result["ok"])
                self.assertEqual(emitted, [])

    def test_scripted_team_can_occur_at_any_stage_and_excludes_support(self):
        tables = deepcopy(metadata()["tables"])
        tables["Scene"]["BH610_1_5"]["MyCampTeam"] = tables["Scene"]["BH610_1_2"].pop("MyCampTeam")
        self.clear(1, 2, 3, 4)
        with patch.object(ap, "metadata", return_value={"tables": tables}):
            info = ap.start_info(self.client.account_state, "BH610_1_5", "missing", {"fixture": True})
        self.assertNotIn("Support", info)
        roles = list(info["CampData1"]["PositionRoleMap"].values())
        self.assertEqual([r["StaticID"] for r in roles], ["AcStoryH610", "AcStoryH016_H610"])
        self.assertIn("H016S4", [s["StaticID"] for s in roles[1]["Skills"]["Skills"]])

    def test_actual_prerequisite_branching_and_version_gate(self):
        self.clear(6)
        self.assertEqual(ap.scene_record(self.client.account_state, "BH610_1_12")["StaticID"], "BH610_1_12")
        with self.assertRaisesRegex(GameError, "前置"):
            ap.scene_record(self.client.account_state, "BH610_1_13")
        self.client.account_state["ActivityDBSettingContainer"]["SettingList"].pop()
        self.assertEqual(len(ap.scenes(self.client.account_state)), 12)
        self.client.account_state["ActivityDBSettingContainer"]["SettingList"] = []
        self.assertEqual(ap.options(self.client.account_state), [])

    def test_skip_cleared_and_resource_shortage_does_not_spend(self):
        self.clear(*range(1, 13))
        self.assertTrue(self.run_activity()["ok"])
        self.assertEqual(self.sent, [])
        fresh = client_fixture()
        fresh.account_state["ItemContainer"]["Items"][0]["Count"] = 0
        fresh.call = Mock()
        result = ap.run(fresh, "BH610", "T", cancel_event=self.cancel)
        self.assertFalse(result["ok"])
        fresh.call.assert_not_called()

    def test_uncertain_settlement_blocks_retry_until_login_replaces_state(self):
        self.client.call.side_effect = lambda *args: {"Drop": {"Items": []}}
        result = self.run_activity()
        self.assertFalse(result["ok"])
        self.assertIn("等待登录同步刷新", result["detail"])
        with self.assertRaisesRegex(GameError, "等待登录"):
            self.run_activity()
        self.assertEqual(self.client.call.call_count, 1)
        self.client._replace_account_state(deepcopy(self.client.account_state))
        self.client.call.side_effect = self.finish
        self.assertTrue(self.run_activity()["ok"])

    def test_disconnect_marks_uncertain_and_stops_after_first_attempt(self):
        self.client.call.side_effect = GameError("网络断开")
        result = self.run_activity()
        self.assertIn("等待登录同步刷新", result["detail"])
        self.assertEqual(self.client.call.call_count, 1)

    def test_explicit_rejection_stops_without_marking_an_unknown_result(self):
        self.client.call.side_effect = GameMessage("EnergyNotEnough")
        result = self.run_activity()
        self.assertFalse(result["ok"])
        self.assertIsNone(self.client._activity_pending)
        self.assertEqual(self.client.call.call_count, 1)

    def test_elite_cancel_before_room_has_no_pending_transaction(self):
        self.clear(*range(1, 13))
        self.cancel.set()
        result = self.run_activity()
        self.assertFalse(result["ok"])
        self.client.call.assert_not_called()
        self.assertIsNone(getattr(self.client, "_activity_pending", None))

    def test_elite_defeat_stops_and_missing_settlement_keeps_pending(self):
        self.clear(*range(1, 13))
        self.elite.side_effect = None
        self.elite.return_value = {"ok": False, "confirmed": True, "detail": "战斗失败"}
        self.assertFalse(self.run_activity()["ok"])
        self.assertIsNone(self.client._activity_pending)
        self.elite.return_value = {"ok": True, "confirmed": False, "detail": "等待登录同步刷新"}
        result = self.run_activity()
        self.assertFalse(result["ok"])
        self.assertIn("等待登录", result["detail"])

    def test_stop_after_confirmed_request_keeps_clear_and_does_not_start_next(self):
        def finish(*args):
            reward = self.finish(*args)
            self.cancel.set()
            return reward
        self.client.call.side_effect = finish
        result = self.run_activity()
        self.assertFalse(result["ok"])
        self.assertEqual(result["completedScenes"], ["BH610_1_1"])
        self.assertIsNone(self.client._activity_pending)

    def test_featured_hero_bonus_applies_to_team_without_mutating_warehouse_or_start(self):
        hero = deepcopy(self.client.account_state["RoleDataContainer"]["Roles"][0])
        hero.update(StaticID="H610", LV=30, Star=3, AwakenLV=0, ImprintLV=3,
                    Skills={"Skills": [{"StaticID": "H610S1", "Level": 1}]})
        regular = deepcopy(self.client.account_state["RoleDataContainer"]["Roles"][0])
        info = {"SceneData": {"StaticID": "BH610_1_13"}, "CampData1": {"PositionRoleMap": {"0": hero, "1": regular}}}
        before = deepcopy(info)
        waves = {"Waves": [{"Monsters": row["Ms"]} for row in metadata()["tables"]["Scene"]["BH610_1_13"]["Waves"]]}
        units = initialize_units(info, waves, single_team=True)
        self.assertEqual(info, before)
        self.assertEqual((units["1-0-0"]["LV"], units["1-0-0"]["Star"], units["1-0-0"]["AwakenLV"]), (60, 6, 6))
        self.assertEqual([s["StaticID"] for s in units["1-0-0"]["NowStatusList"]], ["Activity_Main", "ActivityBuff_B"])
        self.assertEqual([s["StaticID"] for s in units["1-0-1"]["NowStatusList"]], ["ActivityBuff_B"])
        self.assertTrue(all(not u["NowStatusList"] for uid, u in units.items() if uid.startswith("2-")))

    def test_initial_support_fills_vacancy_and_server_replacement_changes_identity(self):
        state = self.client.account_state
        self.clear(*range(1, 13))
        support = deepcopy(state["RoleDataContainer"]["Roles"][0])
        support.update(_id="support", StaticID="H610", LV=30, Star=3,
                       Skills={"Skills": [{"StaticID": "H610S1", "Level": 1}]})
        info = ap.start_info(state, "BH610_1_13", "T", {"PlayerRoleData": {"RoleData": support}})
        waves = {"Waves": [{"Monsters": row["Ms"]} for row in metadata()["tables"]["Scene"]["BH610_1_13"]["Waves"]]}
        units = initialize_units(info, waves, single_team=True)
        self.assertEqual(units["1-0-1"]["StaticID"], "H610")
        self.assertEqual(units["1-0-1"]["LV"], 60)
        self.assertEqual([s["StaticID"] for s in units["1-0-1"]["NowStatusList"]], ["Activity_Main"])
        self.assertEqual(units["1-0-0"]["NowStatusList"], [])
        sync_activity_support({"ActionResult": {"NewSupportRole": {"ID": "1-2-0", "CampPosition": 0,
                              "RoleData": support, "NowHP": 321}}}, units, info, waves)
        self.assertNotIn("1-0-0", units)
        self.assertEqual((units["1-2-0"]["StaticID"], units["1-2-0"]["NowHP"]), ("H610", 321))
        snapshot = abyss_battle._progress_snapshot("BH610_1_13", 2, units, single_team=True)
        self.assertIn("1-2-0", [u["id"] for u in snapshot["allies"]])

    def test_api_progress_cancel_does_not_acquire_busy_account_lock(self):
        acc = SimpleNamespace(email="fixture", client=self.client, lock=threading.Lock(), support_cache={}, push_log=Mock())
        with patch.object(appcore, "_require", return_value=acc), patch.object(appcore, "_status_snapshot", return_value={}):
            def observe(*args, **kwargs):
                self.assertTrue(appcore.activity_progress_snapshot(acc.email)["running"])
                self.assertFalse(appcore.activity_progress_snapshot("other")["running"])
                progress = {"completed": 1, "total": 13, "battle": {
                    "wave": 2, "allies": [{"staticId": "H017", "hpPercent": 75, "dead": False}],
                    "enemies": [{"staticId": "AcEH016_H610", "hpPercent": 50, "dead": False}]}}
                kwargs["on_progress"](progress)
                snapshot = appcore.activity_progress_snapshot(acc.email)["progress"]["battle"]
                self.assertEqual(snapshot["allies"][0]["avatar"], "/assets/avatars/H017.png")
                self.assertEqual(snapshot["enemies"][0]["avatar"], "/assets/avatars/H016.png")
                self.assertEqual(snapshot["enemies"][0]["hpPercent"], 50)
                self.assertNotIn("avatar", progress["battle"]["enemies"][0])
                partial = {"ok": True, "name": "活动自动推图·关卡1", "lines": ["获得 250金币"]}
                kwargs["on_partial"](partial)
                acc.push_log.assert_called_once_with(partial, "")
                server._route_api("POST", "/api/activity/progress/cancel", {"account": acc.email}, {})
                self.assertTrue(kwargs["cancel_event"].is_set())
                return {"ok": False, "detail": "已停止"}
            with patch.object(ap, "run", side_effect=observe):
                result = server._route_api("POST", "/api/activity/progress/run", {"account": acc.email, "family": "BH610", "teamId": "T"}, {})
        self.assertFalse(result["ok"])
        self.assertFalse(appcore.activity_progress_snapshot(acc.email)["running"])

    def test_activity_portraits_use_base_heroes_and_monsters_keep_localized_names(self):
        for sid, base in (("AcBH009_H610", "H009"), ("AcEH016_H610", "H016"),
                          ("AcStoryH610", "H610"), ("AcStoryH016_H610", "H016"),
                          ("AcM0182", "M0182")):
            with self.subTest(sid=sid):
                member = appcore._abyss_member_brief({"staticId": sid})
                self.assertEqual(member["avatar"], appcore.avatar_url(base))
                self.assertEqual(member["name"], tasks.item_name(base))


class SceneDropLogTests(unittest.TestCase):
    def test_sweep_and_first_clear_share_one_mixed_drop_line_with_equipment_cards(self):
        client = client_fixture()
        sid = "BH610_1_1"
        client.apply_scene_reward({"SceneData": {"StaticID": sid, "Stars": [1, 0, 0]}})
        reward = {"SceneData": {"StaticID": sid, "Stars": [1, 0, 0]}, "Drop": {"Items": [
            {"Item": {"StaticID": "1", "Count": 200}},
            {"Equipment": {"StaticID": "E0766", "ClassLV": 4, "Set": "Speed"}},
            {"Item": {"StaticID": "1", "Count": 50}},
            {"Equipment": {"StaticID": "E0766", "ClassLV": 4, "Set": "Speed", "Enhance": 3}},
        ]}}
        before = deepcopy(reward)
        client.call = Mock(return_value=reward)
        sweep = tasks.sweep_once(client, sid, "T")
        first_clear = ap.finish_once(client, ap.start_info(client.account_state, sid, "T"), ap.validate_scene(sid))
        self.assertTrue(sweep["ok"])
        self.assertEqual(sweep["lines"], first_clear["lines"])
        self.assertEqual(len(sweep["lines"]), 1)
        drop = sweep["lines"][0]
        self.assertEqual((drop["type"], drop["text"]), ("drop", "250金币"))
        self.assertEqual([entry["card"]["enhance"] for entry in drop["equips"]], [0, 3])
        self.assertEqual(reward, before)
        client.call.return_value = {"Drop": {"Items": []}}
        self.assertFalse(tasks.sweep_once(client, sid, "T")["ok"])

    def test_elite_settlement_only_exposes_drops_after_confirmed_victory(self):
        for result_name, stars, expected in (("Win", [1, 0, 0], ["获得 700金币"]),
                                             ("Win", [0, 0, 0], []), ("Lose", [0, 0, 0], [])):
            with self.subTest(result=result_name, stars=stars):
                client = client_fixture()
                client.call = Mock(return_value={"_id": "room", "ServerID": 1, "AID": "aid", "Domain": "wss://offline.invalid"})
                info = {"SceneData": {"StaticID": "BH610_1_13"}, "CampData1": {"PositionRoleMap": {
                    "0": deepcopy(client.account_state["RoleDataContainer"]["Roles"][0])}}}
                message = {"BattleEndData": {"Result": result_name}, "SceneRewardData": {
                    "SceneData": {"StaticID": "BH610_1_13", "Stars": stars},
                    "Drop": {"Items": [{"Item": {"StaticID": "1", "Count": 700}}]}}}

                class Socket:
                    async def __aenter__(self): return self
                    async def __aexit__(self, *args): pass
                    async def send(self, raw): pass
                    async def recv(self): return json.dumps(message)

                with patch("websockets.legacy.client.connect", return_value=Socket()):
                    result = abyss_battle.run_activity_battle(client, info)
                self.assertEqual(result.get("lines", []), expected)


class ActivityCaptureReplay(unittest.TestCase):
    def test_elite_three_waves_with_one_persistent_team_and_confirmed_reward(self):
        path = Path(__file__).resolve().parents[1] / "capture/records/capture-20261020-202541.jsonl"
        if not path.exists():
            self.skipTest("Local capture is not distributed with the application")
        from capture.replay_abyss import read_battle
        frames = read_battle(path, "BH610_1_13")
        info = next(row["message"]["StartBattleInfo"] for row in frames if "StartBattleInfo" in row["message"])
        client = client_fixture()
        client.account_state["RoleDataContainer"]["Roles"] = list(deepcopy(info["CampData1"]["PositionRoleMap"]).values())
        client.call = Mock(return_value={"_id": "room", "ServerID": 1, "AID": "aid", "Domain": "wss://offline.invalid"})
        client.aid = "aid"
        received = iter(row["raw"] for row in frames if row["phase"] == "received")
        sent, progress = [], []

        class Socket:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def send(self, raw): sent.append(json.loads(raw))
            async def recv(self): return next(received)

        abyss_battle._AI_RNG.seed(0)
        with patch("websockets.legacy.client.connect", return_value=Socket()):
            result = abyss_battle.run_activity_battle(client, deepcopy(info), on_progress=progress.append)
        self.assertTrue(result["ok"])
        self.assertTrue(result["confirmed"])
        reward = next(row["message"]["SceneRewardData"] for row in frames if "SceneRewardData" in row["message"])
        self.assertEqual(result["lines"], tasks._format_scene_drop(reward)["lines"])
        self.assertEqual({row["wave"] for row in progress}, {0, 1, 2})
        actions = [row for row in sent if "SourceID" in row]
        self.assertEqual(len(actions), 37)
        for row in actions:
            self.assertTrue(row["SourceID"].startswith("1-0-"))
            self.assertTrue(row["TargetID"].startswith(("1-0-", "2-")))
        self.assertTrue(ap.cleared(ap.records(client.account_state).get("BH610_1_13")))
        self.assertEqual(len([row for row in sent if "StartBattleInfo" in row]), 1)
        self.assertTrue(any("NetBattleGameOverCmd" in row for row in sent))


if __name__ == "__main__":
    unittest.main()
