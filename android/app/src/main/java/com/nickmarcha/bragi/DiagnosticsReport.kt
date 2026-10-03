package com.nickmarcha.bragi

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl

/** Keep the browser URL bounded; the full log can be pasted from the clipboard. */
fun diagnosticsIssueUrl(logs: String, version: String, device: String): HttpUrl {
    var excerpt = logs.takeLast(2500)
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
