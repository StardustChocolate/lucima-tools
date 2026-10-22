"""Check source consistency and runtime semantics of metadata refreshes."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import sync_game_metadata as metadata
import sync_assets
from build_battle_metadata import TABLE_FIELDS


class GameMetadataTests(unittest.TestCase):
    def setUp(self):
        temporary_root = metadata.ROOT / "build"
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root if temporary_root.is_dir() else None)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.previous = self.root / "previous"
        self.output = self.root / "output"
        self.tables = self.source / "文本/数据表"
        self.tables.mkdir(parents=True)
        self.data = self.source / "团员/资料"
        self.data.mkdir(parents=True)
        skill = dict.fromkeys(metadata.SKILL_FIELDS, "")
        skill.update(ID="H105S1", CoolDown="5", Soul="2", SP="-20", TargetCamp="My", NoSelf="TRUE")
        self.write_table("Skill", tuple(dict.fromkeys((*metadata.SKILL_FIELDS, *TABLE_FIELDS["Skill"]))), [skill])
        fields = tuple(dict.fromkeys(("ID", "IsHero", *(field for field in metadata.ROLE_FIELDS if field != "NormalSkillID"), *TABLE_FIELDS["Role"])))
        hero = dict.fromkeys(fields, "")
        hero.update(ID="H105", IsHero="1", SkillIDs="H105S1~H105S4:Hide", AIs="TestAI", Element="Dark", Job="Medic", HP="1.1234567890123456789")
        npc = {**hero, "ID": "1", "IsHero": "", "SkillIDs": "H105S1", "Element": "Other", "Job": "", "AIs": ""}
        self.write_table("Role", fields, [npc, hero])
        self.write_table("Equipment", TABLE_FIELDS["Equipment"], [{"ID": "E0391", "Part": "Weapon", "EquipLV": "88"}, {"ID": "EN0001", "Part": "Necklace", "EquipLV": "85"}])
        self.write_table("Item", ("ID", "Name", "ItemType"), [{"ID": "E0391", "Name": "T_Item_Name_E0391", "ItemType": "Equipment"}, {"ID": "EN0001", "Name": "T_Missing", "ItemType": "Equipment"}])
        for name in set(TABLE_FIELDS) - {"Role", "Skill", "Equipment"}:
            self.write_table(name, TABLE_FIELDS[name], [])
        self.write_table("PublicVariable", TABLE_FIELDS["PublicVariable"], [{"ID": "GuildWarLessLV", "VALUE": "6"}])
        self.write_table("Activity", TABLE_FIELDS["Activity"], [{"ID": "Version_GVG"}])
        self.write_table("AIAction", TABLE_FIELDS["AIAction"], [{"ID": "TestAI", "Priority": "1", "SkillIndex": "0", "TargetSetting1": '{Type:"LessHP"}'}])
        self.write_json(self.source / "文本/本地化/loc_CHS_FINAL.json", {"T_Item_Name_E0391": "<color=#fff>游戏原名</color>"})
        images = self.source / "装备/图标"
        images.mkdir(parents=True)
        (images / "E0391_游戏原名.png").write_bytes(b"image")
        self.write_json(self.data / "character_index.json", {"schema_version": "1.0", "characters": [{"id": "H105"}]})
        self.character = {"schema_version": "2.0-processed", "ID": "H105", "Element": "Dark", "Job": "Medic", "Skill": [
            {"ID": "H105S1", "CoolDown": 5, "CoolDown_Final": "[5-4]", "Soul": 2, "Soul_Final": "[2-3]", "Targeting": {"SkillType": "", "TargetCamp": "My", "TargetType": "", "NoSelf": True}},
            {"ID": "H105S4", "missing": True, "CoolDown_Final": None, "Soul_Final": None},
        ], "AutoCast": [{"AIID": "TestAI", "SkillIndex": 0, "SkillID": "H105S1", "Priority": 1, "SourceLine": 2, "Conditions": [{"CheckFun": "NoBuff", "CheckID": "Test"}], "Targets": [{"Type": "LessHP"}, {"Type": "RandomOne"}]}]}
        self.write_json(self.data / "H105.json", self.character)
        self.write_json(self.previous / metadata.STATIC_PATH, {"version": "ark2-0513", "rolesByStatic": {"H105": {"Element": "Other", "manual": "keep"}}, "skills": {"H105S1": {"manual": "skill"}}})
        self.write_json(self.previous / metadata.AI_PATH, {"H105": {"manual": "ai"}})
        self.write_json(self.previous / metadata.EQUIPMENT_PATH, {"E0391": {"level": 85, "slot": "weapon", "name": "Old", "img": "assets/img/equip/E0391.png", "manual": {"keep": True}}, "V0571": {"level": 57, "slot": "weapon", "img": "assets/img/equip/E0191.png"}})
        self.write_evidence()

    def write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def write_table(self, name, fields, rows):
        (self.tables / f"sd_{name}.txt").write_text("@".join(fields) + "\n" + "\n".join("@".join(row.get(field, "") for field in fields) for row in rows) + "\n", encoding="utf-8")

    def write_evidence(self):
        details = [{"name": path.stem[3:], "sha256": metadata.sha256(path)} for path in self.tables.glob("sd_*.txt")]
        raw_tables = {entry["name"]: {"sha256": entry["sha256"]} for entry in details if entry["name"] not in {"Equipment", "Item"}}
        self.write_json(self.data.parent / "资料_原始/manifest.json", {"schema_version": "2.0-raw", "characters": [{"id": "H105"}], "tables": raw_tables})
        self.write_json(self.source / "文本/文本提取报告.json", {"catalog": "tools/work/catalog/catalog_test-current.json", "catalog_build_hash": "original-catalog", "static_table_details": details, "chs_merge": {"final_sha256": metadata.sha256(self.source / "文本/本地化/loc_CHS_FINAL.json")}, "verification_errors": []})

    def test_official_level_and_identity_replace_legacy_metadata(self):
        outputs, report = metadata.build_metadata(self.source, previous_root=self.previous)
        static = outputs[metadata.STATIC_PATH]
        self.assertEqual(static["version"], "ark2-test-current")
        self.assertEqual(static["skills"]["H105S1"]["SP"], "-20")
        self.assertEqual(static["roles"]["H105"]["HP"], "1.1234567890123456789")
        self.assertEqual(static["roles"]["H105"]["SkillIDs"], ["H105S1", "H105S4:Hide"])
        self.assertEqual(static["rolesByStatic"]["H105"]["Element"], "Dark")
        self.assertEqual(static["rolesByStatic"]["H105"]["manual"], "keep")
        equipment = outputs[metadata.EQUIPMENT_PATH]
        self.assertEqual(equipment["E0391"]["level"], 88)
        self.assertEqual(equipment["E0391"]["name"], "游戏原名")
        self.assertEqual(equipment["E0391"]["manual"], {"keep": True})
        self.assertEqual(equipment["V0571"], json.loads((self.previous / metadata.EQUIPMENT_PATH).read_text())["V0571"])
        self.assertEqual(equipment["EN0001"]["img"], "")
        self.assertEqual(equipment["EN0001"]["name"], "")
        self.assertIn("EN0001", report["equipment"]["missing_names"])

    def test_activity_scenes_bundle_wave_and_story_team_at_any_stage(self):
        self.write_table("Scene", TABLE_FIELDS["Scene"], [{
            "ID": "BH105_1_5", "Chapter": "BranchH105", "PreScene": "BH105_1_3", "Energy": "8",
            "MyCampTeam": '{Ms:[{M:"H105",Pos:1,LV:60}]}',
            "WaveInfoJsonString": '{Ms:[{M:"1",Pos:3,LV:40}]}',
            "WaveInfo2JsonString": '{Ms:[{M:"1",Pos:4,LV:41}]}',
            "WaveInfo3JsonString": '{Ms:[{M:"1",Pos:5,LV:42}]}',
            "RoleStatusInfo1": '{RoleIDs:["H105"],Conditions:[{ID:"ActivityBuff",Comparer:"Eq",Value1:0}]}',
        }])
        self.write_evidence()
        outputs, _ = metadata.build_metadata(self.source, previous_root=self.previous)
        scene = outputs[metadata.BATTLE_PATH]["tables"]["Scene"]["BH105_1_5"]
        self.assertEqual(scene["PreScene"], "BH105_1_3")
        self.assertEqual([wave["Ms"][0]["LV"] for wave in scene["Waves"]], [40, 41, 42])
        self.assertEqual(scene["MyCampTeam"]["Ms"][0]["M"], "H105")
        self.assertEqual(scene["RoleStatusInfo1"]["Conditions"][0]["Value1"], 0)

    def test_shared_config_refreshes_from_source_and_rejects_missing_parameters(self):
        self.write_table("PublicVariable", TABLE_FIELDS["PublicVariable"], [{"ID": "GuildWarLessLV", "VALUE": "7"}])
        self.write_evidence()
        outputs, report = metadata.build_metadata(self.source, previous_root=self.previous)
        self.assertEqual(outputs[metadata.GAME_CONFIG_PATH]["tables"], {
            "PublicVariable": {"GuildWarLessLV": {"VALUE": "7"}},
            "Activity": {"Version_GVG": {"IsUnlimitTime": ""}},
        })
        metadata.write_metadata(outputs, report, self.output)
        self.assertTrue((self.output / metadata.GAME_CONFIG_PATH).is_file())
        for invalid in ("", "0", "bad"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "GuildWarLessLV"):
                    metadata.build_game_config({"PublicVariable": {"GuildWarLessLV": {"VALUE": invalid}}, "Activity": {}})

    def test_ai_uses_processed_ranges_order_and_missing_skill_nulls(self):
        outputs, report = metadata.build_metadata(self.source, previous_root=self.previous)
        rule = outputs[metadata.AI_PATH]["H105"]
        self.assertEqual(rule["ai"], "TestAI")
        self.assertEqual(rule["autoCast"], self.character["AutoCast"])
        self.assertEqual(rule["skills"][0]["cooldown"], "[5-4]")
        self.assertEqual(rule["skills"][0]["soul"], "[2-3]")
        self.assertIs(rule["skills"][0]["noSelf"], True)
        self.assertIsNone(rule["skills"][1]["targetCamp"])
        self.assertEqual(report["character_evidence"]["missing_source_skills"], ["H105S4"])

    def test_battle_tables_preserve_repeated_rows_and_composite_levels(self):
        self.write_table("AIAction", TABLE_FIELDS["AIAction"], [
            {"ID": "TestAI", "Priority": "1", "SkillIndex": "0", "TargetSetting1": '{Type:"LessHP"}'},
            {"ID": "TestAI", "Priority": "2", "SkillIndex": "1", "TargetSetting1": '{Type:"Self"}'},
            {"ID": "TestAI", "Priority": "2", "SkillIndex": "0", "TargetSetting1": '{Type:"RandomOne"}'}])
        self.write_table("Fantasy", TABLE_FIELDS["Fantasy"], [
            {"ID": "MysteriousRealm_1", "ActivityIDs": "A", **{f"Wave{kind}Json{i}": value for i in (1, 2) for kind, value in (("Monster", '{Ms:[{M:"H105",Pos:5,Boss:true}]}'), ("Buff", '{Bs:[],DBs:[]}'))}},
            {"ID": "MysteriousRealm_1", "ActivityIDs": "B", **{f"Wave{kind}Json{i}": value for i in (1, 2) for kind, value in (("Monster", '{Ms:[]}'), ("Buff", '{Bs:[],DBs:[]}'))}}])
        self.write_table("RoleProperty", TABLE_FIELDS["RoleProperty"], [{"ID": "HERO", "LV": "1", "HP": "314"}, {"ID": "HERO", "LV": "2", "HP": "393"}])
        self.write_evidence()
        outputs, report = metadata.build_metadata(self.source, previous_root=self.previous)
        tables = outputs[metadata.BATTLE_PATH]["tables"]
        self.assertEqual([rule["SkillIndex"] for rule in tables["AIAction"]["TestAI"]], [1, 0, 0])
        self.assertEqual([row["ActivityIDs"] for row in tables["Fantasy"]["MysteriousRealm_1"]], [["A"], ["B"]])
        self.assertEqual(tables["RoleProperty"]["HERO"], {"1": {"HP": "314"}, "2": {"HP": "393"}})
        self.assertEqual(report["battle"]["rows"]["AIAction"], 3)

    def test_additional_battle_table_must_match_extraction_hash(self):
        with (self.tables / "sd_Fantasy.txt").open("a", encoding="utf-8") as stream:
            stream.write("\n")
        # Raw character manifest doesn't normally own Fantasy. Test the
        # extraction report boundary independently from the dossier hashes.
        manifest_path = self.data.parent / "资料_原始/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["tables"].pop("Fantasy")
        self.write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "Battle source doesn't match"):
            metadata.build_metadata(self.source, previous_root=self.previous)

    def test_equipment_artifact_and_global_property_inputs_are_preserved(self):
        self.write_table("EquipmentSet", TABLE_FIELDS["EquipmentSet"], [{"ID": "Attack", "Count": "4", "AttackRate": "0.45"}])
        self.write_table("Artifact", TABLE_FIELDS["Artifact"], [{"ID": "A", "Job": "Other", "Base.HPValue": "32", "Max.HPValue": "416"}])
        self.write_table("PublicVariable", TABLE_FIELDS["PublicVariable"], [
            {"ID": "RoleStarRate", "VALUE": "0.056"}, {"ID": "GuildWarLessLV", "VALUE": "6"}])
        self.write_evidence()
        outputs, _ = metadata.build_metadata(self.source, previous_root=self.previous)
        tables = outputs[metadata.BATTLE_PATH]["tables"]
        self.assertEqual(tables["EquipmentSet"]["Attack"], {"Count": "4", "AttackRate": "0.45"})
        self.assertEqual(tables["Artifact"]["A"], {"Job": "Other", "Base.HPValue": "32", "Max.HPValue": "416"})
        self.assertEqual(tables["PublicVariable"]["RoleStarRate"], {"VALUE": "0.056"})

    def test_stale_source_hash_rejected_before_writes(self):
        path = self.output / metadata.STATIC_PATH
        self.write_json(path, {"keep": "unchanged"})
        before = path.read_bytes()
        with (self.tables / "sd_Skill.txt").open("a", encoding="utf-8") as stream:
            stream.write("\n")
        with self.assertRaisesRegex(ValueError, "Stale character JSON"):
            metadata.sync(self.source, self.output, previous_root=self.previous)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.output / metadata.EQUIPMENT_PATH).exists())

    def test_processed_targeting_and_roster_must_match_source(self):
        self.character["Skill"][0]["Targeting"]["TargetCamp"] = "Other"
        self.write_json(self.data / "H105.json", self.character)
        with self.assertRaisesRegex(ValueError, "targeting differs"):
            metadata.build_metadata(self.source, previous_root=self.previous)
        self.character["Skill"][0]["Targeting"]["TargetCamp"] = "My"
        self.write_json(self.data / "H105.json", self.character)
        self.write_json(self.data / "character_index.json", {"schema_version": "1.0", "characters": []})
        with self.assertRaisesRegex(ValueError, "rosters differ"):
            metadata.build_metadata(self.source, previous_root=self.previous)

    def test_repeated_build_keeps_files_and_retains_exact_backup(self):
        old_bytes = b'{"old":"preserve"}\n'
        target = self.output / metadata.STATIC_PATH
        target.parent.mkdir(parents=True)
        target.write_bytes(old_bytes)
        first = metadata.sync(self.source, self.output, previous_root=self.previous)
        paths = [self.output / path for path in (metadata.STATIC_PATH, metadata.AI_PATH, metadata.EQUIPMENT_PATH)]
        times = [path.stat().st_mtime_ns for path in paths]
        second = metadata.sync(self.source, self.output, previous_root=self.previous)
        self.assertEqual(second["changed_files"], [])
        self.assertEqual(times, [path.stat().st_mtime_ns for path in paths])
        backup = self.output / "build/game-metadata-backups" / hashlib.sha256(old_bytes).hexdigest() / metadata.STATIC_PATH.name
        self.assertEqual(backup.read_bytes(), old_bytes)
        self.assertEqual(first["version"], second["version"])

    def test_preserved_equipment_image_and_ui_mapping(self):
        target = self.root / "runtime"
        (target / "equip").mkdir(parents=True)
        (target / "equip/MISSING.png").write_bytes(b"historical")
        refs = {"E0391": {"img": "assets/equip/E0391.png"}, "Historical": {"img": "assets/equip/MISSING.png"}, "Empty": {"img": ""}}
        files = sync_assets._equipment_files(self.source, refs, target)
        self.assertEqual(set(files), {"E0391.png"})
        with self.assertRaisesRegex(RuntimeError, "missing 1 required equipment"):
            sync_assets._equipment_files(self.source, refs)
        self.assertEqual(sync_assets.UI_SOURCE_FILES["hero-illustrated.png"], Path("菜单模块/Lobby_Illustrated_图鉴.png"))


if __name__ == "__main__":
    unittest.main()
