package com.nickmarcha.bragi

import android.app.*
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.*
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow

/** User starts this once; Bragi commands toggle streams inside the existing service. */
class BragiService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var session: Job? = null
    private var connection: BragiConnection? = null
    private var engine: AudioEngine? = null
    private var projection: MediaProjection? = null
    private var projectionCallback: MediaProjection.Callback? = null
    @Volatile private var destroyed = false
    private var wakeLock: PowerManager.WakeLock? = null
    private var wakeRenewal: Job? = null

    data class State(val running: Boolean = false, val connection: String = "Stopped",
                     val sending: Boolean = false, val receiving: Boolean = false, val error: String? = null)
    companion object {
        private val mutableState = MutableStateFlow(State())
        val state = mutableState.asStateFlow()
        const val STOP = "com.nickmarcha.bragi.STOP"
        const val CHANNEL = "bragi-audio"
        const val NOTIFICATION = 1
    }

    override fun onBind(intent: Intent?) = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == STOP) { stopSelf(); return START_NOT_STICKY }
        if (intent == null) { stopSelf(); return START_NOT_STICKY }
        if (session != null) return START_NOT_STICKY
        try {
            val url = serverUrl(requireNotNull(intent.getStringExtra("server")))
            val name = requireNotNull(intent.getStringExtra("name"))
            val ip = requireNotNull(intent.getStringExtra("ip"))
            val mode = CaptureMode.parse(requireNotNull(intent.getStringExtra("mode")))
            require(validPeerName(name) && validTailnetIp(ip)) { "Check the peer name and Tailscale IP." }
            getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel(CHANNEL, "Bragi audio service", NotificationManager.IMPORTANCE_LOW))
            val captureType = if (mode == CaptureMode.MICROPHONE) {
                if (Build.VERSION.SDK_INT >= 30) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0
            }
                              else ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
            mutableState.value = State(running = true, connection = "Starting…")
            startForeground(NOTIFICATION, notification(), captureType or ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK)
            wakeLock = getSystemService(PowerManager::class.java).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "Bragi:audio").also { it.setReferenceCounted(false); it.acquire(10 * 60 * 1000L) }
            wakeRenewal = scope.launch {
                while (isActive) { delay(5 * 60 * 1000L); wakeLock?.acquire(10 * 60 * 1000L) }
            }
            if (mode == CaptureMode.DEVICE_AUDIO) {
                @Suppress("DEPRECATION")
                val permission = intent.getParcelableExtra<Intent>("projection")
                    ?: throw IllegalArgumentException("Open Bragi to grant audio capture permission.")
                projection = getSystemService(MediaProjectionManager::class.java).getMediaProjection(Activity.RESULT_OK, permission)
                projectionCallback = object : MediaProjection.Callback() {
                    override fun onStop() {
                        mutableState.value = State(error = "Device audio capture permission ended. Open Bragi to start again.")
                        stopSelf()
                    }
                }
                projection!!.registerCallback(projectionCallback!!, Handler(Looper.getMainLooper()))
            }
            connection = BragiConnection(url, name)
            session = scope.launch {
                try {
                    connection!!.register(ip, mode)
                    connection!!.run(
                        onConfig = { config ->
                            require(config.mode == mode) { "Audio source changed. Stop and restart Bragi on the phone." }
                            engine?.stop()
                            engine = AudioEngine(url.host, ip, config, mode, projection) { send, receive, error ->
                                if (destroyed) return@AudioEngine
                                mutableState.value = mutableState.value.copy(sending = send, receiving = receive,
                                    error = error ?: mutableState.value.error)
                                connection?.status(send, receive, error ?: mutableState.value.error)
                            }
                            mutableState.value = mutableState.value.copy(error = null)
                            engine!!.apply(config.sendEnabled, config.receiveEnabled)
                        },
                        onStreams = { send, receive ->
                            mutableState.value = mutableState.value.copy(error = null)
                            engine?.apply(send, receive)
                        },
                        onDisconnected = { withContext(NonCancellable) { engine?.stop() } },
                        onStatus = { status ->
                            if (destroyed) return@run
                            mutableState.value = mutableState.value.copy(connection = status)
                            updateNotification()
                        },
                    )
                } catch (e: CancellationException) { throw e }
                catch (e: Throwable) {
                    mutableState.value = mutableState.value.copy(error = e.message ?: "Audio service failed.")
                    withContext(Dispatchers.Main) { stopSelf() }
                }
            }
        } catch (e: Exception) {
            mutableState.value = State(error = e.message ?: "Could not start the audio service.")
            stopSelf()
        }
        return START_NOT_STICKY // Android must not recreate mic/projection capture without user interaction.
    }

    private fun notification(): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val stop = PendingIntent.getService(this, 1, Intent(this, BragiService::class.java).setAction(STOP), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        return NotificationCompat.Builder(this, CHANNEL).setSmallIcon(R.drawable.ic_bragi)
            .setContentTitle("Bragi audio bridge").setContentText(mutableState.value.connection)
            .setContentIntent(open).setOngoing(true).addAction(0, "Stop", stop).build()
    }

    private fun updateNotification() { getSystemService(NotificationManager::class.java).notify(NOTIFICATION, notification()) }

    override fun onDestroy() {
        destroyed = true
        wakeRenewal?.cancel()
        connection?.close()
        session?.cancel()
        val previous = mutableState.value
        mutableState.value = State(error = previous.error)
        scope.launch {
            // Wait for configuration callbacks to finish before stopping the final engine.
            try { withContext(NonCancellable) { session?.join(); engine?.stop() } }
            finally { scope.cancel() }
        }
        projectionCallback?.let { projection?.unregisterCallback(it) }
        projection?.stop()
        if (wakeLock?.isHeld == true) wakeLock?.release()
        stopForeground(STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }
}
