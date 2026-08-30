package com.cookbook.app

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.WindowManager
import android.webkit.HttpAuthHandler
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import com.cookbook.app.databinding.ActivityMainBinding
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The whole app is this one screen: a WebView pointed at the Cookbook
 * server. Every "screen" after that — recipe list, a recipe's overview,
 * the New Recipe form, the step-by-step cooking deck — is just a page (or,
 * for the cooking deck, client-side JS) the server itself renders. No
 * offline cache and no local data: this is a browser, not a copy of the
 * recipes.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding

    // "HH" is always 24-hour in SimpleDateFormat, regardless of locale.
    private val clockFormat = SimpleDateFormat("EEEE MMM d - HH:mm:ss", Locale.getDefault())
    private val clockHandler = Handler(Looper.getMainLooper())
    private val clockTick = object : Runnable {
        override fun run() {
            supportActionBar?.title = clockFormat.format(Date()).uppercase(Locale.getDefault())
            clockHandler.postDelayed(this, 1_000)
        }
    }

    // The cooking-mode deck (any /step/ URL) is meant to sit propped up on a
    // counter while your hands are busy — the screen timing out mid-recipe
    // defeats the point. The page itself best-effort-requests a JS Wake
    // Lock too, but that API only actually works over HTTPS/localhost, so
    // it silently no-ops on this app's plain-HTTP LAN server; this native
    // flag is what actually keeps the screen on in practice. Cleared again
    // the moment you navigate anywhere else (recipe list, overview, a real
    // page load — not the in-deck swipes, which never leave this URL).
    private fun setKeepScreenOn(url: String?) {
        if (url != null && url.contains("/step/")) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)

        binding.webView.settings.javaScriptEnabled = true
        binding.webView.settings.domStorageEnabled = true
        binding.webView.settings.cacheMode = WebSettings.LOAD_NO_CACHE

        binding.webView.webViewClient = object : WebViewClient() {
            // The server basic-auth-protects every request. Auto-answering
            // here means the app never shows a login prompt, for the
            // recipe list or any page/form you navigate to from it.
            override fun onReceivedHttpAuthRequest(
                view: WebView,
                handler: HttpAuthHandler,
                host: String,
                realm: String
            ) {
                handler.proceed(Config.USERNAME, Config.PASSWORD)
            }

            override fun onPageStarted(view: WebView, url: String?, favicon: android.graphics.Bitmap?) {
                binding.loadingBar.visibility = android.view.View.VISIBLE
                setKeepScreenOn(url)
            }

            override fun onPageFinished(view: WebView, url: String?) {
                binding.loadingBar.visibility = android.view.View.GONE
                binding.swipeRefresh.isRefreshing = false
                setKeepScreenOn(url)
            }
        }

        binding.swipeRefresh.setOnRefreshListener { binding.webView.reload() }

        binding.webView.loadUrl(Config.BASE_URL)

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (binding.webView.canGoBack()) {
                    binding.webView.goBack()
                } else {
                    isEnabled = false
                    onBackPressedDispatcher.onBackPressed()
                    isEnabled = true
                }
            }
        })
    }

    override fun onResume() {
        super.onResume()
        clockHandler.post(clockTick)
    }

    override fun onPause() {
        clockHandler.removeCallbacks(clockTick)
        super.onPause()
    }

    override fun onDestroy() {
        binding.webView.destroy()
        super.onDestroy()
    }
}
