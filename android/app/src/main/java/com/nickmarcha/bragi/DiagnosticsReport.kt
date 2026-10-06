package com.nickmarcha.bragi

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl

/** Keep the browser URL bounded; the full log can be pasted from the clipboard. */
fun diagnosticsIssueUrl(logs: String, version: String, device: String): HttpUrl {
    var excerpt = collapseHealthLines(logs).takeLast(2500)
    while (true) {
        val url = "https://github.com/NickMarcha/bragi/issues/new".toHttpUrl().newBuilder()
            .addQueryParameter("template", "android-app.yml")
            .addQueryParameter("app-version", version)
            .addQueryParameter("device", device)
            .addQueryParameter("logs", excerpt).build()
        if (url.toString().length <= 8000 || excerpt.isEmpty()) return url
        excerpt = excerpt.takeLast(excerpt.length / 2)
    }
}

private val healthKinds = listOf("Playback health:", "Capture health:")

/**
 * Health lines arrive every 5 s and push startup events out of the excerpt. In each
 * unbroken run of them, keep the first and last line of each kind and count the rest.
 */
fun collapseHealthLines(logs: String): String {
    val out = mutableListOf<String>()
    val run = mutableListOf<String>()
    fun flush() {
        val keep = healthKinds.flatMap { kind ->
            val lines = run.filter { kind in it }
            listOfNotNull(lines.firstOrNull(), lines.lastOrNull()).distinct()
        }.toSet()
        val omitted = run.size - keep.size
        var marked = false
        for (line in run) {
            if (line in keep) out += line
            else if (!marked) { out += "… $omitted health lines omitted …"; marked = true }
        }
        run.clear()
    }
    for (line in logs.split('\n')) {
        if (healthKinds.any { it in line }) run += line else { flush(); out += line }
    }
    flush()
    return out.joinToString("\n")
}
