"""Real-table ability and native decision regressions, without network access."""
from copy import deepcopy
import random
import unittest

from backend import abyss_battle as ai
from backend.abyss_properties import combine_functions, refresh_abilities, role_properties, final_literal_values
from backend.abyss_rules import Context, EvaluationError, check, property_value, select, value_convert
from backend.abyss_state import float32, initialize_units, metadata, sync_units


TABLES = metadata()["tables"]


def hero(sid, uid="1-0-0", *, artifact=None):
    definition = TABLES["Role"][sid]
    unit = {"id": uid, "_id": uid, "StaticID": sid, "LV": 60, "Star": 6, "AwakenLV": 0,
            "Position": int(uid.rsplit("-", 1)[-1]), "Element": definition.get("Element"), "Job": definition.get("Job"),
            "Skills": {"Skills": [{"StaticID": s.split(":")[0], "Level": 6} for s in definition["SkillIDs"].split("~")]},
            "NowStatusList": [], "NowSkillCooldownMap": {}, "NowHP": 1}
    if artifact:
        unit["ArtifactData"] = {"StaticID": artifact, "LV": 30}
    return unit


class AbilityContracts(unittest.TestCase):
    def test_wave_defender_bonus_fills_real_hp_before_first_sync(self):
        unit = hero("H001")
        self.assertEqual(unit["Job"], "Defender")
        base = final_literal_values(role_properties(unit, TABLES).values)["HP"]
        unit["NowStatusList"] = [{"StaticID": "FG_Job_013", "NowStack": 1}]
        self.assertFalse(refresh_abilities([unit], TABLES, initialize=True, round_role=""))
        self.assertEqual(unit["MaxHP"], round(float32(base * float32(1.3))))
        self.assertEqual(unit["NowHP"], unit["MaxHP"])

    def test_legacy_double_separator_retains_monster_property_function(self):
        from tools.game_table_dsl import DSLParser
        row = TABLES["Role"]["FH108"]
        node = row["Dynamic"]["DynamicField1"]
        self.assertEqual(node["separators"], ["~~"])
        self.assertEqual(node["items"][1]["value"]["Property"], "CriticalRate")
        parsed = DSLParser('AntiList#"A~~B"~~Fun#{Trigger:"Property",Property:"SpeedRate",Value:0.1}').parse()
        self.assertEqual(parsed["items"][0]["value"], "A~~B")
        monster = hero("FH108")
        refresh_abilities([monster], TABLES, initialize=True, round_role="")
        self.assertAlmostEqual(monster["_AbilityProperties"]["CriticalRate"], 0.85, places=6)

    def test_passive_level_enhancements_switch_with_authoritative_hp(self):
        unit = hero("H146")
        self.assertFalse(refresh_abilities([unit], TABLES, initialize=True, round_role=""))
        self.assertAlmostEqual(unit["_AbilityProperties"]["AttackFinalRate"], 0.4, places=6)
        self.assertAlmostEqual(unit["_AbilityProperties"]["DeCriticalRate"], 0.4, places=6)
        original_hp = unit["MaxHP"] // 4
        unit["NowHP"] = original_hp
        refresh_abilities([unit], TABLES, round_role=unit["id"])
        self.assertEqual(unit["NowHP"], original_hp)
        self.assertNotIn("AttackFinalRate", unit["_AbilityProperties"])
        self.assertAlmostEqual(unit["_AbilityProperties"]["DefenceFinalRate"], 0.4, places=6)

    def test_active_skill_passive_property_uses_native_ready_condition(self):
        unit = hero("H021")
        refresh_abilities([unit], TABLES, initialize=True, round_role="")
        self.assertAlmostEqual(unit["_AbilityProperties"]["DodgeRate"], 0.5)
        unit["NowSkillCooldownMap"] = {"H021S3": 2}
        refresh_abilities([unit], TABLES, round_role=unit["id"])
        self.assertNotIn("DodgeRate", unit["_AbilityProperties"])

    def test_artifact_converter_level_modifiers_at_hp_boundary(self):
        unit = hero("H001", artifact="A0003")
        refresh_abilities([unit], TABLES, initialize=True, round_role="")
        unit["NowHP"] = unit["MaxHP"] // 5
        refresh_abilities([unit], TABLES, round_role=unit["id"])
        self.assertAlmostEqual(unit["_AbilityProperties"]["DefenceFinalRate"], 1.0, places=6)
        self.assertAlmostEqual(unit["_AbilityProperties"]["ResistanceRate"], 1.0, places=6)

    def test_aura_uses_provider_main_round_and_same_camp(self):
        provider, ally, enemy = hero("H003", artifact="A0031"), hero("H001", "1-0-1"), hero("H001", "2-0-0")
        refresh_abilities([provider, ally, enemy], TABLES, initialize=True, round_role="")
        refresh_abilities([provider, ally, enemy], TABLES, round_role=provider["id"])
        self.assertAlmostEqual(ally["_AbilityProperties"]["AttackFinalRate"], 0.2)
        self.assertNotIn("AttackFinalRate", enemy["_AbilityProperties"])
        self.assertNotIn("AttackFinalRate", provider["_AbilityProperties"])
        provider["NowHP"] = 0
        refresh_abilities([provider, ally, enemy], TABLES, round_role=provider["id"])
        self.assertNotIn("AttackFinalRate", ally["_AbilityProperties"])

    def test_status_value_converter_uses_synced_status_value(self):
        unit = hero("H193")
        unit["NowStatusList"] = [{"StaticID": "H193S2_1", "NowStack": 1, "Value": 0.37}]
        refresh_abilities([unit], TABLES, initialize=True, round_role="")
        self.assertAlmostEqual(unit["_AbilityProperties"]["ResistanceRate"], 0.37, places=6)

    def test_merge_does_not_change_metadata_and_addvalue_preserves_condition(self):
        condition = {"Property": "NowHPRate", "Value": 0.5, "Comparer": "Gt"}
        functions = [{"ID": "a", "Value": 0.2, "Condition": condition},
                     {"ID": "a", "Value": 0.3, "LV": 1}, {"ID": "a", "Fun": "AddValue", "Value": 0.1},
                     {"ID": "b", "GID": "g", "Value": 0.2}, {"ID": "c", "GID": "g", "Value": 0.5}]
        original = deepcopy(functions)
        merged = combine_functions(functions)
        self.assertEqual(functions, original)
        self.assertEqual([f["ID"] for f in merged], ["a", "c"])
        self.assertAlmostEqual(merged[0]["Value"], 0.4, places=6)
        merged = combine_functions([functions[0], functions[2]])
        self.assertEqual(merged[0]["Condition"], condition)

    def test_single_artifact_only_highest_level_provider_runs(self):
        tables = deepcopy(TABLES)
        tables["Artifact"]["A0031"]["IsFunSingle"] = "TRUE"
        a, b, ally = hero("H003", artifact="A0031"), hero("H003", "1-0-1", artifact="A0031"), hero("H001", "1-0-2")
        a["ArtifactData"]["LV"] = 3
        refresh_abilities([a, b, ally], tables, initialize=True, round_role="")
        refresh_abilities([a, b, ally], tables, round_role=a["id"])
        self.assertNotIn("AttackFinalRate", ally["_AbilityProperties"])
        refresh_abilities([a, b, ally], tables, round_role=b["id"])
        self.assertAlmostEqual(ally["_AbilityProperties"]["AttackFinalRate"], 0.2)
        self.assertGreater(a["MaxHP"], 0)


class InitializationContracts(unittest.TestCase):
    def test_scene_monster_level_star_max_skill_and_initial_wave_status(self):
        start = {"SceneData": {"StaticID": "MysteriousRealm_1"}, "WaveCampDatas": [{"PositionRoleMap": {}}, {"PositionRoleMap": {}}]}
        config = deepcopy(TABLES["Fantasy"]["MysteriousRealm_1"][0])
        config["Waves"][0]["Monsters"][0]["LV"] = 1
        units = initialize_units(start, config, TABLES)
        monster = units["2-0-0"]
        self.assertEqual(monster["LV"], 50)
        self.assertEqual(monster["Star"], 6)
        self.assertEqual([s["Level"] for s in monster["Skills"]["Skills"]], [6, 6, 6, 1, 1])
        self.assertEqual(monster["NowHP"], monster["MaxHP"])
        self.assertIn("FB_Immune_003", [s["StaticID"] for s in monster["NowStatusList"]])
        self.assertNotIn("FG_Job_006", [s["StaticID"] for s in monster["NowStatusList"]])

    def test_monster_defaults_artifact_and_initial_cooldown(self):
        start = {"WaveCampDatas": [{"PositionRoleMap": {}}, {"PositionRoleMap": {}}]}
        config = {"Waves": [{"Monsters": [{"M": "M0011", "ArtifactID": "A0003", "ArtifactLV": 12}]}, {"Monsters": []}]}
        unit = initialize_units(start, config, TABLES)["2-0-1"]
        self.assertEqual(unit["LV"], 1)
        self.assertEqual(unit["ArtifactData"]["LV"], 12)
        self.assertEqual(unit["NowSkillCooldownMap"]["M001S2"], 3)
        self.assertNotIn("M001S1", unit["NowSkillCooldownMap"])
        config["Waves"][0]["Monsters"][0].pop("ArtifactLV")
        unit = initialize_units(start, config, TABLES)["2-0-1"]
        self.assertEqual(unit["ArtifactData"]["LV"], 0)

    def test_trusted_sync_replaces_initial_buffs_and_preserves_hp_during_reset(self):
        unit = hero("H001")
        unit["NowStatusList"] = [{"StaticID": "FG_Job_013", "NowStack": 1}]
        refresh_abilities([unit], TABLES, initialize=True, round_role="")
        units = {unit["id"]: unit}
        sync_units({"ActionResult": {"SkillEventList": [{"RoleEventList": [{"TargetRoleID": unit["id"], "IsCanSyncRole": 1, "NowHP": 123, "NowInjuryRate": 0.2}]}]}}, units)
        refresh_abilities([unit], TABLES, round_role=unit["id"])
        self.assertEqual(unit["NowHP"], 123)
        self.assertEqual(unit["NowStatusList"], [])
        self.assertEqual(unit["MaxHP"], round(float32(float32(0.8) * unit["NoInjuryMaxHP"])))


class RuleContracts(unittest.TestCase):
    def setUp(self):
        self.source = {"id": "1-0-0", "NowHP": 100, "MaxHP": 100, "Position": 0}
        self.other = {"id": "1-0-3", "NowHP": 20, "MaxHP": 100, "Position": 3}
        self.ctx = Context(self.source, [self.source, self.other], [], tables=TABLES, target=self.source, round_role=self.source["id"], rng=random.Random(0))

    def test_invert_never_inverts_nested_mandatory_gates(self):
        cond = {"Property": "NowHP", "Comparer": "Eq", "Value": 0, "Invert": True,
                "NextCondition": {"Property": "NowHP", "Comparer": "Eq", "Value": 0}}
        self.assertFalse(check(cond, self.ctx))
        cond.pop("NextCondition")
        self.assertTrue(check(cond, self.ctx))

    def test_compared_value_converts_actual_selected_subject(self):
        cond = {"Who": "Target", "Property": "NowHP", "Comparer": "Ltn", "ComparedValue": {"Who": "Source", "Property": "NowHP", "Rate": 0.5}}
        self.assertTrue(check(cond, self.ctx.for_target(self.other)))
        self.assertFalse(check(cond, self.ctx))

    def test_converter_defaults_nested_and_limits_and_lost_hp_percent(self):
        self.assertEqual(value_convert({"Property": "NowHP", "Base": 2}, self.ctx), 2)
        self.assertEqual(value_convert({"Property": "ValueOne", "Rate": 3, "Base": 2, "ValueConvert": {"Base": 7}, "FinalMaxValue": 10}, self.ctx), 10)
        self.assertEqual(property_value("LastHPRate", self.other, self.ctx), 80)
        self.assertAlmostEqual(property_value("LostHPRate", self.other, self.ctx), 0.8)

    def test_fixed_front_back_positions_and_self_exclusion(self):
        self.assertEqual(select({"TargetType": "FrontRow", "TargetCamp": "My"}, self.ctx), [self.source])
        self.assertEqual(select({"TargetType": "BackRow", "TargetCamp": "My"}, self.ctx), [self.other])
        self.assertFalse(select({"TargetType": "Self", "NoSelf": True}, self.ctx))
        self.other["Position"] = 2
        self.assertFalse(select({"TargetType": "BackRow", "TargetCamp": "My"}, self.ctx))

    def test_group_stacks_use_static_status_group_and_condition_selector(self):
        tables = {**TABLES, "Status": {"a": {"Group": "g"}, "b": {"Group": "g"}, "c": {"Group": "other"}}}
        self.ctx.tables = tables
        self.source["NowStatusList"] = [{"StaticID": "a", "NowStack": 2}, {"StaticID": "b", "NowStack": 4}]
        self.other["NowStatusList"] = [{"StaticID": "c", "NowStack": 20}]
        self.assertEqual(select({"TargetType": "MaxBuffGroupStack", "TargetTypeID": "g", "TargetCamp": "My"}, self.ctx), [self.source])
        condition = {"TargetInfo": {"TargetType": "BackRow", "TargetCamp": "My"}, "Property": "NowHPRate", "Comparer": "Ltn", "Value": 0.5}
        self.assertTrue(check(condition, self.ctx))

    def test_ready_and_unavailable_skill_are_opposite_native_gates(self):
        ready = {"CheckFun": "CheckSkillCooldown", "CheckID": "S"}
        blocked = {"CheckFun": "CheckSkillNoUse", "CheckID": "S"}
        self.assertTrue(check(ready, self.ctx))
        self.assertFalse(check(blocked, self.ctx))
        self.source["NowSkillCooldownMap"] = {"S": 1}
        self.assertFalse(check(ready, self.ctx))
        self.assertTrue(check(blocked, self.ctx))

    def test_missing_event_context_and_unknown_rule_are_explicit(self):
        with self.assertRaises(EvaluationError):
            check({"Who": "TriggerSource", "Invert": True}, self.ctx)
        self.assertFalse(ai._condition_ok({"CheckFun": "Unknown", "Invert": True}, self.source, [self.source], []))
        self.assertIn("unknown_condition:Unknown", self.source["_EvaluationIssues"])

    def test_dead_hideout_and_random_candidates_stay_legal(self):
        self.other["NowHP"] = 0
        self.assertEqual(select({"TargetType": "DeadOne", "TargetCamp": "My"}, self.ctx), [self.other])
        self.other["NowIsDieOut"] = True
        self.assertFalse(select({"TargetType": "DeadOne", "TargetCamp": "My"}, self.ctx))
        self.other.update(NowHP=20, NowIsDieOut=False, HideOut=True)
        self.assertEqual(select({"TargetType": "RandomTwo", "TargetCamp": "My"}, self.ctx), [self.source])


if __name__ == "__main__":
    unittest.main()
