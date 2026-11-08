"""Android-led release numbering, retries and four-part update ordering."""
import importlib.util
import json
from pathlib import Path
import plistlib
import sys
from tempfile import TemporaryDirectory
import unittest

from tools.release_version import prepare_version, finish_release
from backend.updater import _newer


class ReleaseVersions(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        (self.project/'backend').mkdir()
        (self.project/'backend/version.py').write_text('APP_VERSION = "1.3.4"\nANDROID_VERSION_CODE = 10310\n')

    def complete(self, version, platform='android'):
        artifact=self.project/(platform+'-'+version+'.artifact');artifact.write_bytes(b'verified-test-artifact')
        finish_release(self.project, version, artifact, platform=platform)

    def test_desktop_and_ios_follow_android_without_consuming_its_next_version(self):
        first=prepare_version(self.project,'android');self.assertEqual(first,'1.3.4.1');self.complete(first)
        android_source=(self.project/'backend/version.py').read_bytes()
        for platform in ('ios','windows','windows','ios'):
            self.assertEqual(prepare_version(self.project,platform),first)
            self.complete(first,platform)
            self.assertEqual((self.project/'backend/version.py').read_bytes(),android_source)
        self.assertEqual(prepare_version(self.project,'android'),'1.3.4.2')

    def test_new_android_prefix_restarts_and_other_platforms_use_it(self):
        first=prepare_version(self.project,'android');self.complete(first)
        second=prepare_version(self.project,'android',requested='1.3.5')
        self.assertEqual(second,'1.3.5.1');self.complete(second)
        self.assertEqual(prepare_version(self.project,'windows'),'1.3.5.1')
        self.complete(second,'windows')
        self.assertEqual(prepare_version(self.project,'android'),'1.3.5.2')

    def test_failed_build_retry_reuses_the_exact_version_and_install_code(self):
        first=prepare_version(self.project,'android')
        before=(self.project/'backend/version.py').read_text()
        self.assertEqual(prepare_version(self.project,'android'),first)
        self.assertEqual(prepare_version(self.project,'android',reuse=True),first)
        self.assertEqual((self.project/'backend/version.py').read_text(),before)
        self.complete(first)
        self.assertEqual(prepare_version(self.project,'android'),'1.3.4.2')

    def test_preflight_never_creates_state_or_modifies_the_source(self):
        before=(self.project/'backend/version.py').read_bytes()
        self.assertEqual(prepare_version(self.project,'android',requested='1.3.5',preview=True),'1.3.5.1')
        self.assertEqual((self.project/'backend/version.py').read_bytes(),before)
        self.assertFalse((self.project/'build').exists())

    def test_explicit_full_version_and_reserved_retry_are_respected(self):
        self.assertEqual(prepare_version(self.project,'android',requested='1.3.5.8'),'1.3.5.8')
        before=(self.project/'backend/version.py').read_bytes()
        self.assertEqual(prepare_version(self.project,'android',requested='1.3.5.8'),'1.3.5.8')
        self.assertEqual((self.project/'backend/version.py').read_bytes(),before)

    def test_platform_version_checks_and_preflight_do_not_allocate_versions(self):
        for platform in ('ios','windows'):
            with self.assertRaises(ValueError):prepare_version(self.project,platform,preview=True)
        first=prepare_version(self.project,'android');self.complete(first)
        before=(self.project/'backend/version.py').read_bytes()
        state=(self.project/'build/release-version.json').read_bytes()
        for platform in ('ios','windows'):
            self.assertEqual(prepare_version(self.project,platform,preview=True),first)
            with self.assertRaises(ValueError):prepare_version(self.project,platform,requested='1.3.5',preview=True)
            with self.assertRaises(ValueError):prepare_version(self.project,platform,requested='1.3.4.2',preview=True)
        self.assertEqual((self.project/'backend/version.py').read_bytes(),before)
        self.assertEqual((self.project/'build/release-version.json').read_bytes(),state)

    def test_failed_android_base_change_survives_a_windows_build(self):
        first=prepare_version(self.project,'android');self.complete(first)
        pending=prepare_version(self.project,'android',requested='1.3.5')
        self.assertEqual(prepare_version(self.project,'windows'),first)
        self.complete(first,'windows')
        self.assertEqual(prepare_version(self.project,'android',reuse=True),pending)
        self.complete(pending)
        self.assertEqual(prepare_version(self.project,'ios'),pending)

    def test_legacy_windows_counter_does_not_skip_an_android_revision(self):
        (self.project/'backend/version.py').write_text('APP_VERSION = "1.3.4.3"\nANDROID_VERSION_CODE = 10313\n')
        directory=self.project/'build/android-skill';directory.mkdir(parents=True)
        apk=self.project/'android-1.3.4.2.apk';apk.write_bytes(b'verified-test-apk')
        (directory/'last-success.json').write_text(json.dumps({'status':'ok','version':'1.3.4.2','version_code':10312,'apk':str(apk)}))
        windows=self.project/'windows-1.3.4.3.exe';windows.write_bytes(b'verified-test-installer')
        (self.project/'build/release-version.json').write_text(json.dumps({'counters':{'1.3.4':3},'reservation':{
            'platform':'windows','version':'1.3.4.3','androidCode':10313,'status':'complete','artifact':str(windows)}}))
        before=(self.project/'build/release-version.json').read_bytes()
        self.assertEqual(prepare_version(self.project,'ios',preview=True),'1.3.4.2')
        self.assertEqual((self.project/'build/release-version.json').read_bytes(),before)
        self.assertEqual(prepare_version(self.project,'windows'),'1.3.4.2')
        self.complete('1.3.4.2','windows')
        self.assertEqual(prepare_version(self.project,'android'),'1.3.4.3')
        self.assertIn('ANDROID_VERSION_CODE = 10314',(self.project/'backend/version.py').read_text())

    def test_missing_counter_recovers_the_last_source_revision_and_monotonic_code(self):
        (self.project/'backend/version.py').write_text('APP_VERSION = "1.3.4.19"\nANDROID_VERSION_CODE = 15000\n')
        self.assertEqual(prepare_version(self.project,'android'),'1.3.4.20')
        self.assertIn('ANDROID_VERSION_CODE = 15001',(self.project/'backend/version.py').read_text())

    def test_invalid_version_and_missing_artifact_do_not_complete_a_reservation(self):
        with self.assertRaises(ValueError):prepare_version(self.project,'android',requested='1.3.4.0')
        with self.assertRaises(ValueError):prepare_version(self.project,'android',requested='bad/version')
        version=prepare_version(self.project,'android')
        with self.assertRaises(ValueError):finish_release(self.project,version,self.project/'missing.apk')
        self.assertEqual(prepare_version(self.project,'android'),version)

    def test_update_comparison_uses_all_four_numeric_parts(self):
        for newer,older in [('1.3.4.1','1.3.4'),('1.3.4.10','1.3.4.9'),('1.3.5.1','1.3.4.100'),('1.3.4.1','1.3.4.1-beta')]:
            self.assertTrue(_newer(newer,older));self.assertFalse(_newer(older,newer))

    def test_android_skill_reads_four_parts_and_the_monotonic_install_code(self):
        prepare_version(self.project,'android')
        path=Path.home()/'.codex/skills/lucima-android-build/scripts/build_android.py'
        spec=importlib.util.spec_from_file_location('android_skill_version',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertEqual(module.project_version(self.project),('1.3.4.1',10311))

    def test_ios_native_and_application_display_versions_remain_consistent(self):
        directory=Path.home()/'.codex/skills/lucima-ios-build/scripts'
        sys.path.insert(0,str(directory));self.addCleanup(lambda:sys.path.remove(str(directory)))
        spec=importlib.util.spec_from_file_location('ios_skill_version',directory/'build_ios.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        ios=self.project/'ios';(ios/'LucimaTools.xcodeproj').mkdir(parents=True);(ios/'LucimaTools').mkdir()
        project=ios/'LucimaTools.xcodeproj/project.pbxproj'
        project.write_text(('MARKETING_VERSION = 1.0.0;\nCURRENT_PROJECT_VERSION = 10;\n')*2)
        info=ios/'LucimaTools/Info.plist';info.write_bytes(plistlib.dumps({'CFBundleShortVersionString':'$(MARKETING_VERSION)'}))
        module.set_version(ios,'1.3.4.21',99)
        self.assertEqual(project.read_text().count('MARKETING_VERSION = "1.3.4";'),2)
        self.assertEqual(plistlib.loads(info.read_bytes())['LucimaDisplayVersion'],'1.3.4.21')


if __name__=='__main__':unittest.main()
