package com.nickmarcha.bragi

import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

class BragiConnection(private val base: HttpUrl, private val name: String) {
    private val http = OkHttpClient.Builder().connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS).pingInterval(20, TimeUnit.SECONDS).build()
    @Volatile private var socket: WebSocket? = null

    suspend fun register(ip: String, mode: CaptureMode): PeerConfig = withContext(Dispatchers.IO) {
        val payload = JSONObject().put("name", name).put("tailscale_ip", ip).put("capture_mode", mode.wireName)
        val request = Request.Builder().url(base.endpoint("api/peers/register"))
            .post(payload.toString().toRequestBody("application/json".toMediaType())).build()
        http.newCall(request).execute().use { response ->
            val body = response.body?.string() ?: throw IOException("Bragi returned an empty response.")
            if (!response.isSuccessful) throw IOException("Registration failed (${response.code}): ${JSONObject(body).optString("detail", body)}")
            PeerConfig.parse(JSONObject(body))
        }
    }

    suspend fun setStreams(send: Boolean, receive: Boolean) = withContext(Dispatchers.IO) {
        val body = JSONObject().put("send_enabled", send).put("receive_enabled", receive)
        val request = Request.Builder().url(base.endpoint("api/peers/$name/streams"))
            .post(body.toString().toRequestBody("application/json".toMediaType())).build()
        http.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw IOException("Stream control failed (${response.code}).")
        }
    }

    private sealed interface Event {
        data class Message(val text: String) : Event
        data class Closed(val reason: String, val terminal: Boolean = false) : Event
    }

    suspend fun run(onConfig: suspend (PeerConfig) -> Unit,
                    onStreams: suspend (Boolean, Boolean) -> Unit,
                    onDisconnected: suspend () -> Unit,
                    onStatus: (String) -> Unit) {
        var retry = 1L
        while (currentCoroutineContext().isActive) {
            val events = Channel<Event>(Channel.UNLIMITED)
            try {
                onStatus("Connecting to Bragi…")
                val request = Request.Builder().url(base.endpoint("ws/peer/$name").newBuilder().addQueryParameter("client", "android").build()).build()
                socket = http.newWebSocket(request, object : WebSocketListener() {
                    override fun onMessage(webSocket: WebSocket, text: String) { events.trySend(Event.Message(text)) }
                    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                        events.trySend(Event.Closed(t.message ?: "Connection failed", response?.code == 403))
                    }
                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(code, reason)
                        events.trySend(Event.Closed(reason, code == 1008))
                    }
                })
                val first = withTimeoutOrNull(15_000) { events.receive() } ?: throw IOException("Timed out waiting for Bragi configuration.")
                if (first is Event.Closed && first.terminal) throw PeerRemovedException(first.reason)
                if (first !is Event.Message) throw IOException((first as Event.Closed).reason)
                val initial = JSONObject(first.text)
                require(initial.getString("type") == "peer_config") { "Bragi did not supply peer configuration." }
                val config = PeerConfig.parse(initial)
                require(config.name == name) { "Bragi supplied another device's configuration." }
                onConfig(config)
                onStatus("Connected")
                retry = 1
                while (currentCoroutineContext().isActive) {
                    when (val event = events.receive()) {
                        is Event.Closed -> if (event.terminal) throw PeerRemovedException(event.reason) else throw IOException(event.reason)
                        is Event.Message -> {
                            val message = JSONObject(event.text)
                            if (message.optString("type") == "peer_streams")
                                onStreams(message.getBoolean("send_enabled"), message.getBoolean("receive_enabled"))
                        }
                    }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: PeerRemovedException) {
                throw e
            } catch (e: Exception) {
                onStatus("Disconnected: ${e.message}. Retrying…")
            } finally {
                socket?.cancel()
                socket = null
                events.close()
                onDisconnected()
            }
            delay(retry * 1000)
            retry = (retry * 2).coerceAtMost(30)
        }
    }

    fun status(send: Boolean, receive: Boolean, error: String?) {
        socket?.send(JSONObject().put("type", "peer_status").put("send_active", send)
            .put("receive_active", receive).put("error", error ?: JSONObject.NULL).toString())
    }

    fun close() { socket?.cancel(); http.dispatcher.cancelAll(); http.connectionPool.evictAll() }
}

class PeerRemovedException(message: String) : IOException(message)
