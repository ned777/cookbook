package com.cookbook.app

import android.content.Intent
import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import com.cookbook.app.databinding.ActivitySetupBinding

/**
 * First-run screen (and, later, the "Server Settings" reset path reachable
 * from MainActivity's overflow menu): asks for the server address plus the
 * Basic Auth username/password. This doesn't create an account on the
 * server — those credentials must already exist in its local_secrets.py —
 * it just tells this install of the app where to find it and how to log
 * in, so losing/replacing the phone or the machine that built the APK
 * never requires touching source code again.
 *
 * Launched two ways, distinguished by EXTRA_FIRST_RUN: on first run (no
 * saved config yet) MainActivity sends here and finishes itself, so Save
 * must start a fresh MainActivity. Reopened later from the settings menu,
 * MainActivity stays on the back stack, so Save (or Cancel) just finishes
 * back to it — its onResume() notices the config changed and reloads.
 */
class SetupActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_FIRST_RUN = "first_run"
    }

    private lateinit var binding: ActivitySetupBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySetupBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val firstRun = intent.getBooleanExtra(EXTRA_FIRST_RUN, false)

        val savedUrl = Config.baseUrl(this)
        binding.serverInput.setText(
            (savedUrl ?: Config.defaultBaseUrl())?.removePrefix("http://")?.removePrefix("https://") ?: ""
        )
        binding.usernameInput.setText(
            if (savedUrl != null) Config.username(this) else Config.defaultUsername() ?: ""
        )
        binding.passwordInput.setText(
            if (savedUrl != null) Config.password(this) else Config.defaultPassword() ?: ""
        )

        if (!firstRun) {
            binding.cancelButton.visibility = View.VISIBLE
            binding.cancelButton.setOnClickListener { finish() }
        }

        binding.saveButton.setOnClickListener {
            val server = binding.serverInput.text.toString().trim()
            val username = binding.usernameInput.text.toString().trim()
            val password = binding.passwordInput.text.toString()

            if (server.isEmpty() || username.isEmpty() || password.isEmpty()) {
                binding.errorText.text = "All three fields are required."
                binding.errorText.visibility = View.VISIBLE
                return@setOnClickListener
            }

            val baseUrl = if (server.startsWith("http://") || server.startsWith("https://")) {
                server
            } else {
                "http://$server"
            }

            Config.save(this, baseUrl, username, password)

            if (firstRun) {
                startActivity(Intent(this, MainActivity::class.java))
            }
            finish()
        }
    }
}
