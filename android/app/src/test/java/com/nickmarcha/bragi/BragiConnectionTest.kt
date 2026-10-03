package com.nickmarcha.bragi

import java.net.InetAddress
import java.net.Socket
import javax.net.SocketFactory
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.*
import org.junit.Test

class BragiConnectionTest {
    @Test fun closing_from_the_lifecycle_thread_releases_network_sockets_on_io() = runBlocking {
        val lifecycleThread = Thread.currentThread()
        val socketFactory = object : SocketFactory() {
            override fun createSocket(): Socket = object : Socket() {
                override fun close() {
                    // Model Android's main-thread network guard at the actual socket operation.
                    assertNotSame("Network socket closed on the lifecycle thread", lifecycleThread, Thread.currentThread())
                    super.close()
                }
            }
            override fun createSocket(host: String, port: Int): Socket = error("Use unconnected sockets")
            override fun createSocket(host: String, port: Int, local: InetAddress, localPort: Int): Socket = error("Use unconnected sockets")
            override fun createSocket(host: InetAddress, port: Int): Socket = error("Use unconnected sockets")
            override fun createSocket(host: InetAddress, port: Int, local: InetAddress, localPort: Int): Socket = error("Use unconnected sockets")
        }
        val http = OkHttpClient.Builder().socketFactory(socketFactory).build()
        val server = MockWebServer()
        server.start()
        try {
            server.enqueue(MockResponse().setBody("""{
                "name":"fairphone","ports":{"mic_source":10041,"mic_control":10043,
                "playback_source":10044,"playback_control":10046},"fec":"disable",
                "sample_rate":44100,"channels":2,"capture_mode":"microphone",
                "send_enabled":true,"receive_enabled":false
            }"""))
            val connection = BragiConnection(server.url("/"), "fairphone", http)
            connection.register("100.98.253.67", CaptureMode.MICROPHONE)
            assertEquals(1, http.connectionPool.connectionCount())
            // Service teardown starts on the lifecycle thread, with a live pooled connection.
            connection.close()
            assertEquals(0, http.connectionPool.connectionCount())
        } finally {
            withContext(Dispatchers.IO) { http.connectionPool.evictAll(); server.shutdown() }
        }
    }
}
