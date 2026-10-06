package com.nickmarcha.bragi

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PlaybackConfigTest {
    @Test fun receiver_covers_fairphone_playback_bursts_without_excessive_delay() {
        // FP5 logs show a 7108-frame buffer and 166ms gaps between blocking writes.
        // At 40ms latency the real Roc receiver reproduces 68.8% silent chunks;
        // 202ms eliminates them under the same 160ms burst schedule.
        val latency = playbackReceiverConfig(7108).targetLatency
        assertTrue("Receiver must cover a full playback burst plus network headroom", latency >= 200_000_000L)
        assertTrue("Avoid unnecessary extra listening delay", latency <= 220_000_000L)
    }

    @Test fun smaller_playback_buffers_keep_lower_listening_latency() {
        val latency = playbackReceiverConfig(882).targetLatency
        assertTrue("Keep network headroom in addition to the 20ms playback buffer", latency >= 60_000_000L)
        assertTrue("Do not force FP5 latency on devices with small buffers", latency < 100_000_000L)
    }

    @Test fun output_latency_counts_written_frames_not_yet_heard() {
        // 4800 frames queued past the presented one at 48 kHz is 100 ms.
        assertEquals(100L, outputLatencyMs(48_000, 43_200, 0, 0, 48_000))
        // 50 ms after that timestamp, playback has moved on by 2400 frames.
        assertEquals(50L, outputLatencyMs(48_000, 43_200, 0, 50_000_000, 48_000))
        // A stale timestamp never reports negative delay.
        assertEquals(0L, outputLatencyMs(48_000, 43_200, 0, 1_000_000_000, 48_000))
    }

    @Test fun playback_buffer_starts_small_and_grows_by_half_until_capacity() {
        val buffer = PlaybackBuffer(sampleRate = 48_000, capacityFrames = 4_360)
        assertEquals(960, buffer.frames) // 20 ms
        assertTrue(buffer.grow())
        assertEquals(1_440, buffer.frames) // 30 ms
        while (buffer.grow()) Unit
        assertEquals(4_360, buffer.frames)
        assertFalse("A full buffer cannot grow further", buffer.grow())
    }

    @Test fun playback_buffer_never_exceeds_a_small_track() {
        assertEquals(600, PlaybackBuffer(sampleRate = 48_000, capacityFrames = 600).frames)
    }

    @Test fun capture_delay_is_the_age_of_the_newest_frame_read() {
        // Frame 48 000 was captured at t=0; reading up to frame 48 000 at t=30 ms is 30 ms behind.
        assertEquals(30L, captureDelayMs(48_000, 48_000, 0, 30_000_000, 48_000))
        // 2400 frames past the timestamp were captured 50 ms later, so 50 ms of the wait is accounted for.
        assertEquals(10L, captureDelayMs(50_400, 48_000, 0, 60_000_000, 48_000))
        // Clock skew between the timestamp and now never reports negative delay.
        assertEquals(0L, captureDelayMs(52_800, 48_000, 0, 60_000_000, 48_000))
    }

    @Test fun reported_delays_change_only_by_noticeable_amounts() {
        assertTrue(delayWorthReporting(null, 102))
        assertTrue(delayWorthReporting(102, null))
        assertFalse(delayWorthReporting(102, 105))
        assertTrue(delayWorthReporting(102, 107))
        assertTrue(delayWorthReporting(102, 97))
    }

    @Test fun received_traffic_tells_missing_packets_from_silent_ones() {
        // Roc plays zeros both when no packets arrive and when they carry silence (issue #2).
        assertEquals(500L, receivedKiB(previousBytes = 1_000_000, currentBytes = 1_512_000))
        assertEquals(0L, receivedKiB(previousBytes = 1_000_000, currentBytes = 1_000_000))
        // TrafficStats.UNSUPPORTED is -1.
        assertNull(receivedKiB(previousBytes = -1, currentBytes = 1_000_000))
        assertNull(receivedKiB(previousBytes = 1_000_000, currentBytes = -1))
    }
}
