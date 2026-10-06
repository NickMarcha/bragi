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

    @Test fun health_runs_keep_their_first_and_last_lines_so_startup_events_fit() {
        // Issue #2's excerpt held only health lines; the bound address had scrolled out.
        val health = (1..40).flatMap { listOf("t$it Playback health: n=$it", "t$it Capture health: n=$it") }
        val logs = (listOf("t0 Receiver bound to 100.98.253.67:10045") + health + "t41 Stopping").joinToString("\n")
        assertEquals(listOf(
            "t0 Receiver bound to 100.98.253.67:10045",
            "t1 Playback health: n=1", "t1 Capture health: n=1",
            "… 76 health lines omitted …",
            "t40 Playback health: n=40", "t40 Capture health: n=40",
            "t41 Stopping",
        ).joinToString("\n"), collapseHealthLines(logs))
        assertTrue(diagnosticsIssueUrl(logs, "0.1.13", "FP5").queryParameter("logs")!!.contains("Receiver bound to"))
    }

    @Test fun short_health_runs_are_left_alone() {
        val logs = "a Playback health: 1\nb Playback health: 2\nc Stopping\nd Playback health: 3"
        assertEquals(logs, collapseHealthLines(logs))
    }
}
