package com.cookbook.app

import android.content.Context

/**
 * Server address and Basic Auth login, entered once through SetupActivity
 * and kept in SharedPreferences rather than baked into the APK at build
 * time. That means a phone that lost this app (new device, wiped phone,
 * the machine that built the old APK is gone) just gets asked again on
 * first launch instead of needing a rebuild from a gitignored
 * secrets.properties file that may no longer exist anywhere.
 *
 * `default*()` still reads the old BuildConfig fields (populated from
 * secrets.properties when present) purely to pre-fill the Setup screen on
 * a build from a machine that already has them — a fresh checkout with no
 * secrets.properties falls back to the obvious placeholders, which are
 * filtered out here so a first run always prompts instead of silently
 * pointing at "localhost".
 */
object Config {
    private const val PREFS_NAME = "cookbook_config"
    private const val KEY_BASE_URL = "base_url"
    private const val KEY_USERNAME = "username"
    private const val KEY_PASSWORD = "password"

    private fun prefs(context: Context) =
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun isConfigured(context: Context): Boolean =
        !baseUrl(context).isNullOrBlank() && username(context).isNotBlank() && password(context).isNotBlank()

    fun baseUrl(context: Context): String? = prefs(context).getString(KEY_BASE_URL, null)
    fun username(context: Context): String = prefs(context).getString(KEY_USERNAME, "") ?: ""
    fun password(context: Context): String = prefs(context).getString(KEY_PASSWORD, "") ?: ""

    fun save(context: Context, baseUrl: String, username: String, password: String) {
        prefs(context).edit()
            .putString(KEY_BASE_URL, baseUrl)
            .putString(KEY_USERNAME, username)
            .putString(KEY_PASSWORD, password)
            .apply()
    }

    fun defaultBaseUrl(): String? = BuildConfig.COOKBOOK_BASE_URL.takeIf { it.isNotBlank() && it != "http://localhost:8092" }
    fun defaultUsername(): String? = BuildConfig.COOKBOOK_USERNAME.takeIf { it.isNotBlank() && it != "admin" }
    fun defaultPassword(): String? = BuildConfig.COOKBOOK_PASSWORD.takeIf { it.isNotBlank() && it != "changeme" }
}
