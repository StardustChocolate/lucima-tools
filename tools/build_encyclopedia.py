"""Build the lightweight character encyclopedia bundle from ark2 data.

The application ships the index and complete minified text dossiers. Large
portraits, Spine files, and skill icons stay on the configured resource server.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any

from encyclopedia_catalog import load_character_records


ELEMENT_LABELS = {
    "Fire": "火",
    "Ice": "水",
    "Earth": "木",
    "Light": "光",
    "Dark": "暗",
}
JOB_LABELS = {
    "Warrior": "战士", "Caster": "术师", "Sniper": "狙击", "Vanguard": "先锋",
    "Defender": "重装", "Medic": "医疗", "Other": "不限/其他",
}
CONSTELLATION_LABELS = {
    "Aries": "白羊座", "Taurus": "金牛座", "Gemini": "双子座", "Cancer": "巨蟹座",
    "Leo": "狮子座", "Virgo": "处女座", "Libra": "天秤座", "Scorpio": "天蝎座",
    "Sagittarius": "射手座", "Capricorn": "摩羯座", "Aquarius": "水瓶座", "Pisces": "双鱼座",
}
GROUP_LABELS = {
    "_014": "沙漠之狼", "_010": "方舟γ偶像培育所", "_013": "星源研究所", "_005": "真理会",
    "_012": "自由黑客", "_003": "方舟β", "_017": "未知", "_002": "方舟α", "_011": "双蛇会",
    "_015": "暗影之刃", "_008": "无国界医疗", "_006": "地联商团", "_001": "主角团",
    "_016": "时空管理局", "_004": "方舟γ", "_009": "方舟β太空研发总部", "_007": "联合佣兵团",
    "_018": "方舟Ω", "_019": "藿普",
}
PROPERTY_LABELS = {
    "HP": "生命值", "HPValue": "生命值", "HPRate": "生命值", "Attack": "攻击力",
    "AttackValue": "攻击力", "AttackRate": "攻击力", "Defence": "防御力",
    "DefenceValue": "防御力", "DefenceRate": "防御力", "Speed": "速度",
    "SpeedValue": "速度", "CriticalRate": "暴击率", "CriticalDamageRate": "暴击伤害",
    "EffectHitRate": "状态命中", "ResistanceRate": "状态抗性", "PinchRate": "追击率",
}


def character_statuses(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the archive's associated states, including hidden/child skills.

    Skill.Status is already resolved by Ark2's generation chain. Description
    text and function cards are not additional Buff/Debuff associations.
    """
    statuses: list[dict[str, Any]] = []
    for skill in source.get("Skill") or []:
        if not isinstance(skill, dict):
            continue
        associated = skill.get("Status")
        if isinstance(associated, dict):
            associated = [associated]
        if isinstance(associated, list):
            statuses.extend(status for status in associated if isinstance(status, dict))
    return statuses


def build_status_filters(
    records: list[tuple[dict[str, Any], dict[str, Any]]], status_icons: dict[str, str],
) -> dict[str, list[dict[str, str]]]:
    """Use official state names and the shared icon mapping's Buff/Debuff type.

    Unreferenced mappings (such as a removed duplicate stage) are not choices.
    No mechanisms without status icons or names inferred from text are added.
    """
    # Match the card's status_tones, including normalized state-specific icon
    # names such as H168S2_1 and Immortal_Resident, without reclassifying them.
    tones = {
        status_id: ("debuff" if Path(str(mapped_path).replace("\\", "/")).name.startswith("Debuff_") else "buff")
        for status_id, mapped_path in status_icons.items()
    }
    names: dict[str, str] = {}
    for entry, source in records:
        for status in character_statuses(source):
            status_id = status.get("ID")
            if status_id not in tones:
                continue
            name = status.get("Name")
            if not isinstance(name, str) or not name:
                raise RuntimeError(f"{entry['id']} 的状态 {status_id} 缺少正式名称")
            if status_id in names and names[status_id] != name:
                raise RuntimeError(f"状态 {status_id} 在角色资料中存在不一致的名称")
            names[status_id] = name
    return {
        tone: sorted(
            [{"id": status_id, "name": name} for status_id, name in names.items() if tones[status_id] == tone],
            key=lambda item: (item["name"], item["id"]),
        )
        for tone in ("buff", "debuff")
    }


def build_character(
    source: dict[str, Any], entry: dict[str, Any],
    job_icons: dict[str, str], faction_icons: dict[str, str],
) -> dict[str, Any]:
    # Keep the formal processed archive's text and mechanics fields intact.
    result = dict(source)
    result["labels"] = {
        "element": ELEMENT_LABELS.get(str(source.get("Element") or ""), str(source.get("Element") or "")),
        "job": JOB_LABELS.get(str(source.get("Job") or ""), str(source.get("Job") or "")),
        "constellation": CONSTELLATION_LABELS.get(str(source.get("Constellation") or ""), str(source.get("Constellation") or "")),
        "group": GROUP_LABELS.get(str(source.get("Group") or ""), str(source.get("Group") or "")),
        "property": PROPERTY_LABELS,
    }
    spine = {"Base": entry["id"], "Skin": "LV1", "Animation": "00_Idle"} if entry["hasSpine"] else {}
    result["assets"] = {
        "jobIcon": job_icons.get(str(source.get("Job") or ""), ""),
        "factionIcon": faction_icons.get(str(source.get("Group") or ""), ""),
        "spine": spine,
    }
    return result


def build_bundle(ark2_root: Path, output_root: Path) -> None:
    resource_root = ark2_root / "out" / "assets" / "团员" / "资料"
    characters = load_character_records(ark2_root)
    translation_path = resource_root / "translation_map.json"
    translation = json.loads(translation_path.read_text(encoding="utf-8"))
    if translation.get("schema_version") != "1.6":
        raise RuntimeError("translation_map.json 必须使用角色公共映射 schema_version 1.6")
    job_icons = translation.get("job_icons") or {}
    faction_icons = translation.get("faction_icons") or {}
    if not isinstance(job_icons, dict) or not isinstance(faction_icons, dict):
        raise RuntimeError("translation_map.json 中的 job_icons/faction_icons 必须是对象")

    records: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry, source in characters:
        character_id = entry["id"]
        source_path = resource_root / f"{character_id}.json"
        if not isinstance(source.get("Skill"), list):
            raise RuntimeError(f"{source_path} 的技能资料无效")
        for skill in source["Skill"]:
            sprite = skill.get("IconSprite") if isinstance(skill, dict) else None
            if not isinstance(sprite, str) or (sprite and not re.fullmatch(r"Icon_Skill_[A-Za-z0-9_]+", sprite)):
                raise RuntimeError(f"{source_path} 的技能 IconSprite 必须是无扩展名的文件名或空串")
        job = str(source.get("Job") or "")
        group = str(source.get("Group") or "")
        if job and not job_icons.get(job):
            raise RuntimeError(f"translation_map.json 缺少职业图标公共映射：{job}")
        if group and not faction_icons.get(group):
            raise RuntimeError(f"translation_map.json 缺少阵营图标公共映射：{group}")
        hidden = source.get("HiddenSkillIDs")
        if not isinstance(hidden, list) or any(not isinstance(skill_id, str) for skill_id in hidden):
            raise RuntimeError(f"{source_path} 缺少 HiddenSkillIDs 数组")
        skill_ids = {skill.get("ID") for skill in source["Skill"] if isinstance(skill, dict)}
        if any(skill_id not in skill_ids for skill_id in hidden):
            raise RuntimeError(f"{source_path} 的 HiddenSkillIDs 引用了不存在的技能")
        records.append((entry, source))

    status_filters = build_status_filters(records, translation.get("status_icons") or {})
    selectable_status_ids = {
        status["id"] for group in status_filters.values() for status in group
    }
    output_root.mkdir(parents=True, exist_ok=True)
    details_root = output_root / "characters"
    details_root.mkdir(parents=True, exist_ok=True)
    index: list[dict[str, Any]] = []
    for entry, source in records:
        character_id = entry["id"]
        name_en = str(source.get("NAME_EN") or "")
        element = entry["element"]
        job = entry["job"]
        detail_name = f"{character_id}.json"
        (details_root / detail_name).write_text(
            json.dumps(build_character(source, entry, job_icons, faction_icons), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        index.append({
            "id": character_id,
            "number": entry["number"],
            "name": entry["name"],
            "nameEn": name_en,
            "aliases": source.get("NAME_ALIASES") or [],
            "element": element,
            "elementName": ELEMENT_LABELS.get(element, element),
            "job": job,
            "statuses": sorted({
                status["ID"] for status in character_statuses(source)
                if status.get("ID") in selectable_status_ids
            }),
            "star": source.get("InitStar"),
            "avatar": f"/assets/avatars/{character_id}.png",
            "portraitKey": character_id,
            "hasStaticPortrait": entry["hasStaticPortrait"],
            "hasSpine": entry["hasSpine"],
            "detail": f"characters/{detail_name}",
        })

    index.sort(key=lambda item: (item["number"], item["id"]))
    (output_root / "index.json").write_text(
        json.dumps({"version": "ark2-encyclopedia-2", "characters": index}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    # Preserve the sample renderer's public mapping contract while resolving
    # its shared images to this application's local asset bundle.
    small_icons: dict[str, str] = {}

    def local_icon_url(path: Path) -> str:
        return "/assets/" + path.relative_to(output_root.parent).as_posix()

    def copy_small_icon(source_path: Path, destination: Path) -> bool:
        if not source_path.is_file():
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
        source_relative = source_path.relative_to(ark2_root / "out" / "assets").as_posix()
        small_icons[f"../assets/{source_relative}"] = local_icon_url(destination)
        return True

    # Job icons are tiny and useful for the filter even while the cloud bundle
    # is unavailable. Keep the original labels in the file names for traceability.
    jobs_source = ark2_root / "out" / "assets" / "界面图标" / "职业"
    jobs_output = output_root.parent / "jobs"
    jobs_output.mkdir(parents=True, exist_ok=True)
    job_files = {
        "All": "All_全部.png",
        "Caster": "Caster_术师.png",
        "Defender": "Defender_重装.png",
        "Medic": "Medic_医疗.png",
        "Other": "Other_其他.png",
        "Sniper": "Sniper_狙击.png",
        "Vanguard": "Vanguard_先锋.png",
        "Warrior": "Warrior_战士.png",
    }
    for target, filename in job_files.items():
        source_path = jobs_source / filename
        copy_small_icon(source_path, jobs_output / f"{target}.png")

    # The card itself only needs a small local icon set.  Keep these assets in
    # the encyclopedia bundle so the card can render its complete Ark2 data
    # layout while portraits, Spine files, and skill sprites remain optional.
    icon_root = output_root / "icons"
    for relative in ["facts", "potential/ranks", "potential/types", "factions", "status", "misc"]:
        (icon_root / relative).mkdir(parents=True, exist_ok=True)
    ui_icons = ark2_root / "out" / "assets" / "界面图标"
    icon_sources = {
        "facts/height.png": ui_icons / "角色资料" / "UI078_Icon_Height_身高.png",
        "facts/weight.png": ui_icons / "角色资料" / "UI078_Icon_Weight_体重.png",
        "facts/measurements.png": ui_icons / "角色资料" / "UI078_Icon_Measurements_胸围.png",
        "facts/birthday.png": ui_icons / "角色资料" / "UI078_Icon_Birthday_生日.png",
        "potential/types/self.png": ui_icons / "潜能类型" / "UI03_Icon_Mission_自身潜能.png",
        "potential/types/team.png": ui_icons / "潜能类型" / "UI03_Icon_Mission_2_队伍潜能.png",
        "misc/soul.png": ui_icons / "其它" / "Skill" / "Icon_Skill_Soul.png",
        "misc/buff-fallback.png": ui_icons / "其它" / "UI_QuestionMark" / "Buff1.png",
        "misc/debuff-fallback.png": ui_icons / "其它" / "UI_QuestionMark" / "Debuff1.png",
    }
    for relative, source_path in icon_sources.items():
        copy_small_icon(source_path, icon_root / relative)
    for rank in ("D", "C", "B", "A", "S", "SS", "SSS"):
        source_path = ui_icons / "潜能等级" / f"UI03_Icon_Mission_txt_{rank}.png"
        copy_small_icon(source_path, icon_root / "potential" / "ranks" / f"{rank}.png")

    # Reuse the shared icons used elsewhere in the app, with the exact same
    # game images as the sample. No character media is copied here.
    stat_files = {
        "Health": "HP_生命力.png",
        "Attack": "Attack_攻击力.png",
        "Defense": "Defence_防御力.png",
        "Speed": "Speed_速度.png",
        "Critical": "Critical_暴击率.png",
        "CriticalDamage": "CriticalDamage_暴击伤害.png",
        "EffectHit": "EffectHit_状态命中.png",
        "Resistance": "Resistance_状态抗性.png",
        "Pinch": "Pinch_追击率.png",
    }
    for target, filename in stat_files.items():
        copy_small_icon(ui_icons / "能力" / filename, output_root.parent / "stats" / f"{target}.png")
    element_files = {
        "fire": "Fire_火属性.png", "water": "Ice_水属性.png", "wood": "Earth_木属性.png",
        "light": "Light_光属性.png", "dark": "Dark_暗属性.png",
    }
    for target, filename in element_files.items():
        copy_small_icon(ui_icons / "属性" / filename, output_root.parent / "attributes" / f"{target}.png")

    faction_source = ark2_root / "out" / "assets" / "团员" / "阵营"
    for source_path in faction_source.glob("阵营*.png"):
        match = re.match(r"阵营(\d{2})_", source_path.name)
        if match:
            copy_small_icon(source_path, icon_root / "factions" / f"Group_0{match.group(1)}.png")

    status_map: dict[str, str] = {}
    for status_id, mapped_path in (translation.get("status_icons") or {}).items():
        source_path = ark2_root / "out" / "assets" / Path(str(mapped_path).replace("\\", "/")).relative_to("assets")
        destination = icon_root / "status" / f"{status_id}.png"
        if copy_small_icon(source_path, destination):
            status_map[status_id] = local_icon_url(destination)
    ui_bundle = {
        key: translation.get(key, {})
        for key in ("scaling_CHS", "field_labels_CHS", "skill_function_cards", "auto_cast_display_CHS", "values_CHS")
    }
    ui_bundle["status_icons"] = status_map
    ui_bundle["status_filters"] = status_filters
    ui_bundle["job_icons"] = {
        job: small_icons[f"../{mapped_path}"]
        for job, mapped_path in job_icons.items()
        if f"../{mapped_path}" in small_icons
    }
    ui_bundle["faction_icons"] = {
        group: small_icons[f"../{mapped_path}"]
        for group, mapped_path in faction_icons.items()
        if f"../{mapped_path}" in small_icons
    }
    ui_bundle["small_icons"] = small_icons
    ui_bundle["status_tones"] = {
        status_id: ("debuff" if Path(str(mapped_path)).name.startswith("Debuff_") else "buff")
        for status_id, mapped_path in (translation.get("status_icons") or {}).items()
    }
    (output_root / "ui.json").write_text(
        json.dumps(ui_bundle, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ark2-root", type=Path, default=Path(r"E:\Project\ark2"))
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "assets" / "encyclopedia")
    args = parser.parse_args()
    build_bundle(args.ark2_root, args.output)


if __name__ == "__main__":
    main()
