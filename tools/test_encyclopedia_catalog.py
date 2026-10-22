"""Contract tests for the Ark2 encyclopedia index consumers."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from build_encyclopedia import build_bundle
from encyclopedia_catalog import load_character_records
from stage_encyclopedia_resources import stage_resources


class EncyclopediaCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "out" / "assets" / "团员"
        self.data = self.source / "资料"
        self.data.mkdir(parents=True)
        self.characters = [
            {"id": "H001", "number": 1, "name": "甲", "element": "Fire", "job": "Defender", "hasStaticPortrait": True, "hasSpine": True},
            {"id": "H002", "number": 2, "name": "乙", "element": "Ice", "job": "Medic", "hasStaticPortrait": False, "hasSpine": False},
        ]
        self.write_index()
        for entry in self.characters:
            record = {
                "schema_version": "2.0-processed", "ID": entry["id"], "NAME": entry["name"],
                "Element": entry["element"], "Job": entry["job"], "Group": "_014",
                "Skill": [{"ID": f"{entry['id']}S1", "IconSprite": "Icon_Skill_Test"}],
                "HiddenSkillIDs": [],
            }
            (self.data / f"{entry['id']}.json").write_text(json.dumps(record), encoding="utf-8")
        (self.data / "translation_map.json").write_text(json.dumps({
            "schema_version": "1.6", "job_icons": {"Defender": "defender.png", "Medic": "medic.png"},
            "faction_icons": {"_014": "faction.png"},
        }), encoding="utf-8")
        portrait = self.source / "立绘" / "H001.png"
        portrait.parent.mkdir(parents=True)
        portrait.write_bytes(b"portrait")
        spine = self.source / "动态立绘" / "H001"
        spine.mkdir(parents=True)
        (spine / "H001.skel").write_bytes(b"skeleton")
        (spine / "H001.atlas").write_text("CG_H001_a.png\nsize: 1, 1\n", encoding="utf-8")
        (spine / "CG_H001_a.png").write_bytes(b"texture")
        icons = self.source / "技能图标"
        icons.mkdir()
        (icons / "Icon_Skill_Test.png").write_bytes(b"icon")
        runtime = self.root / "out" / "assets" / "spine" / "spine-webgl.js"
        runtime.parent.mkdir()
        runtime.write_text("runtime", encoding="utf-8")

    def write_index(self) -> None:
        (self.data / "character_index.json").write_text(json.dumps({
            "schema_version": "1.0", "characters": self.characters,
        }), encoding="utf-8")

    def test_build_and_stage_use_index_and_canonical_media(self) -> None:
        output = self.root / "bundle" / "encyclopedia"
        build_bundle(self.root, output)
        index = json.loads((output / "index.json").read_text(encoding="utf-8"))
        self.assertEqual([entry["id"] for entry in index["characters"]], ["H001", "H002"])
        self.assertEqual([entry["hasSpine"] for entry in index["characters"]], [True, False])
        self.assertEqual([entry["statuses"] for entry in index["characters"]], [[], []])
        detail = json.loads((output / "characters" / "H001.json").read_text(encoding="utf-8"))
        self.assertEqual(detail["assets"]["spine"], {"Base": "H001", "Skin": "LV1", "Animation": "00_Idle"})
        self.assertEqual(detail["assets"]["jobIcon"], "defender.png")

        staged = self.root / "staged"
        counts = stage_resources(self.root, staged)
        self.assertEqual((counts["characters"], counts["portraits"], counts["spine"]), (2, 1, 1))
        self.assertTrue((staged / "portraits" / "spine" / "H001" / "CG_H001_a.png").is_file())

    def test_state_index_uses_associations_and_shared_official_icon_groups(self) -> None:
        translation_path = self.data / "translation_map.json"
        translation = json.loads(translation_path.read_text(encoding="utf-8"))
        translation["status_icons"] = {
            "Immune": "assets/界面图标/状态/Buff_Immune.png",
            "ReduceDef": "assets/界面图标/状态/Debuff_DecreaseDefense.png",
            "AddAttack": "assets/界面图标/状态/Buff_IncreaseAttack.png",
            "H168S2_1": "assets/界面图标/状态/H168S2_1_星链标记.png",
            "H168S2_3": "assets/界面图标/状态/Buff_RemovedDuplicate.png",
        }
        translation_path.write_text(json.dumps(translation), encoding="utf-8")
        for mapped_path in translation["status_icons"].values():
            icon = self.root / "out" / mapped_path
            icon.parent.mkdir(parents=True, exist_ok=True)
            icon.write_bytes(b"icon")
        record_path = self.data / "H001.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["Skill"][0].update({
            "Status": [
                {"ID": "Immune", "Name": "免疫"},
                {"ID": "ReduceDef", "Name": "防御力下降"},
                {"ID": "Immune", "Name": "免疫"},
                {"ID": "Penetrate", "Name": "贯穿"},
                {"ID": "H168S2_1", "Name": "星链标记"},
            ],
            "Description_Markup": "[星链标记:H168S2_3:StatusFun] [贯穿:Penetrate:StatusFun]",
        })
        record["Skill"].append({
            "ID": "H001S4", "IconSprite": "", "Status": {"ID": "AddAttack", "Name": "攻击力提升"},
        })
        record["HiddenSkillIDs"] = ["H001S4"]
        record_path.write_text(json.dumps(record), encoding="utf-8")
        output = self.root / "bundle" / "encyclopedia"
        build_bundle(self.root, output)
        index = json.loads((output / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["characters"][0]["statuses"], ["AddAttack", "H168S2_1", "Immune", "ReduceDef"])
        self.assertEqual(index["characters"][1]["statuses"], [])
        ui = json.loads((output / "ui.json").read_text(encoding="utf-8"))
        self.assertEqual(ui["status_filters"], {
            "buff": [
                {"id": "Immune", "name": "免疫"}, {"id": "AddAttack", "name": "攻击力提升"},
                {"id": "H168S2_1", "name": "星链标记"},
            ],
            "debuff": [{"id": "ReduceDef", "name": "防御力下降"}],
        })
        self.assertEqual(ui["status_icons"]["AddAttack"], "/assets/encyclopedia/icons/status/AddAttack.png")
        self.assertEqual(ui["status_tones"]["ReduceDef"], "debuff")
        detail = json.loads((output / "characters" / "H001.json").read_text(encoding="utf-8"))
        self.assertEqual(detail["Skill"], record["Skill"])

    def test_rejects_incomplete_index_and_false_media_flags(self) -> None:
        self.characters.pop()
        self.write_index()
        with self.assertRaisesRegex(ValueError, "H002"):
            load_character_records(self.root)
        self.characters[0]["hasSpine"] = False
        self.characters.append({"id": "H002", "number": 2, "name": "乙", "element": "Ice", "job": "Medic", "hasStaticPortrait": False, "hasSpine": False})
        self.write_index()
        with self.assertRaisesRegex(ValueError, "hasSpine"):
            load_character_records(self.root)


if __name__ == "__main__":
    unittest.main()
