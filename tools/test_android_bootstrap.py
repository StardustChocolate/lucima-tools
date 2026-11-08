"""Exercise production asynchronous Android bootstrap with JVM platform adapters."""
from pathlib import Path
import os
import subprocess
import tempfile
from tools.test_android_asset_sync import jar, ROOT


def main():
    java=Path.home()/'.local/share/starfall-tools/jdk17-root/Library/Java/JavaVirtualMachines/temurin-17.jdk/Contents/Home/bin/java'
    stdlib=jar('org.jetbrains.kotlin','kotlin-stdlib','1.9.24')
    dependencies=[jar('org.jetbrains.kotlin','kotlin-compiler-embeddable','1.9.24'),stdlib,
        jar('org.jetbrains.kotlin','kotlin-script-runtime','1.9.24'),jar('org.jetbrains.kotlin','kotlin-reflect','1.6.10'),
        jar('org.jetbrains.intellij.deps','trove4j','1.0.20200330'),jar('org.jetbrains','annotations','13.0')]
    with tempfile.TemporaryDirectory() as tmp:
        work=Path(tmp)
        sources={
'Context.kt':'''package android.content
import java.io.File
class ApplicationInfo { val sourceDir="test.apk" }
class Context(val filesDir: File) { val applicationContext get() = this; val applicationInfo=ApplicationInfo() }
''',
'Log.kt':'''package android.util
object Log {
    fun e(tag:String,message:String,error:Throwable) = 0
    fun getStackTraceString(error:Throwable) = error.stackTraceToString()
}
''',
'Platform.kt':'''package com.chaquo.python.android
import android.content.Context
class AndroidPlatform(val context: Context)
''',
'Python.kt':'''package com.chaquo.python
import com.chaquo.python.android.AndroidPlatform
import java.util.concurrent.CountDownLatch
import java.util.concurrent.atomic.AtomicInteger
class Python {
    fun getModule(name:String) = this
    fun callAttr(name:String,vararg args:Any): Result {
        if(name=="running_port") return Result(runningPort)
        if(failServer) { failServer=false;throw IllegalStateException("simulated bind startup failure") }
        preferredPort=args[3] as Int
        lastInstanceId=args[5] as String
        runningPort=32001
        launches.incrementAndGet()
        return Result(32001)
    }
    class Result(val port:Int) { fun toInt()=port }
    companion object {
        @Volatile var active=false
        @Volatile var fail=true
        @Volatile var failServer=true
        @Volatile var runningPort=0
        var preferredPort=0
        var lastInstanceId=""
        val gate=CountDownLatch(1)
        val entered=CountDownLatch(1)
        val starts=AtomicInteger()
        val launches=AtomicInteger()
        fun isStarted()=active
        fun start(platform:AndroidPlatform) {
            check(Thread.currentThread().name != "main")
            starts.incrementAndGet();entered.countDown();gate.await()
            if(fail) { fail=false;throw IllegalStateException("simulated native startup failure") }
            active=true
        }
        fun getInstance()=Python()
    }
}
''',
'Adapters.kt':'''package com.openrubi.lucimatools
import android.content.Context
import java.io.File
object EquipmentNotifications { fun initialize(context:Context) {} }
object AssetSync {
    @Volatile var fail=true
    fun syncWeb(context:Context,target:File) {
        check(Thread.currentThread().name != "main")
        if(fail) { fail=false;throw IllegalStateException("simulated extraction failure") }
    }
}
''',
'Checks.kt':'''import android.content.Context
import com.chaquo.python.Python
import com.openrubi.lucimatools.AppBootstrap
import com.openrubi.lucimatools.LocalServerEndpoint
import com.sun.net.httpserver.HttpServer
import java.net.InetSocketAddress
import java.io.File
import java.util.concurrent.TimeUnit
fun until(check:()->Boolean) {
    val end=System.nanoTime()+TimeUnit.SECONDS.toNanos(5)
    while(!check()) { check(System.nanoTime()<end) { "timed out" };Thread.sleep(5) }
}
fun main(args:Array<String>) {
    val context=Context(File(args[0]))
    val begin=System.nanoTime()
    AppBootstrap.ensureStarted(context)
    check(System.nanoTime()-begin < TimeUnit.SECONDS.toNanos(1))
    check(Python.entered.await(2,TimeUnit.SECONDS))
    AppBootstrap.ensureStarted(context);check(Python.starts.get()==1)
    Python.gate.countDown();until { AppBootstrap.failure != null }
    until { File(context.filesDir,"logs/startup.log").exists() }
    check(AppBootstrap.failure!!.contains("simulated native startup failure"))
    Thread.sleep(20);AppBootstrap.ensureStarted(context)
    until { AppBootstrap.failure?.contains("simulated extraction failure") == true }
    Thread.sleep(20);AppBootstrap.ensureStarted(context)
    until { AppBootstrap.failure?.contains("simulated bind startup failure") == true }
    check(AppBootstrap.endpoint==null)
    Thread.sleep(20);AppBootstrap.ensureStarted(context)
    until { AppBootstrap.endpoint != null }
    check(AppBootstrap.failure==null)
    check(AppBootstrap.endpoint!!.port==32001)
    check(Python.preferredPort==27843)
    check(AppBootstrap.endpoint!!.instanceId==Python.lastInstanceId)
    check(AppBootstrap.endpoint!!.url=="http://127.0.0.1:32001/")
    AppBootstrap.ensureStarted(context);Thread.sleep(20);check(Python.launches.get()==1)
    // Health recovery restarts a stopped worker, while a slow live server is reused.
    Thread.sleep(50);Python.runningPort=0
    AppBootstrap.ensureStarted(context)
    until { Python.launches.get()==2 }
    check(AppBootstrap.endpoint!!.port==32001)
    val http=HttpServer.create(InetSocketAddress("127.0.0.1",0),0)
    var responseInstance="ours"
    var status=200
    var healthCalls=0
    http.createContext("/api/health") { exchange ->
        healthCalls++
        exchange.responseHeaders.set("X-Lucima-Instance",responseInstance)
        exchange.responseHeaders.set("Location","http://127.0.0.1:1/")
        exchange.sendResponseHeaders(status,0)
        exchange.responseBody.close()
    }
    http.start()
    val endpoint=LocalServerEndpoint(http.address.port,"ours")
    try {
        check(endpoint.isReady())
        responseInstance="another-app";check(!endpoint.isReady())
        responseInstance="ours";status=503;check(!endpoint.isReady())
        status=302;check(!endpoint.isReady())
        check(healthCalls==4)
    } finally { http.stop(0) }
    check(!endpoint.isReady())
    check(runCatching { LocalServerEndpoint(0,"ours") }.isFailure)
    check(runCatching { LocalServerEndpoint(32001,"") }.isFailure)
    println("PASS: asynchronous bootstrap; native/extraction/server failure retries; returned port; instance ownership; readiness, foreign listener, HTTP error, redirect, offline rejection")
}
'''}
        paths=[]
        for name,source in sources.items():
            path=work/name;path.write_text(source);paths.append(str(path))
        classes=work/'classes'
        compile=[str(java),'-cp',os.pathsep.join(map(str,dependencies)),'org.jetbrains.kotlin.cli.jvm.K2JVMCompiler',
            '-no-stdlib','-no-reflect','-classpath',str(stdlib),'-d',str(classes),*paths,
            str(ROOT/'android/app/src/main/java/com/openrubi/lucimatools/AppBootstrap.kt'),
            str(ROOT/'android/app/src/main/java/com/openrubi/lucimatools/LocalServerEndpoint.kt')]
        compiled=subprocess.run(compile,capture_output=True,text=True)
        if compiled.returncode:raise RuntimeError(compiled.stdout+compiled.stderr)
        result=subprocess.run([str(java),'-cp',os.pathsep.join(map(str,[classes,stdlib])),'ChecksKt',str(work)],capture_output=True,text=True)
        (ROOT/'build/android-port-conflict-review/bootstrap-tests.log').write_text(result.stdout+result.stderr)
        if result.returncode:raise RuntimeError(result.stdout+result.stderr)
        print(result.stdout.strip())


if __name__=='__main__':main()
