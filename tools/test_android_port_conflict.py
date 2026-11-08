"""Real TCP collision and HTTP readiness regressions for the Android entry."""
from concurrent.futures import ThreadPoolExecutor
import errno
import http.client
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from backend import android_entry as entry, background, config, resource_sync, server


class AndroidPortConflict(unittest.TestCase):
    def setUp(self):
        self.folder=TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name)
        self.env=self.enterContext(patch.dict(os.environ,{}))
        self.health=self.enterContext(patch.object(background,'start'))
        self.resources=self.enterContext(patch.object(resource_sync,'start'))
        self.enterContext(patch.object(server.redeem_codes,'start_scheduler'))
        self.assertIsNone(entry._server)
        self.instance='own-test-instance'
        self.args=(str(self.root/'data'),str(self.root/'web'),'')
        self.addCleanup(self.stop_server)

    def stop_server(self):
        http=entry._server;worker=entry._server_thread
        if http is not None:
            http.shutdown()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        self.assertIsNone(entry._server)
        self.assertEqual(entry.running_port(),0)

    def start(self,port=0,instance=None):
        return entry.start(*self.args,port,'sample.apk',instance or self.instance)

    def request(self,port,path='/api/health'):
        client=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
        try:
            client.request('GET',path)
            response=client.getresponse();body=response.read()
            return response.status,dict(response.getheaders()),body
        finally:client.close()

    def test_exact_address_in_use_error_chooses_live_own_server(self):
        with socket.socket() as occupant:
            occupant.bind(('127.0.0.1',0));occupant.listen()
            occupied=occupant.getsockname()[1]
            # This is the same bind error shown by the user's screenshot.
            with self.assertRaises(OSError) as collision:
                with socket.socket() as duplicate:duplicate.bind(('127.0.0.1',occupied))
            self.assertEqual(collision.exception.errno,errno.EADDRINUSE)
            port=self.start(occupied)
            self.assertNotEqual(port,occupied)
            self.assertEqual(entry.running_port(),port)
            self.assertEqual(entry._server.server_address,('127.0.0.1',port))
            status,headers,body=self.request(port)
            self.assertEqual(status,200)
            self.assertEqual(headers['X-Lucima-Instance'],self.instance)
            self.assertEqual(headers['Cache-Control'],'no-store')
            self.assertEqual(json.loads(body)['service'],'LucimaTools')
            self.assertEqual(occupant.getsockname()[1],occupied)
            self.health.assert_called_once();self.resources.assert_called_once()

    def test_free_preferred_port_is_kept(self):
        # Keep the selected socket bound until make_server receives it, so the
        # preferred-port assertion cannot be weakened by an unrelated race.
        with server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler) as prebound:
            preferred=prebound.server_port
            with patch.object(server,'make_server',return_value=prebound):port=self.start(preferred)
            self.assertEqual(port,preferred)
            self.assertEqual(self.request(port)[0],200)
            self.stop_server()

    def test_concurrent_activity_service_and_retry_share_one_listener(self):
        original=server.make_server
        with patch.object(server,'make_server',wraps=original) as bind:
            with ThreadPoolExecutor(max_workers=8) as pool:
                ports=list(pool.map(lambda _:self.start(),range(16)))
            self.assertEqual(len(set(ports)),1)
            self.assertEqual(bind.call_count,1)
        self.health.assert_called_once();self.resources.assert_called_once()
        self.assertEqual(self.request(ports[0])[1]['X-Lucima-Instance'],self.instance)

    def test_other_instance_is_not_reused_or_allowed_to_rewrite_environment(self):
        port=self.start();environment=dict(os.environ)
        with self.assertRaisesRegex(RuntimeError,'其他启动实例'):
            entry.start('/different/data','/different/web','',port,'other.apk','other-instance')
        self.assertEqual(dict(os.environ),environment)
        self.assertEqual(self.request(port)[1]['X-Lucima-Instance'],self.instance)

    def test_non_collision_bind_failure_is_reported_without_fallback(self):
        with patch.object(server,'make_server',side_effect=OSError(errno.EACCES,'permission denied')) as bind:
            with self.assertRaises(OSError) as denied:self.start()
        self.assertEqual(denied.exception.errno,errno.EACCES)
        self.assertEqual(bind.call_count,1)
        self.assertIsNone(entry._server)
        self.health.assert_not_called();self.resources.assert_not_called()
        self.assertEqual(self.request(self.start())[0],200)

    def test_failure_after_bind_closes_port_and_allows_retry(self):
        candidate=[];original=server.make_server
        def bind(*args,**kwargs):
            http=original(*args,**kwargs);candidate.append(http);return http
        with patch.object(server,'make_server',side_effect=bind),patch.object(resource_sync,'start',side_effect=RuntimeError('startup step failed')):
            with self.assertRaisesRegex(RuntimeError,'startup step failed'):self.start()
        self.assertIsNone(entry._server)
        self.assertEqual(candidate[0].socket.fileno(),-1)
        self.assertEqual(self.request(self.start())[0],200)

    def test_stopped_server_can_be_started_again(self):
        port=self.start();self.stop_server()
        restarted=self.start(port)
        self.assertEqual(self.request(restarted)[0],200)
        self.assertEqual(self.request(restarted)[1]['X-Lucima-Instance'],self.instance)

    def test_selected_port_serves_production_home_and_configuration(self):
        port=self.start()
        status,headers,body=self.request(port,'/')
        self.assertEqual(status,200);self.assertIn(b'<!DOCTYPE html>',body)
        status,headers,body=self.request(port,'/api/config')
        self.assertEqual(status,200);self.assertIn('platform',json.loads(body))
        self.assertEqual(headers['X-Lucima-Instance'],self.instance)


if __name__=='__main__':unittest.main()
