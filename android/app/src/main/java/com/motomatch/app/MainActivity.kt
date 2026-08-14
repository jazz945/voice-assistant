package com.motomatch.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.GeolocationPermissions
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat

/**
 * Coque native autour du client web de MotoMatch.
 *
 * L'application n'embarque pas l'interface : elle affiche celle servie par le
 * serveur de l'utilisateur. C'est délibéré — MotoMatch s'auto-héberge, il n'y a
 * pas d'adresse unique à figer dans le binaire. L'adresse est donc demandée au
 * premier lancement puis mémorisée.
 *
 * Ce que cette coque apporte par rapport au navigateur : une icône de lanceur,
 * le plein écran, et surtout le pont de permission de géolocalisation, sans
 * lequel la fonction « croisements » ne reçoit aucune position.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var setupPanel: View
    private lateinit var serverInput: EditText

    private var pendingGeolocationOrigin: String? = null
    private var pendingGeolocationCallback: GeolocationPermissions.Callback? = null

    private val locationPermissionRequest =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { granted ->
            val allowed = granted[Manifest.permission.ACCESS_FINE_LOCATION] == true ||
                granted[Manifest.permission.ACCESS_COARSE_LOCATION] == true
            // La page attend une réponse : ne jamais laisser le rappel en suspens,
            // sinon la demande de position reste bloquée jusqu'au redémarrage.
            pendingGeolocationCallback?.invoke(pendingGeolocationOrigin, allowed, false)
            pendingGeolocationCallback = null
            pendingGeolocationOrigin = null
            if (!allowed) {
                Toast.makeText(this, R.string.location_denied, Toast.LENGTH_LONG).show()
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.web_view)
        setupPanel = findViewById(R.id.setup_panel)
        serverInput = findViewById(R.id.server_url)

        configureWebView()
        wireSetupPanel()
        handleBackNavigation()

        val saved = savedServerUrl()
        if (saved == null) showSetup() else load(saved)
    }

    // --- Adresse du serveur -------------------------------------------------

    private fun preferences() =
        getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    private fun savedServerUrl(): String? = preferences().getString(KEY_SERVER_URL, null)

    private fun wireSetupPanel() {
        findViewById<Button>(R.id.connect_button).setOnClickListener {
            val url = normaliseUrl(serverInput.text.toString())
            if (url == null) {
                serverInput.error = getString(R.string.invalid_url)
                return@setOnClickListener
            }
            preferences().edit().putString(KEY_SERVER_URL, url).apply()
            load(url)
        }
        findViewById<TextView>(R.id.setup_hint).text = getString(R.string.setup_hint)
    }

    /**
     * Complète et valide l'adresse saisie.
     *
     * `https` est ajouté par défaut : sur un réseau public, laisser passer un
     * `http` implicite enverrait les jetons de session en clair.
     */
    private fun normaliseUrl(raw: String): String? {
        val trimmed = raw.trim().trimEnd('/')
        if (trimmed.isEmpty()) return null
        val withScheme =
            if (trimmed.startsWith("http://") || trimmed.startsWith("https://")) trimmed
            else "https://$trimmed"
        val parsed = Uri.parse(withScheme)
        return if (parsed.host.isNullOrBlank()) null else withScheme
    }

    private fun showSetup() {
        setupPanel.visibility = View.VISIBLE
        webView.visibility = View.GONE
        serverInput.setText(savedServerUrl() ?: "")
    }

    private fun load(url: String) {
        setupPanel.visibility = View.GONE
        webView.visibility = View.VISIBLE
        webView.loadUrl(url)
    }

    // --- WebView ------------------------------------------------------------

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView() {
        webView.settings.apply {
            javaScriptEnabled = true          // l'interface est une application monopage
            domStorageEnabled = true          // les jetons vivent dans localStorage
            setGeolocationEnabled(true)
            cacheMode = WebSettings.LOAD_DEFAULT
            // Aucune raison de lire des fichiers locaux : l'interface vient du réseau.
            allowFileAccess = false
            allowContentAccess = false
            mediaPlaybackRequiresUserGesture = true
        }

        webView.webViewClient = object : WebViewClient() {
            /**
             * Garde la navigation dans l'application, et seulement pour le
             * serveur configuré : tout lien externe part vers le navigateur,
             * pour qu'une page tierce ne s'affiche jamais sous notre identité.
             */
            override fun shouldOverrideUrlLoading(
                view: WebView,
                request: WebResourceRequest,
            ): Boolean {
                val configured = savedServerUrl()?.let { Uri.parse(it).host }
                if (configured != null && request.url.host == configured) return false
                startActivity(android.content.Intent(android.content.Intent.ACTION_VIEW, request.url))
                return true
            }

            override fun onReceivedError(
                view: WebView,
                request: WebResourceRequest,
                error: android.webkit.WebResourceError,
            ) {
                if (request.isForMainFrame) {
                    Toast.makeText(this@MainActivity, R.string.connection_failed, Toast.LENGTH_LONG)
                        .show()
                    showSetup()
                }
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            /**
             * Pont de géolocalisation.
             *
             * La page demande une position ; Android exige d'abord une
             * permission d'application. On enchaîne les deux, puis on rend la
             * réponse à la page.
             */
            override fun onGeolocationPermissionsShowPrompt(
                origin: String,
                callback: GeolocationPermissions.Callback,
            ) {
                if (hasLocationPermission()) {
                    callback.invoke(origin, true, false)
                    return
                }
                pendingGeolocationOrigin = origin
                pendingGeolocationCallback = callback
                locationPermissionRequest.launch(
                    arrayOf(
                        Manifest.permission.ACCESS_FINE_LOCATION,
                        Manifest.permission.ACCESS_COARSE_LOCATION,
                    )
                )
            }
        }
    }

    private fun hasLocationPermission(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED ||
            ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    /** Le bouton retour remonte l'historique web avant de quitter l'application. */
    private fun handleBackNavigation() {
        onBackPressedDispatcher.addCallback(
            this,
            object : OnBackPressedCallback(true) {
                override fun handleOnBackPressed() {
                    if (webView.visibility == View.VISIBLE && webView.canGoBack()) {
                        webView.goBack()
                    } else {
                        isEnabled = false
                        onBackPressedDispatcher.onBackPressed()
                    }
                }
            },
        )
    }

    companion object {
        private const val PREFERENCES = "motomatch"
        private const val KEY_SERVER_URL = "server_url"
    }
}
