"""Run the production Kotlin AssetSync against file-backed Android test adapters.

Uses the project's cached Kotlin compiler/JDK; no emulator, installation or network.
"""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path.home() / '.gradle/caches/modules-2/files-2.1'


def jar(group, artifact, version):
    matches = list((CACHE / group / artifact / version).glob('*/' + artifact + '-' + version + '.jar'))
    if len(matches) != 1:
        raise RuntimeError(f'Cached compiler dependency unavailable: {artifact}:{version}')
    return matches[0]


def main():
    java = Path(os.environ.get('JAVA_HOME', str(Path.home() / '.local/share/starfall-tools/jdk17-root/Library/Java/JavaVirtualMachines/temurin-17.jdk/Contents/Home'))) / 'bin/java'
    stdlib = jar('org.jetbrains.kotlin', 'kotlin-stdlib', '1.9.24')
    dependencies = [jar('org.jetbrains.kotlin', 'kotlin-compiler-embeddable', '1.9.24'), stdlib,
                    jar('org.jetbrains.kotlin', 'kotlin-script-runtime', '1.9.24'),
                    jar('org.jetbrains.kotlin', 'kotlin-reflect', '1.6.10'),
                    jar('org.jetbrains.intellij.deps', 'trove4j', '1.0.20200330'),
                    jar('org.jetbrains', 'annotations', '13.0')]
    output = ROOT / 'build/abyss-intel-delivery-review'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as temporary:
        work = Path(temporary)
        adapter = work / 'Context.kt'
        adapter.write_text('''package android.content
import java.io.File
class Assets(private val root: File) {
    var failedPath: String? = null
    fun list(path: String): Array<String> = File(root,path).list() ?: emptyArray()
    fun open(path: String): java.io.InputStream {
        if(path==failedPath) throw java.io.IOException("simulated resource read failure")
        return File(root,path).inputStream()
    }
}
class PackageInfo(val versionCode: Int=10300, val lastUpdateTime: Long=1234)
class PackageManager {
    var fail = false
    fun getPackageInfo(name: String, flags: Int): PackageInfo {
        if(fail) throw IllegalStateException("No install metadata")
        return PackageInfo()
    }
}
class Context(root: File) {
    val assets = Assets(root)
    val packageManager = PackageManager()
    val packageName = "com.openrubi.lucimatools"
}
''')
        runner = work / 'AssetSyncChecks.kt'
        runner.write_text('''import android.content.Context
import com.openrubi.lucimatools.AssetSync
import java.io.File
fun main(args: Array<String>) {
    val root=File(args[0]); val packaged=File(root,"packaged"); val target=File(root,"private/web")
    val names=listOf("index.html","app.js","style.css")
    fun pack(tag: String) { names.forEach { name -> File(packaged,"web/frontend/$name").apply {parentFile.mkdirs();writeText("$name:$tag")} } }
    fun matches(tag: String) { names.forEach { name -> check(File(target,"frontend/$name").readText()=="$name:$tag") } }
    val settings=File(root,"private/settings.json").apply {parentFile.mkdirs();writeText("keep user accounts")}
    val ctx=Context(packaged)
    pack("merged"); File(target,"frontend").mkdirs()
    names.forEach { name -> File(target,"frontend/$name").writeText("old separated panels") }
    File(target,".version").writeText("10300:1234")
    AssetSync.syncWeb(ctx,target); matches("merged")
    // A valid stamp must not hide a stale or damaged extracted frontend.
    File(target,"frontend/app.js").writeText("old separated panels")
    AssetSync.syncWeb(ctx,target); matches("merged")
    // Unchanged files skip the expensive complete resource extraction.
    val marker=File(target,"unchanged-marker").apply {writeText("preserve")}
    AssetSync.syncWeb(ctx,target); check(marker.exists())
    // Same versionCode AND same install time, but genuinely different APK contents.
    pack("merged-r2"); AssetSync.syncWeb(ctx,target); matches("merged-r2"); check(!marker.exists())
    // Missing target files and unavailable package timestamps also recover.
    File(target,"frontend/index.html").delete(); AssetSync.syncWeb(ctx,target); matches("merged-r2")
    ctx.packageManager.fail=true
    AssetSync.syncWeb(ctx,target)
    pack("merged-r3"); AssetSync.syncWeb(ctx,target); matches("merged-r3")
    // Interrupted extraction must fail, never leave a valid stamp or file-shaped directory.
    File(packaged,"web/assets/extra.json").apply { parentFile.mkdirs();writeText("resource") }
    pack("io-failure");ctx.assets.failedPath="web/assets/extra.json"
    check(runCatching { AssetSync.syncWeb(ctx,target) }.isFailure)
    check(!File(target,".version").exists())
    ctx.assets.failedPath=null;AssetSync.syncWeb(ctx,target);matches("io-failure")
    check(File(target,"assets/extra.json").readText()=="resource")
    check(settings.readText()=="keep user accounts")
    println("PASS: 7 production Kotlin asset-sync recovery checks; user data preserved.")
}
''')
        classes = work / 'classes'
        compiler = [str(java), '-cp', os.pathsep.join(map(str, dependencies)), 'org.jetbrains.kotlin.cli.jvm.K2JVMCompiler',
                    '-no-stdlib', '-no-reflect', '-classpath', str(stdlib), '-d', str(classes), str(adapter), str(runner),
                    str(ROOT / 'android/app/src/main/java/com/openrubi/lucimatools/AssetSync.kt')]
        result = subprocess.run(compiler, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        result = subprocess.run([str(java), '-cp', os.pathsep.join(map(str, [classes, stdlib])), 'AssetSyncChecksKt', str(work)], capture_output=True, text=True)
        (output / 'asset-sync-checks.log').write_text(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        print(result.stdout.strip())


if __name__ == '__main__':
    main()
