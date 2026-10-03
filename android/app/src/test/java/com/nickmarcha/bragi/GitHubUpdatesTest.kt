package com.nickmarcha.bragi

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class GitHubUpdatesTest {
    private fun release(tag: String, draft: Boolean = false, pre: Boolean = false,
                        asset: String = "BragiAndroid.apk", checksum: String = "a".repeat(64),
                        host: String = "github.com") = JSONObject().put("tag_name", tag)
        .put("draft", draft).put("prerelease", pre).put("assets", JSONArray().put(JSONObject()
            .put("name", asset).put("digest", "sha256:$checksum")
            .put("browser_download_url", "https://$host/NickMarcha/bragi/releases/download/$tag/$asset")))

    @Test fun only_stable_android_apks_are_candidates() {
        val releases = JSONArray().put(release("client-v9.0.0"))
            .put(release("android-v0.2.0", pre = true)).put(release("android-v0.3.0", draft = true))
            .put(release("android-v0.4.0", asset = "BragiClient.AppImage"))
            .put(release("android-v0.1.1"))
        val update = androidUpdate(releases, AppVersion(0, 1, 0))!!
        assertEquals(AppVersion(0, 1, 1), update.version)
        assertTrue(update.url.endsWith("/android-v0.1.1/BragiAndroid.apk"))
    }

    @Test fun highest_numeric_version_wins_and_old_versions_do_not_update() {
        val releases = JSONArray().put(release("android-v0.2.0")).put(release("android-v0.10.0"))
        assertEquals(AppVersion(0, 10, 0), androidUpdate(releases, AppVersion(0, 1, 0))!!.version)
        assertNull(androidUpdate(releases, AppVersion(0, 10, 0)))
    }

    @Test fun release_must_have_a_checksum_and_belong_to_this_repository() {
        val releases = JSONArray().put(release("android-v0.2.0", checksum = ""))
            .put(release("android-v0.3.0", host = "other.example"))
        assertNull(androidUpdate(releases, AppVersion(0, 1, 0)))
    }
}
