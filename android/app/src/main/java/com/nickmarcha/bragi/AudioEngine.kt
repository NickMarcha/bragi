package com.nickmarcha.bragi

import android.annotation.SuppressLint
import android.media.*
import android.media.projection.MediaProjection
import android.os.Process
import android.os.SystemClock
import java.io.IOException
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.rocstreaming.roctoolkit.*
import kotlin.math.max
import kotlin.math.abs

/** Android devices clock the PCM loops; Roc uses its external clock to avoid clock drift. */
class AudioEngine(
    private val host: String,
    private val localIp: String,
    private val config: PeerConfig,
    private val mode: CaptureMode,
    private val projection: MediaProjection?,
    private val onStatus: (Boolean, Boolean, String?) -> Unit,
) {
    private val changes = Mutex()
    private var sender: Worker? = null
    private var receiver: Worker? = null
    private val sending = AtomicBoolean(false)
    private val receiving = AtomicBoolean(false)

    suspend fun apply(sendEnabled: Boolean, receiveEnabled: Boolean) = withContext(Dispatchers.IO) {
        changes.withLock {
        if (!sendEnabled || mode == CaptureMode.NONE) { sender?.stop(); sender = null }
        else if (sender?.isAlive != true) { sender = Worker(true).also { it.start() } }
        if (!receiveEnabled) { receiver?.stop(); receiver = null }
        else if (receiver?.isAlive != true) { receiver = Worker(false).also { it.start() } }
        onStatus(sending.get(), receiving.get(), null)
        }
    }

    suspend fun stop() = apply(false, false)

    private inner class Worker(private val send: Boolean) {
        private val running = AtomicBoolean(true)
        @Volatile private var record: AudioRecord? = null
        @Volatile private var track: AudioTrack? = null
        private val thread = Thread({
            Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO)
            try { if (send) sendAudio() else receiveAudio() }
            catch (error: Throwable) {
                Diagnostics.record("${if (send) "Sender" else "Receiver"} worker failed", error)
                if (running.get()) onStatus(sending.get(), receiving.get(), "${if (send) "Sender" else "Receiver"}: ${error.message ?: error.javaClass.simpleName}")
            } finally {
                if (send) sending.set(false) else receiving.set(false)
                onStatus(sending.get(), receiving.get(), null)
            }
        }, if (send) "Bragi sender" else "Bragi receiver")
        val isAlive get() = thread.isAlive
        fun start() = thread.start()
        fun stop() {
            Diagnostics.record("Stopping ${if (send) "sender" else "receiver"} worker")
            running.set(false)
            runCatching { record?.stop() }
            runCatching { track?.pause(); track?.flush() }
            thread.interrupt()
            thread.join(4000)
            Diagnostics.record("Worker join completed; alive=${thread.isAlive}")
            check(!thread.isAlive) { "Audio did not stop. Restart Bragi before starting another stream." }
        }

        @SuppressLint("MissingPermission")
        private fun sendAudio() {
            val channels = if (mode == CaptureMode.MICROPHONE) 1 else 2
            val mask = if (channels == 1) AudioFormat.CHANNEL_IN_MONO else AudioFormat.CHANNEL_IN_STEREO
            val minBuffer = AudioRecord.getMinBufferSize(44100, mask, AudioFormat.ENCODING_PCM_16BIT)
            require(minBuffer > 0) { "This device does not support 44.1 kHz capture." }
            val format = AudioFormat.Builder().setSampleRate(44100).setChannelMask(mask)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build()
            val builder = AudioRecord.Builder().setAudioFormat(format).setBufferSizeInBytes(max(minBuffer, 441 * channels * 2 * 4))
            if (mode == CaptureMode.MICROPHONE) builder.setAudioSource(MediaRecorder.AudioSource.MIC)
            else builder.setAudioPlaybackCaptureConfig(AudioPlaybackCaptureConfiguration.Builder(
                requireNotNull(projection) { "Open Bragi to grant device audio capture permission." })
                .addMatchingUsage(AudioAttributes.USAGE_MEDIA).addMatchingUsage(AudioAttributes.USAGE_GAME)
                .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN).excludeUid(Process.myUid()).build())
            val capture = builder.build()
            record = capture
            try {
                check(capture.state == AudioRecord.STATE_INITIALIZED) { "Audio recorder could not initialize." }
                RocContext().use { context ->
                    val senderConfig = RocSenderConfig.builder().frameSampleRate(44100)
                        .frameChannels(ChannelSet.STEREO).frameEncoding(FrameEncoding.PCM_FLOAT)
                        .packetSampleRate(44100).packetChannels(ChannelSet.STEREO).packetEncoding(PacketEncoding.AVP_L16)
                        .clockSource(ClockSource.EXTERNAL).fecEncoding(FecEncoding.DISABLE).build()
                    RocSender(context, senderConfig).use { roc ->
                        roc.connect(Slot.DEFAULT, Interface.AUDIO_SOURCE, Endpoint("rtp://$host:${config.sendSourcePort}"))
                        roc.connect(Slot.DEFAULT, Interface.AUDIO_CONTROL, Endpoint("rtcp://$host:${config.sendControlPort}"))
                        if (!running.get()) return
                        capture.startRecording()
                        check(capture.recordingState == AudioRecord.RECORDSTATE_RECORDING) { "Android did not start recording." }
                        sending.set(true)
                        onStatus(sending.get(), receiving.get(), null)
                        val pcm = ShortArray(441 * channels)
                        while (running.get()) {
                            val count = capture.read(pcm, 0, pcm.size, AudioRecord.READ_BLOCKING)
                            if (!running.get()) break
                            if (count < 0) throw IOException("Audio capture failed ($count).")
                            if (count > 0) roc.write(stereoSamples(pcm, count - count % channels, channels))
                        }
                    }
                }
            } finally { Diagnostics.record("Releasing microphone/device audio recorder"); runCatching { capture.stop() }; capture.release(); record = null; Diagnostics.record("Recorder released") }
        }

        private fun receiveAudio() {
            val minBuffer = AudioTrack.getMinBufferSize(44100, AudioFormat.CHANNEL_OUT_STEREO, AudioFormat.ENCODING_PCM_FLOAT)
            require(minBuffer > 0) { "This device does not support floating-point stereo playback." }
            val playback = AudioTrack.Builder()
                .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setAudioFormat(AudioFormat.Builder().setSampleRate(44100).setChannelMask(AudioFormat.CHANNEL_OUT_STEREO)
                    .setEncoding(AudioFormat.ENCODING_PCM_FLOAT).build())
                .setTransferMode(AudioTrack.MODE_STREAM).setBufferSizeInBytes(max(minBuffer, 882 * 4 * 2)).build()
            track = playback
            try {
                RocContext().use { context ->
                    val receiverConfig = playbackReceiverConfig(playback.bufferSizeInFrames)
                    RocReceiver(context, receiverConfig).use { roc ->
                        roc.bind(Slot.DEFAULT, Interface.AUDIO_SOURCE, Endpoint("rtp://$localIp:${config.receiveSourcePort}"))
                        roc.bind(Slot.DEFAULT, Interface.AUDIO_CONTROL, Endpoint("rtcp://$localIp:${config.receiveControlPort}"))
                        Diagnostics.record("Receiver bound to $localIp:${config.receiveSourcePort}; target latency=${receiverConfig.targetLatency / 1_000_000}ms")
                        check(playback.state == AudioTrack.STATE_INITIALIZED) { "Audio playback could not initialize." }
                        if (!running.get()) return
                        playback.play()
                        Diagnostics.record("Playback started; rate=${playback.sampleRate}; buffer frames=${playback.bufferSizeInFrames}")
                        receiving.set(true)
                        onStatus(sending.get(), receiving.get(), null)
                        val samples = FloatArray(882)
                        var reportAt = SystemClock.elapsedRealtime()
                        var previousReadAt = reportAt
                        var largestReadGapMs = 0L
                        var chunks = 0
                        var silentChunks = 0
                        var peak = 0f
                        var previousUnderruns = playback.underrunCount
                        var previousHead = playback.playbackHeadPosition.toLong() and 0xffffffffL
                        while (running.get()) {
                            val readAt = SystemClock.elapsedRealtime()
                            largestReadGapMs = max(largestReadGapMs, readAt - previousReadAt)
                            previousReadAt = readAt
                            roc.read(samples)
                            var chunkPeak = 0f
                            for (sample in samples) chunkPeak = max(chunkPeak, abs(sample))
                            peak = max(peak, chunkPeak)
                            chunks++
                            if (chunkPeak == 0f) silentChunks++
                            var offset = 0
                            while (running.get() && offset < samples.size) {
                                val written = playback.write(samples, offset, samples.size - offset, AudioTrack.WRITE_BLOCKING)
                                if (written < 0) throw IOException("Audio playback failed ($written).")
                                if (written == 0) throw IOException("Audio playback stopped accepting samples.")
                                offset += written
                            }
                            val now = SystemClock.elapsedRealtime()
                            if (now - reportAt >= 5000) {
                                val underruns = playback.underrunCount
                                val head = playback.playbackHeadPosition.toLong() and 0xffffffffL
                                val advanced = (head - previousHead) and 0xffffffffL
                                val route = playback.routedDevice
                                Diagnostics.record("Playback health: elapsedMs=${now - reportAt}; decodedFrames=${chunks * 441}; silentChunks=$silentChunks/$chunks; peak=$peak; largestReadGapMs=$largestReadGapMs; underruns=${underruns - previousUnderruns}; playedFrames=$advanced; state=${playback.playState}; route=${route?.type}:${route?.productName}")
                                reportAt = now
                                previousUnderruns = underruns
                                previousHead = head
                                largestReadGapMs = 0
                                chunks = 0
                                silentChunks = 0
                                peak = 0f
                            }
                        }
                    }
                }
            } finally { Diagnostics.record("Releasing audio playback"); runCatching { playback.stop() }; playback.release(); track = null; Diagnostics.record("Playback released") }
        }
    }
}
