package com.nickmarcha.bragi

import android.content.Context
import android.content.pm.PackageManager
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray

/** Android tags form a separate release channel from the desktop client. */
data class AppVersion(val major: Int, val minor: Int, val patch: Int) : Comparable<AppVersion> {
    override fun compareTo(other: AppVersion): Int = compareValuesBy(this, other, AppVersion::major, AppVersion::minor, AppVersion::patch)
    override fun toString() = "$major.$minor.$patch"
    companion object {
        fun parse(value: String): AppVersion? {
            val parts = Regex("^(\\d+)\\.(\\d+)\\.(\\d+)$").matchEntire(value)?.groupValues?.drop(1) ?: return null
            val numbers = parts.map { it.toIntOrNull() ?: return null }
            return AppVersion(numbers[0], numbers[1], numbers[2])
        }
    }
}

data class AppUpdate(val version: AppVersion, val url: String, val sha256: String)

fun androidUpdate(releases: JSONArray, installed: AppVersion): AppUpdate? {
    val candidates = mutableListOf<AppUpdate>()
    for (i in 0 until releases.length()) {
        val release = releases.getJSONObject(i)
        if (release.optBoolean("draft") || release.optBoolean("prerelease")) continue
        val tag = release.optString("tag_name")
        if (!tag.startsWith("android-v")) continue
        val version = AppVersion.parse(tag.removePrefix("android-v")) ?: continue
        if (version <= installed) continue
        val assets = release.optJSONArray("assets") ?: continue
        for (j in 0 until assets.length()) {
            val asset = assets.getJSONObject(j)
            if (asset.optString("name") != "BragiAndroid.apk") continue
            val url = asset.optString("browser_download_url")
            val hash = asset.optString("digest").removePrefix("sha256:")
            if (!url.startsWith("https://github.com/NickMarcha/bragi/releases/download/$tag/") ||
                !Regex("^[0-9a-fA-F]{64}$").matches(hash)) continue
            candidates += AppUpdate(version, url, hash.lowercase())
        }
    }
    return candidates.maxByOrNull { it.version }
}

class GitHubUpdates {
    private val http = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS).readTimeout(30, TimeUnit.SECONDS).build()

    suspend fun check(): AppUpdate? = withContext(Dispatchers.IO) {
        val installed = requireNotNull(AppVersion.parse(BuildConfig.VERSION_NAME))
        var best: AppUpdate? = null
        // Paginate because this repository also contains desktop releases.
        for (page in 1..5) {
            val request = Request.Builder().url("https://api.github.com/repos/NickMarcha/bragi/releases?per_page=100&page=$page")
                .header("Accept", "application/vnd.github+json").header("User-Agent", "Bragi-Android/${BuildConfig.VERSION_NAME}").build()
            val releases = http.newCall(request).execute().use { response ->
                if (!response.isSuccessful) throw IOException("GitHub update check failed (${response.code}). Try again later.")
                JSONArray(response.body?.string() ?: throw IOException("GitHub returned an empty response."))
            }
            androidUpdate(releases, installed)?.let { candidate -> if (best == null || candidate.version > best!!.version) best = candidate }
            if (releases.length() < 100) break
        }
        best
    }

    suspend fun download(context: Context, update: AppUpdate): File = withContext(Dispatchers.IO) {
        val directory = File(context.cacheDir, "updates").apply { mkdirs() }
        val partial = File(directory, "update.part")
        val apk = File(directory, "BragiAndroid.apk")
        try {
            val digest = MessageDigest.getInstance("SHA-256")
            http.newCall(Request.Builder().url(update.url).build()).execute().use { response ->
                if (!response.isSuccessful) throw IOException("APK download failed (${response.code}).")
                val body = response.body ?: throw IOException("Empty APK download.")
                body.byteStream().use { input -> partial.outputStream().use { output ->
                    val buffer = ByteArray(8192)
                    var total = 0L
                    while (true) {
                        val count = input.read(buffer)
                        if (count == -1) break
                        total += count
                        if (total > 256L * 1024 * 1024) throw IOException("APK download is too large.")
                        digest.update(buffer, 0, count)
                        output.write(buffer, 0, count)
                    }
                } }
            }
            val actual = digest.digest().joinToString("") { "%02x".format(it) }
            check(actual == update.sha256) { "APK checksum did not match the GitHub release." }
            verifyPackage(context, partial)
            if (apk.exists()) check(apk.delete()) { "Could not replace the cached APK." }
            check(partial.renameTo(apk)) { "Could not save the APK." }
            apk
        } catch (error: Exception) { partial.delete(); throw error }
    }

    @Suppress("DEPRECATION")
    private fun verifyPackage(context: Context, file: File) {
        val flags = PackageManager.GET_SIGNING_CERTIFICATES
        val downloaded = context.packageManager.getPackageArchiveInfo(file.absolutePath, flags)
            ?: throw IOException("Downloaded file is not an Android APK.")
        val installed = context.packageManager.getPackageInfo(context.packageName, flags)
        check(downloaded.packageName == context.packageName && downloaded.longVersionCode > installed.longVersionCode) {
            "Downloaded APK is not a newer Bragi app."
        }
        val signatures = downloaded.signingInfo?.apkContentsSigners ?: throw IOException("APK is not signed.")
        val existing = installed.signingInfo?.apkContentsSigners ?: throw IOException("Installed app signature is unavailable.")
        check(signatures.size == existing.size && signatures.toSet() == existing.toSet()) { "Update is signed with a different key." }
    }
}
