"""Android resource path: no WebView blob copies or unbounded detail cache."""
from pathlib import Path
import unittest
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
SOURCE=(ROOT/'frontend/app.js').read_text()


def function(name):
    start=SOURCE.index('function '+name+'(')
    if SOURCE[start-6:start]=='async ':start-=6
    return SOURCE[start:SOURCE.index('\n}',start)+2]


class PrivateResourceUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright=sync_playwright().start()
        cls.browser=cls.playwright.chromium.launch(headless=True,executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')

    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.playwright.stop()

    def setUp(self):
        self.context=self.browser.new_context()
        self.addCleanup(self.context.close)
        self.context.route('**/resource-cache/*',lambda route:route.fulfill(
            body=(ROOT/'assets/avatars/H001.png').read_bytes(),content_type='image/png'))
        self.page=self.context.new_page()
        self.page.set_content('<base href="https://private-resource.invalid/">')
        self.errors=[];self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        code='\n'.join(function(name) for name in ('usesPrivateResourceCache','encyclopediaPrivateResourceUrl',
            'encyclopediaFetch','readEncyclopediaResourceConfig','loadEncyclopediaDetail'))
        self.page.add_script_tag(content="""
          const ENCYCLOPEDIA_STATE={details:new Map()};
          const encyclopediaPendingRequests=new Map(), calls=[];
          let blobCopies=0;
          window.LucimaFiles={getDownloadLocation:()=>''};
          URL.createObjectURL=()=>{blobCopies++;throw Error('Android must read disk files directly');};
          const api=async path=>{
            calls.push(path);
            if(path==='/api/encyclopedia/local-config') return {schema:2,version:'test',privateFileIndex:true,baseUrl:'https://media.invalid/pool/'};
            if(path.startsWith('/api/encyclopedia/cache?')) return {url:'/resource-cache/'+'a'.repeat(64)};
            throw Error('unexpected API '+path);
          };
          const encyclopediaJson=async url=>({ID:url,NAME:'test'});
          const encyclopediaDetailUrl=entry=>entry.id;
        """+code)
        self.page.add_script_tag(path=str(ROOT/'frontend/encyclopedia-media.js'))

    def tearDown(self):self.assertEqual(self.errors,[])

    def test_native_config_leaves_large_file_table_on_disk_and_detail_lru_is_bounded(self):
        config=self.page.evaluate('readEncyclopediaResourceConfig()')
        self.assertTrue(config['privateFileIndex']);self.assertNotIn('files',config)
        self.assertEqual(self.page.evaluate('calls'),['/api/encyclopedia/local-config'])
        self.page.evaluate("async()=>{for(let i=0;i<50;i++)await loadEncyclopediaDetail({id:'hero'+i});}")
        self.assertEqual(self.page.evaluate('ENCYCLOPEDIA_STATE.details.size'),6)
        self.assertEqual(self.page.evaluate('Array.from(ENCYCLOPEDIA_STATE.details.keys())'),['hero'+str(i) for i in range(44,50)])
        self.page.evaluate("loadEncyclopediaDetail({id:'hero44'})")
        self.assertEqual(self.page.evaluate('Array.from(ENCYCLOPEDIA_STATE.details.keys()).at(-1)'),'hero44')

    def test_native_picture_uses_local_file_without_blob_or_duplicate_image(self):
        self.page.evaluate("""() => {
          const card=document.createElement('div');
          card.innerHTML='<div class="portrait-layer"><canvas class="portrait-canvas"></canvas><img class="portrait-fallback"></div>';
          document.body.append(card);
          window.media=LucimaEncyclopediaMedia.mount({renderer:{card},entry:{id:'H001'},data:{NAME:'测试'},
            config:{schema:2,privateFileIndex:true,baseUrl:'https://media.invalid/pool/'},
            fetchResource:()=>{throw Error('Images must bypass binary fetch');},
            resolveResourceUrl:encyclopediaPrivateResourceUrl});
        }""")
        self.assertEqual(self.page.evaluate('media.ready'),[True])
        self.assertIn('/resource-cache/',self.page.locator('img').get_attribute('src'))
        self.assertEqual(self.page.evaluate('blobCopies'),0)
        self.assertEqual(len(self.page.evaluate('calls')),1)
        self.assertIn('portraits%2Fstatic%2FH001.png',self.page.evaluate('calls[0]'))
        self.page.evaluate('media.dispose()')
        self.assertIsNone(self.page.locator('img').get_attribute('src'))


if __name__=='__main__':unittest.main()
