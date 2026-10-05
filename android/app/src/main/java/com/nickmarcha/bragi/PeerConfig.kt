package com.nickmarcha.bragi

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import org.json.JSONObject

/** Directions are named from the server's perspective; phone sends to playback ports. */
data class PeerConfig(
    val name: String,
    val sendSourcePort: Int,
    val sendControlPort: Int,
    val receiveSourcePort: Int,
    val receiveControlPort: Int,
    val mode: CaptureMode,
    val sendEnabled: Boolean,
    val receiveEnabled: Boolean,
) {
    companion object {
        fun parse(value: JSONObject): PeerConfig {
            require(value.getString("fec") == "disable") { "This app needs Bragi's no-FEC configuration." }
            require(value.getInt("sample_rate") == 44100 && value.getInt("channels") == 2) { "Unsupported Bragi audio format." }
            val ports = value.getJSONObject("ports")
            fun port(key: String): Int = ports.getInt(key).also { require(it in 1..65535) { "Invalid audio port." } }
            return PeerConfig(value.getString("name"), port("playback_source"), port("playback_control"),
                port("mic_source"), port("mic_control"), CaptureMode.parse(value.getString("capture_mode")),
                value.getBoolean("send_enabled"), value.getBoolean("receive_enabled"))
        }
    }
}

enum class CaptureMode(val wireName: String) {
    MICROPHONE("microphone"), DEVICE_AUDIO("device_audio"), NONE("none");
    companion object {
        fun parse(value: String) = entries.firstOrNull { it.wireName == value }
            ?: throw IllegalArgumentException("Unknown audio source: $value")
    }
}

/** Debug builds reach only the local dev server (dev/README.md), so they can never touch production. */
private val LOCAL_DEV_HOSTS = setOf("10.0.2.2", "127.0.0.1", "localhost")

fun serverUrl(value: String, localDev: Boolean = BuildConfig.DEBUG): HttpUrl {
    val url = value.trim().trimEnd('/').toHttpUrl()
    if (localDev) require(url.host in LOCAL_DEV_HOSTS) { "Debug builds only connect to the local dev server, e.g. http://10.0.2.2:20080/." }
    else require(url.isHttps) { "Use the HTTPS Bragi URL supplied by Tailscale." }
    require(url.username.isEmpty() && url.password.isEmpty() && url.query == null && url.fragment == null) {
        "Enter the Bragi server URL without credentials, a query, or a fragment."
    }
    return url.newBuilder().addPathSegment("").build()
}

fun HttpUrl.endpoint(path: String): HttpUrl = newBuilder().addPathSegments(path).build()

fun validPeerName(value: String): Boolean = Regex("^[a-z][a-z0-9-]{0,39}$").matches(value)
/** The local dev server reaches an emulator through adb UDP redirects on its own loopback. */
fun validPeerIp(value: String, localDev: Boolean = BuildConfig.DEBUG): Boolean =
    if (localDev) value == "127.0.0.1" else validTailnetIp(value)

fun validTailnetIp(value: String): Boolean {
    val parts = value.split('.').map { it.toIntOrNull() ?: return false }
    return parts.size == 4 && parts[0] == 100 && parts[1] in 64..127 && parts.all { it in 0..255 }
}

/** Converts captured PCM16 to stereo floats; microphone mono is duplicated into both channels. */
fun stereoSamples(input: ShortArray, count: Int, channels: Int): FloatArray {
    require(channels == 1 || channels == 2)
    require(count in 0..input.size && count % channels == 0)
    return if (channels == 1) FloatArray(count * 2) { input[it / 2] / 32768f }
    else FloatArray(count) { input[it] / 32768f }
}
