from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"


def _png_names(category: str) -> set[str]:
    directory = ASSETS / category
    return {path.name for path in directory.iterdir() if path.is_file()}


def _set_icon_names() -> set[str]:
    tree = ast.parse((ROOT / "backend" / "tasks.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "SET_ICON" for target in node.targets
        ):
            return {f"{name}.png" for name in ast.literal_eval(node.value).values()}
    raise AssertionError("SET_ICON mapping not found")


class RuntimeAssetTests(unittest.TestCase):
    def test_equipment_images_exactly_match_reference_table(self):
        with (ROOT / "backend" / "equip_ref.json").open(encoding="utf-8") as stream:
            equipment = json.load(stream)
        expected = {Path(row["img"]).name for row in equipment.values() if row.get("img")}
        self.assertEqual(expected, _png_names("equip"))

    def test_set_images_exactly_match_runtime_mapping(self):
        self.assertEqual(_set_icon_names(), _png_names("sets"))

    def test_avatar_images_are_flat_and_have_unique_hero_ids(self):
        names = _png_names("avatars")
        self.assertTrue(names)
        ids = []
        for name in names:
            match = re.fullmatch(r"(H\d+)\.png", name)
            self.assertIsNotNone(match, name)
            ids.append(match.group(1))
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
