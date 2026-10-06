package com.nickmarcha.bragi

import org.rocstreaming.roctoolkit.*

/** Roc reads are paced by Android's blocking writes, which can consume whole buffers in bursts. */
fun playbackReceiverConfig(bufferFrames: Int, sampleRate: Int = 44100): RocReceiverConfig {
    require(bufferFrames > 0) { "Playback buffer must contain audio frames." }
    // AudioTrack may drain this many frames before a blocking write returns.
    // The receiver needs that full burst available, plus its network jitter budget.
    val playbackBufferNanos = (bufferFrames.toLong() * 1_000_000_000L + sampleRate - 1) / sampleRate
    return RocReceiverConfig.builder().frameSampleRate(sampleRate)
        .frameChannels(ChannelSet.STEREO).frameEncoding(FrameEncoding.PCM_FLOAT)
        .clockSource(ClockSource.EXTERNAL).targetLatency(playbackBufferNanos + 40_000_000L).build()
}

/** Frames written but not yet heard, from an AudioTrack timestamp taken at [presentedAtNanos]. */
fun outputLatencyMs(framesWritten: Long, presentedFrame: Long, presentedAtNanos: Long, nowNanos: Long, sampleRate: Int): Long {
    val playedSince = (nowNanos - presentedAtNanos) * sampleRate / 1_000_000_000L
    return maxOf(0L, framesWritten - presentedFrame - playedSince) * 1000L / sampleRate
}

/**
 * How much of the AudioTrack buffer playback uses. Every queued frame is heard that much
 * later, so it starts at 20 ms and grows by half whenever Android reports underruns.
 */
class PlaybackBuffer(private val sampleRate: Int, private val capacityFrames: Int) {
    var frames = clamp(sampleRate / 50)
        private set

    fun grow(): Boolean {
        val next = clamp(frames * 3 / 2)
        if (next == frames) return false
        frames = next
        return true
    }

    private fun clamp(value: Int) = minOf(value, capacityFrames)
}

/** How long ago the newest frame read was captured, from an AudioRecord timestamp taken at [capturedAtNanos]. */
fun captureDelayMs(framesRead: Long, capturedFrame: Long, capturedAtNanos: Long, nowNanos: Long, sampleRate: Int): Long {
    val newestCapturedAtNanos = capturedAtNanos + (framesRead - capturedFrame) * 1_000_000_000L / sampleRate
    return maxOf(0L, nowNanos - newestCapturedAtNanos) / 1_000_000L
}

/** Delays wobble by a few ms every second; only appearing, disappearing, or a 5 ms move is news. */
fun delayWorthReporting(previous: Int?, next: Int?): Boolean =
    if (previous == null || next == null) previous != next else kotlin.math.abs(next - previous) >= 5
