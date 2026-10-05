package com.nickmarcha.bragi

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class PeerConfigTest {
    private fun config() = JSONObject("""{
      "name":"fairphone", "ports":{"mic_source":10041,"mic_control":10043,
      "playback_source":10044,"playback_control":10046}, "fec":"disable",
      "sample_rate":44100,"channels":2,"capture_mode":"device_audio",
      "send_enabled":true,"receive_enabled":false
    }""")

    @Test fun ports_are_from_the_phone_perspective() {
        val peer = PeerConfig.parse(config())
        assertEquals(10044, peer.sendSourcePort)
        assertEquals(10046, peer.sendControlPort)
        assertEquals(10041, peer.receiveSourcePort)
        assertEquals(10043, peer.receiveControlPort)
        assertEquals(CaptureMode.DEVICE_AUDIO, peer.mode)
        assertTrue(peer.sendEnabled)
        assertFalse(peer.receiveEnabled)
    }

    @Test fun listen_only_phones_have_no_capture_source() {
        assertEquals(CaptureMode.NONE, PeerConfig.parse(config().put("capture_mode", "none")).mode)
    }

    @Test fun unsupported_server_formats_fail_before_audio_starts() {
        for (value in listOf(config().put("fec", "rs8m"), config().put("sample_rate", 48000),
                             config().put("channels", 1), config().put("capture_mode", "unknown"))) {
            assertThrows(IllegalArgumentException::class.java) { PeerConfig.parse(value) }
        }
    }

    @Test fun names_and_ips_cannot_inject_pipewire_configuration() {
        assertTrue(validPeerName("fairphone-5"))
        assertFalse(validPeerName("fairphone\""))
        assertFalse(validPeerName("../phone"))
        assertFalse(validPeerName("5phone"))
        assertTrue(validTailnetIp("100.98.253.67"))
        assertFalse(validTailnetIp("100.1.2.3"))
        assertFalse(validTailnetIp("100.98.256.1"))
        assertFalse(validTailnetIp("127.0.0.1"))
    }

    @Test fun server_urls_keep_reverse_proxy_paths_and_reject_cleartext() {
        assertEquals("https://example.org/bragi/api/peers/register", serverUrl("https://example.org/bragi/", false).endpoint("api/peers/register").toString())
        assertEquals("https://example.org/ws/peer/fairphone", serverUrl("https://example.org/", false).endpoint("ws/peer/fairphone").toString())
        assertThrows(IllegalArgumentException::class.java) { serverUrl("http://example.org", false) }
        assertThrows(IllegalArgumentException::class.java) { serverUrl("http://10.0.2.2:20080/", false) }
    }

    @Test fun debug_builds_reach_only_the_local_dev_server() {
        assertEquals("http://10.0.2.2:20080/api/peers/register", serverUrl("http://10.0.2.2:20080/", true).endpoint("api/peers/register").toString())
        assertThrows(IllegalArgumentException::class.java) { serverUrl("https://sagepi.tail08dfa.ts.net/", true) }
        assertTrue(validPeerIp("127.0.0.1", true))
        assertFalse(validPeerIp("100.98.253.67", true))
        assertFalse(validPeerIp("127.0.0.1", false))
        assertTrue(validPeerIp("100.98.253.67", false))
    }

    @Test fun microphone_samples_preserve_level_in_both_stereo_channels() {
        assertArrayEquals(floatArrayOf(-1f, -1f, 0f, 0f, .5f, .5f), stereoSamples(shortArrayOf(-32768, 0, 16384), 3, 1), 0f)
        assertArrayEquals(floatArrayOf(.5f, -.5f), stereoSamples(shortArrayOf(16384, -16384, 100), 2, 2), 0f)
    }
}
