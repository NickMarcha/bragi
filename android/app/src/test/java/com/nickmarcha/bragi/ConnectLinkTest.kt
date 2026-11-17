package com.nickmarcha.bragi

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ConnectLinkTest {
    @Test
    fun readsTheServerAndCodeFromTheDashboardsLink() {
        val link = ConnectLink.parse("bragi://connect?server=https%3A%2F%2Fbragi.example.ts.net%2F&code=K7F2-9QXM")
        assertEquals(ConnectLink("https://bragi.example.ts.net/", "K7F2-9QXM"), link)
    }

    @Test
    fun theCodeIsOptional() {
        assertEquals(ConnectLink("https://bragi.example.ts.net/", null),
            ConnectLink.parse(" BRAGI://connect?server=https%3A%2F%2Fbragi.example.ts.net%2F "))
    }

    @Test
    fun anythingElseIsNotALink() {
        assertNull(ConnectLink.parse("https://bragi.example.ts.net/"))
        assertNull(ConnectLink.parse("bragi://other?server=x"))
        assertNull(ConnectLink.parse("bragi://connect?code=K7F2-9QXM"))
        assertNull(ConnectLink.parse("not a link at all"))
    }
}
