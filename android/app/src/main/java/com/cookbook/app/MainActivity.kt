package com.cookbook.app

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.Menu
import android.view.MenuItem
import android.view.WindowManager
import android.webkit.HttpAuthHandler
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import com.cookbook.app.databinding.ActivityMainBinding

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

    // Tracks which server URL is currently loaded so onResume() can tell a
    // return from the Settings screen actually changed something (vs. just
    // switching apps and back) before reloading the WebView from scratch.
    private var loadedBaseUrl: String? = null

    // The New/Edit recipe forms' <input type=file accept=image/*> otherwise
    // does nothing in a WebView — Chrome and every other real browser have
    // a built-in file picker, but WebView only gets one if the app supplies
    // it here. GetContent() covers both "choose from gallery" and, on
    // phones that route it there, "take a photo" — whichever the system
    // picker itself offers for an image MIME type.
    private var filePathCallback: ValueCallback<Array<Uri>>? = null
    private val pickImageLauncher = registerForActivityResult(ActivityResultContracts.GetContent()) { uri: Uri? ->
        filePathCallback?.onReceiveValue(uri?.let { arrayOf(it) })
        filePathCallback = null
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

        if (!Config.isConfigured(this)) {
            startActivity(Intent(this, SetupActivity::class.java).putExtra(SetupActivity.EXTRA_FIRST_RUN, true))
            finish()
            return
        }

        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)
        supportActionBar?.title = "My Kitchen"
        supportActionBar?.subtitle = "by Ned Nguyen"

        binding.webView.settings.javaScriptEnabled = true
        binding.webView.settings.domStorageEnabled = true
        binding.webView.settings.cacheMode = WebSettings.LOAD_NO_CACHE
        // Pinch-to-zoom, no on-screen +/- controls — mainly for the recipe
        // photo lightbox, where native zoom is how you actually see detail.
        binding.webView.settings.setSupportZoom(true)
        binding.webView.settings.builtInZoomControls = true
        binding.webView.settings.displayZoomControls = false

        binding.webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                webView: WebView?,
                callback: ValueCallback<Array<Uri>>,
                fileChooserParams: FileChooserParams?
            ): Boolean {
                filePathCallback?.onReceiveValue(null)
                filePathCallback = callback
                pickImageLauncher.launch("image/*")
                return true
            }
        }

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
                handler.proceed(Config.username(this@MainActivity), Config.password(this@MainActivity))
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

        loadServer()

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

    private fun loadServer() {
        val url = Config.baseUrl(this) ?: return
        loadedBaseUrl = url
        binding.webView.loadUrl(url)
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main_menu, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        if (item.itemId == R.id.action_settings) {
            startActivity(Intent(this, SetupActivity::class.java))
            return true
        }
        return super.onOptionsItemSelected(item)
    }

    override fun onResume() {
        super.onResume()
        // Coming back from the Settings screen with a changed server/login
        // — reload against the new one instead of leaving the old page up.
        if (::binding.isInitialized && Config.baseUrl(this) != loadedBaseUrl) {
            loadServer()
        }
    }

    override fun onDestroy() {
        if (::binding.isInitialized) {
            binding.webView.destroy()
        }
        super.onDestroy()
    }
}
