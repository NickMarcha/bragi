package com.nickmarcha.bragi

import android.app.ActivityManager
import android.app.Application
import android.os.Build
import java.io.File
import java.time.Instant

/** Private, bounded event log that survives service shutdown and process death. */
object Diagnostics {
    private var file: File? = null

    @Synchronized
    fun record(event: String, error: Throwable? = null) {
        runCatching {
            val target = file ?: return
            if (target.length() > 256 * 1024) target.writeText(target.readText().takeLast(128 * 1024))
            target.appendText("${Instant.now()} $event\n" + (error?.stackTraceToString()?.let { "$it\n" } ?: ""))
        }
    }

    @Synchronized
    fun read(): String = runCatching { file?.readText() ?: "No diagnostics recorded." }
        .getOrElse { "Could not read diagnostics: ${it.message}" }

    @Synchronized
    fun clear(): Boolean = runCatching {
        val target = file ?: return false
        target.writeText("")
        record("Diagnostics cleared")
        true
    }.getOrElse { false }

    fun initialize(app: Application) {
        file = File(app.filesDir, "diagnostics.log")
        val original = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, error ->
            record("Uncaught exception on ${thread.name}", error)
            original?.uncaughtException(thread, error)
        }
        record("App opened. Bragi ${BuildConfig.VERSION_NAME}; Android ${Build.VERSION.RELEASE} API ${Build.VERSION.SDK_INT}; ${Build.MANUFACTURER} ${Build.MODEL}")
        if (Build.VERSION.SDK_INT >= 30) {
            runCatching {
                val preferences = app.getSharedPreferences("diagnostics", Application.MODE_PRIVATE)
                val last = preferences.getLong("exitTimestamp", 0)
                val exits = app.getSystemService(ActivityManager::class.java)
                    .getHistoricalProcessExitReasons(app.packageName, 0, 3)
                    .filter { it.timestamp > last }.sortedBy { it.timestamp }
                for (exit in exits) {
                    record("Previous process exit ${Instant.ofEpochMilli(exit.timestamp)}: reason=${exit.reason} status=${exit.status} description=${exit.description}")
                    // Native-crash/ANR traces may be unavailable on some Android versions.
                    exit.traceInputStream?.use { stream ->
                        val bytes = ByteArray(64 * 1024)
                        var count = 0
                        while (count < bytes.size) {
                            val read = stream.read(bytes, count, bytes.size - count)
                            if (read <= 0) break
                            count += read
                        }
                        val trace = String(bytes, 0, count, Charsets.UTF_8)
                        record("Android exit trace:\n$trace")
                    }
                }
                exits.lastOrNull()?.let { preferences.edit().putLong("exitTimestamp", it.timestamp).apply() }
            }.onFailure { record("Could not read Android exit details", it) }
        }
    }
}

class BragiApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        Diagnostics.initialize(this)
    }
}
