package com.nickmarcha.bragi

import java.net.URI
import java.net.URLDecoder

/**
 * A link from the dashboard's "Connect a device" (app/pairing.py):
 * bragi://connect?server=<Bragi URL>&code=<one-time code>. The phone only
 * needs the server; it finds its own name and Tailscale address, and
 * registering needs no key. The code is for the desktop tray app.
 */
data class ConnectLink(val server: String, val code: String?) {
    companion object {
        fun parse(text: String): ConnectLink? {
            val uri = runCatching { URI(text.trim()) }.getOrNull() ?: return null
            if (!uri.scheme.equals("bragi", ignoreCase = true) || !uri.host.equals("connect", ignoreCase = true)) return null
            val query = uri.rawQuery ?: return null
            val values = query.split('&').mapNotNull { part ->
                val key = part.substringBefore('=', "")
                if (key.isEmpty()) null
                else key to URLDecoder.decode(part.substringAfter('=', ""), "UTF-8")
            }.toMap()
            val server = values["server"]?.takeIf { it.isNotBlank() } ?: return null
            return ConnectLink(server, values["code"]?.takeIf { it.isNotBlank() })
        }
    }
}
