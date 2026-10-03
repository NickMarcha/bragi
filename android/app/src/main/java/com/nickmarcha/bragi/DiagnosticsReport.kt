package com.nickmarcha.bragi

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl

/** Keep the browser URL bounded; the full log can be pasted from the clipboard. */
fun diagnosticsIssueUrl(logs: String, version: String, device: String): HttpUrl {
    var excerpt = logs.takeLast(2500)
    while (true) {
        val body = """Bragi Android $version
Device: $device

What happened?
Describe the problem here.

Steps to reproduce:
1. 

Expected behavior:
Describe what you expected here.

Recent diagnostics excerpt:
````text
$excerpt
````

The app copied the full diagnostics log to the clipboard. Paste it below if this excerpt is insufficient.
"""
        val url = "https://github.com/NickMarcha/bragi/issues/new".toHttpUrl().newBuilder()
            .addQueryParameter("title", "Android app problem (Bragi $version)")
            .addQueryParameter("body", body).build()
        if (url.toString().length <= 8000 || excerpt.isEmpty()) return url
        excerpt = excerpt.takeLast(excerpt.length / 2)
    }
}
