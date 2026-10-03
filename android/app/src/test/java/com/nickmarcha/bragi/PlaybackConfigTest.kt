package com.nickmarcha.bragi

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
}
