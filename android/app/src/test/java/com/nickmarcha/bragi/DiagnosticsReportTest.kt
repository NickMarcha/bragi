package com.nickmarcha.bragi

import org.junit.Assert.*
import org.junit.Test

class DiagnosticsReportTest {
    @Test fun github_issue_form_preserves_log_text_and_identifies_the_device() {
        val url = diagnosticsIssueUrl("Error: a+b & c?\nStack frame", "0.1.4", "Fairphone FP5; Android 15")
        assertEquals("github.com", url.host)
        assertEquals("/NickMarcha/bragi/issues/new", url.encodedPath)
        assertEquals("android-app.yml", url.queryParameter("template"))
        assertEquals("Error: a+b & c?\nStack frame", url.queryParameter("logs"))
        assertEquals("Fairphone FP5; Android 15", url.queryParameter("device"))
        assertEquals("0.1.4", url.queryParameter("app-version"))
        assertNull(url.queryParameter("body"))
    }

    @Test fun large_unicode_logs_fit_in_a_browser_url_and_keep_recent_events() {
        val url = diagnosticsIssueUrl("🙂".repeat(20000) + "\nMost recent failure", "0.1.4", "Fairphone FP5")
        assertTrue(url.toString().length <= 8000)
        assertTrue(url.queryParameter("logs")!!.contains("Most recent failure"))
    }
}
