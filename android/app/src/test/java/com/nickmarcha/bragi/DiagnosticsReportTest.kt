package com.nickmarcha.bragi

import org.junit.Assert.*
import org.junit.Test

class DiagnosticsReportTest {
    @Test fun github_issue_preserves_log_text_and_identifies_the_device() {
        val url = diagnosticsIssueUrl("Error: a+b & c?\nStack frame", "0.1.4", "Fairphone FP5; Android 15")
        assertEquals("github.com", url.host)
        assertEquals("/NickMarcha/bragi/issues/new", url.encodedPath)
        assertTrue(url.queryParameter("body")!!.contains("Error: a+b & c?\nStack frame"))
        assertTrue(url.queryParameter("body")!!.contains("Fairphone FP5; Android 15"))
        assertEquals("Android app problem (Bragi 0.1.4)", url.queryParameter("title"))
    }

    @Test fun large_unicode_logs_fit_in_a_browser_url_and_keep_recent_events() {
        val url = diagnosticsIssueUrl("🙂".repeat(20000) + "\nMost recent failure", "0.1.4", "Fairphone FP5")
        assertTrue(url.toString().length <= 8000)
        assertTrue(url.queryParameter("body")!!.contains("Most recent failure"))
    }
}
