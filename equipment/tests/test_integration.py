"""新工程的真实 HTTP 分派及静态资源离线验证，不连接游戏。"""
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from backend import appcore, server
from equipment.tests.test_auto import fixture


class EquipmentIntegrationTests(unittest.TestCase):
    def test_entrypoint_reuses_the_selected_account(self):
        state,_=fixture(2)
        acc=SimpleNamespace(client=SimpleNamespace(account_state=state),lock=threading.Lock())
        with patch.object(appcore,'_require',return_value=acc) as require:
            result=server._route_api('POST','/api/equipment/action',{'account':'offline','action':'overview'}, {})
        require.assert_called_once_with('offline')
        self.assertEqual(len(result['roles']),6)
        self.assertEqual(len(result['equips']),12)

    def test_http_serves_the_page_and_all_equipment_resources(self):
        # 不调用 make_server，以免启动更新或兑换码定时任务。
        httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        worker=threading.Thread(target=httpd.serve_forever,daemon=True)
        worker.start()
        try:
            conn=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=5)
            for path in ('/','/equipment/equipment.js','/equipment/equipment.css',
                         '/equipment/role-search.js','/equipment/vendor/pinyin-pro-3.29.5.js'):
                conn.request('GET',path)
                response=conn.getresponse();data=response.read()
                self.assertEqual(response.status,200,path)
                self.assertTrue(data,path)
                if path=='/':
                    html=data.decode()
                    self.assertIn('id="section-equipment"',html)
                    self.assertLess(html.index('data-section="guild"'),html.index('data-section="equipment"'))
                    self.assertLess(html.index('data-section="equipment"'),html.index('data-section="toolbox"'))
            conn.close()
        finally:
            httpd.shutdown();httpd.server_close();worker.join(timeout=5)

if __name__=='__main__':unittest.main()
