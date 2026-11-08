package com.openrubi.lucimatools

import java.net.HttpURLConnection
import java.net.URL

/** Address returned by our Python bind; never inferred from another listener. */
data class LocalServerEndpoint(val port: Int, val instanceId: String) {
    init {
        require(port in 1..65535)
        require(instanceId.isNotBlank())
    }

    val url: String get() = "http://127.0.0.1:$port/"

    fun isReady(): Boolean = try {
        val connection = URL(url + "api/health").openConnection() as HttpURLConnection
        try {
            connection.connectTimeout = 500
            connection.readTimeout = 500
            connection.instanceFollowRedirects = false
            connection.useCaches = false
            connection.requestMethod = "GET"
            connection.responseCode == 200 && connection.getHeaderField("X-Lucima-Instance") == instanceId
        } finally {
            connection.disconnect()
        }
    } catch (_: Exception) {
        false
    }
}
