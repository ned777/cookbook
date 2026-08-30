package com.cookbook.app

/**
 * Points straight at the Cookbook server — this app has no data of its
 * own. Same pattern as Vehicle Maintenance Record: credentials are baked
 * in so there's never a login screen, and the same header is attached to
 * every WebView request so the New Recipe form's POST authenticates
 * transparently too.
 *
 * The actual values come from BuildConfig, generated at build time from
 * secrets.properties (gitignored — see secrets.properties.example) rather
 * than being hardcoded here, so the server address and password never end
 * up in source control.
 */
object Config {
    const val BASE_URL = BuildConfig.COOKBOOK_BASE_URL
    const val USERNAME = BuildConfig.COOKBOOK_USERNAME
    const val PASSWORD = BuildConfig.COOKBOOK_PASSWORD
}
