package com.nickmarcha.bragi

import org.rocstreaming.roctoolkit.*

/** Roc reads are paced by Android's blocking writes, which can consume whole buffers in bursts. */
fun playbackReceiverConfig(bufferFrames: Int): RocReceiverConfig {
    require(bufferFrames > 0) { "Playback buffer must contain audio frames." }
    // AudioTrack may drain this many frames before a blocking write returns.
    // The receiver needs that full burst available, plus its network jitter budget.
    val playbackBufferNanos = (bufferFrames.toLong() * 1_000_000_000L + 44099L) / 44100L
    return RocReceiverConfig.builder().frameSampleRate(44100)
        .frameChannels(ChannelSet.STEREO).frameEncoding(FrameEncoding.PCM_FLOAT)
        .clockSource(ClockSource.EXTERNAL).targetLatency(playbackBufferNanos + 40_000_000L).build()
}
