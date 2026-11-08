"""Private on-disk media, bounded downloads and HTTP cache regressions."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import httpx
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from backend import resource_cache as cache, config, server


class DiskResources(unittest.TestCase):
    def setUp(self):
        self.folder=TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.enterContext(patch.object(config,'data_dir',return_value=Path(self.folder.name)))
        self.endpoint={'manifestUrl':'https://media.invalid/pool/manifest.json','baseUrl':'https://media.invalid/pool/'}
        self.enterContext(patch.object(cache,'resource_config',return_value=self.endpoint))
        self.payload=b'complete verified image contents'
        self.digest=hashlib.sha256(self.payload).hexdigest()
        self.object_url='https://media.invalid/pool/objects/'+self.digest+'.png'
        self.manifest={'schema':2,'version':'test1','baseUrl':'./','files':{
            'portraits/static/H001.png':{'sha256':self.digest,'size':len(self.payload)}}}
        self.offline=False;self.calls=[]
        original=httpx.Client
        def serve(request):
            self.calls.append(str(request.url))
            if self.offline:raise httpx.ConnectError('offline')
            if str(request.url)==self.endpoint['manifestUrl']:return httpx.Response(200,json=self.manifest)
            if str(request.url)==self.object_url:return httpx.Response(200,content=self.payload)
            return httpx.Response(404)
        self.enterContext(patch.object(cache.httpx,'Client',side_effect=lambda **kw: original(transport=httpx.MockTransport(serve),**kw)))
        cache._manifest.cache_clear()
        self.addCleanup(cache._manifest.cache_clear)

    def test_logical_resource_is_verified_on_disk_and_survives_process_cache_reset(self):
        result=cache.prepare('https://media.invalid/pool/portraits/static/H001.png')
        path,info=cache.cached_file(result['url'].rsplit('/',1)[-1])
        self.assertEqual(path.read_bytes(),self.payload)
        self.assertEqual(info['sha256'],self.digest)
        self.assertEqual(path.parent,Path(self.folder.name)/'resources/downloads')
        cache._manifest.cache_clear();self.offline=True
        self.assertEqual(cache.prepare(self.object_url),result)
        self.assertEqual(self.calls,[self.endpoint['manifestUrl'],self.object_url])
        compact=cache.local_config()
        self.assertTrue(compact['privateFileIndex'])
        self.assertNotIn('files',compact,'The full file table must remain outside the WebView heap')

    def test_parallel_duplicate_requests_download_only_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:cache.prepare(self.object_url),range(4)))
        self.assertTrue(all(row==results[0] for row in results))
        self.assertEqual(self.calls.count(self.object_url),1)

    def test_bad_hash_missing_declaration_and_path_escape_do_not_publish_files(self):
        for url in ('https://other.invalid/image.png','https://media.invalid/pool/../secret.png',
                    'https://media.invalid/pool/%2e%2e/secret.png','https://media.invalid/pool/unknown.png'):
            with self.assertRaises(ValueError):cache.prepare(url)
        self.payload=b'x'*len(self.payload)
        with self.assertRaisesRegex(ValueError,'哈希'):cache.prepare(self.object_url)
        self.assertFalse(list(cache.root().glob('*.part')))
        self.assertEqual(len(list(cache.root().glob('*.data'))),1,'Only the manifest may exist')

    def test_failed_manifest_refresh_keeps_last_good_generation_and_offline_images(self):
        first=cache.prepare(self.object_url)
        manifest_file=next(path for path in cache.root().glob('*.json') if json.loads(path.read_text()).get('manifest'))
        info=json.loads(manifest_file.read_text());info['checkedAt']=time.time()-10000
        manifest_file.write_text(json.dumps(info))
        self.manifest['files']={'bad/path.png':{'sha256':'invalid','size':1}}
        self.assertEqual(cache.prepare(self.object_url),first)
        self.offline=True
        self.assertEqual(cache.prepare(self.object_url),first)
        self.assertFalse(list(cache.root().glob('*.part')))

    def test_lru_disk_budget_evicts_media_but_retains_manifest(self):
        cache.prepare(self.object_url)
        extra='f'*64
        (cache.root()/(extra+'.data')).write_bytes(b'12345')
        (cache.root()/(extra+'.json')).write_text(json.dumps({'size':5}))
        with patch.object(cache,'DISK_LIMIT',10):cache._prune(extra)
        self.assertFalse((cache.root()/(hashlib.sha256((self.object_url+'\n').encode()).hexdigest()+'.data')).exists())
        self.assertTrue(any(json.loads(path.read_text()).get('manifest') for path in cache.root().glob('*.json')))


class StaticFileCache(unittest.TestCase):
    def test_static_etag_streams_files_but_api_state_is_never_cached(self):
        original=httpx.Client
        with TemporaryDirectory() as folder:
            path=Path(folder)/'sample.json';path.write_text('{"value":1}')
            with patch.dict(server._STATIC_MOUNTS,{'/test-assets/':Path(folder)}):
                http=server.make_server(port=0)
                worker=threading.Thread(target=http.serve_forever,daemon=True);worker.start()
                try:
                    with original() as client:
                        url=f'http://127.0.0.1:{http.server_port}'
                        first=client.get(url+'/test-assets/sample.json')
                        self.assertEqual(first.json(),{'value':1})
                        self.assertEqual(first.headers['Cache-Control'],'public, max-age=0, must-revalidate')
                        second=client.get(url+'/test-assets/sample.json',headers={'If-None-Match':first.headers['ETag']})
                        self.assertEqual(second.status_code,304);self.assertEqual(second.content,b'')
                        path.write_text('{"value":222}')
                        updated=client.get(url+'/test-assets/sample.json',headers={'If-None-Match':first.headers['ETag']})
                        self.assertEqual(updated.status_code,200);self.assertEqual(updated.json(),{'value':222})
                        state=client.get(url+'/api/encyclopedia/resources')
                        self.assertEqual(state.headers['Cache-Control'],'no-store')
                finally:http.shutdown();http.server_close();worker.join()


if __name__=='__main__':unittest.main()
