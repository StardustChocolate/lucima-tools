"""只供离线 UI 验收的虚拟账号，不创建游戏会话。"""
import json
import sys
from backend.gvg import adapters,search
from backend.gvg.catalog import catalog
from tools.test_gvg import account_fixture


def main():
    acc,_=account_fixture(10)
    data=adapters.roster(acc.client.account_state)
    body=json.load(sys.stdin) if not sys.stdin.isatty() else {}
    if body.get('action')=='recommend':
        print(json.dumps(search.recommend(data,adapters.normalize(body)),ensure_ascii=False))
    else:print(json.dumps({'catalog':catalog(),'overview':data},ensure_ascii=False))


if __name__=='__main__':main()
