"""Compile all bundled Fantasy periods and raw soul additions, without network/account data."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from backend.abyss_state import metadata
from backend.tasks import _localization, _abyss_skill_text
from tools.game_table_dsl import DSLParser

ROOT = Path(__file__).resolve().parents[1]


def build():
    source = metadata()
    tables = source['tables']
    effects, configurations, souls = {}, [], {}
    local = _localization()
    for scene, rows in sorted(tables['Fantasy'].items()):
        for row in rows:
            configurations.append({'scene': scene, 'activities': row['ActivityIDs'], 'waves': row['Waves']})
            for wave in row['Waves']:
                for kind in ('Bs', 'DBs'):
                    for entry in (wave.get('Buffs') or {}).get(kind) or []:
                        sid = entry['B']
                        definition = tables['Status'].get(sid)
                        if not definition:
                            raise ValueError(f'Missing Fantasy status {sid}')
                        effects[sid] = dict(id=sid, description=_abyss_skill_text(local.get('T_Status_Description_'+sid)),
                            definitionSha256=hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest())
    dossier_hash = hashlib.sha256()
    for path in sorted((ROOT/'assets/encyclopedia/characters').glob('*.json')):
        dossier_hash.update(path.name.encode()+path.read_bytes())
        for skill in json.loads(path.read_text()).get('Skill', []):
            soul = skill.get('SoulBurst') or {}
            if not soul.get('SkillSoulCost'):
                continue
            nodes = DSLParser(soul['SoulSkillFunc']).parse() if soul.get('SoulSkillFunc') else {}
            souls[skill['ID']] = {'cost': soul['SkillSoulCost'], 'functions': nodes}
    return {'schema': 1, 'version': source['version'], 'tableSha256': source['table_sha256'],
            'dossierSha256': dossier_hash.hexdigest(), 'effects': dict(sorted(effects.items())),
            'configurations': configurations, 'soulSkills': dict(sorted(souls.items()))}


if __name__ == '__main__':
    result=build();path=ROOT/'assets/abyss_auto_catalog.json'
    path.write_text(json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n')
    print(json.dumps({'path':str(path),'configurations':len(result['configurations']),'effects':len(result['effects']),'soulSkills':len(result['soulSkills'])}))
