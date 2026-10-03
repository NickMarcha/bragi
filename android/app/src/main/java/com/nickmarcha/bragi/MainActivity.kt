package com.nickmarcha.bragi

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.ClipData
import android.content.ClipboardManager
import android.content.pm.PackageManager
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.view.View
import android.widget.*
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.lifecycleScope
import java.io.File
import java.net.NetworkInterface
import kotlinx.coroutines.launch
import kotlinx.coroutines.CancellationException

class MainActivity : ComponentActivity() {
    private lateinit var server: EditText
    private lateinit var name: EditText
    private lateinit var ip: EditText
    private val microphoneId = View.generateViewId()
    private val deviceAudioId = View.generateViewId()
    private lateinit var modes: RadioGroup
    private lateinit var start: Button
    private lateinit var stop: Button
    private lateinit var status: TextView
    private lateinit var updates: Button
    private var availableUpdate: AppUpdate? = null
    private var pendingInstall: File? = null
    private var checking = false
    private val updater = GitHubUpdates()
    private val preferences by lazy { getSharedPreferences("bragi", MODE_PRIVATE) }

    private val permissions = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) requestCapture()
        else status.text = "Microphone permission is required for Android audio capture."
    }
    private val projectionPermission = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        if (result.resultCode == Activity.RESULT_OK && result.data != null) startAudioService(result.data)
        else status.text = "Device audio capture was not approved."
    }
    private val installPermission = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
        pendingInstall?.let { if (packageManager.canRequestPackageInstalls()) install(it) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val panel = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(24), dp(24), dp(24), dp(24)) }
        val scroll = ScrollView(this).apply { addView(panel) }
        setContentView(scroll)
        ViewCompat.setOnApplyWindowInsetsListener(scroll) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        fun text(value: String, size: Float = 16f) = TextView(this).apply { text = value; textSize = size; panel.addView(this) }
        text("Bragi", 28f)
        text("Send phone audio to your network headset.")
        fun field(label: String, initial: String, type: Int): EditText {
            text(label)
            return EditText(this).apply { setText(initial); inputType = type; setSingleLine(); panel.addView(this) }
        }
        server = field("Bragi server", preferences.getString("server", "https://sagepi.tail08dfa.ts.net/")!!, InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI)
        val defaultName = Build.MODEL.lowercase().replace(Regex("[^a-z0-9]+"), "-").trim('-').take(35).let { if (it.firstOrNull()?.isLetter() == true) it else "phone-$it" }.take(40)
        name = field("Peer name", preferences.getString("name", defaultName)!!, InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS)
        ip = field("Phone Tailscale IPv4 address", preferences.getString("ip", tailnetAddress())!!, InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI)
        text("Keep Tailscale connected. Bragi assigns the audio ports.", 13f)
        text("Audio source", 18f)
        modes = RadioGroup(this).apply {
            addView(RadioButton(this@MainActivity).apply { id = microphoneId; text = "Phone microphone" })
            addView(RadioButton(this@MainActivity).apply { id = deviceAudioId; text = "Device audio" })
            check(if (preferences.getString("mode", "microphone") == "device_audio") deviceAudioId else microphoneId)
            panel.addView(this)
        }
        text("Device audio needs Android's capture approval each time you start. Apps can block capture; calls may not be available.", 13f)
        status = text("Stopped")
        start = Button(this).apply { text = "Start audio service"; panel.addView(this); setOnClickListener { prepareStart() } }
        stop = Button(this).apply { text = "Stop"; panel.addView(this); setOnClickListener { Diagnostics.record("Stop tapped in app"); this@MainActivity.stopService(Intent(this@MainActivity, BragiService::class.java)) } }
        Button(this).apply { text = "Open web UI"; panel.addView(this); setOnClickListener {
            runCatching { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(serverUrl(server.text.toString()).toString()))) }
                .onFailure { status.text = it.message }
        } }
        text("Start once here, then use the web UI to pause sending or listen to the headset microphone. Stop and restart to change the audio source.", 13f)
        text("Updates", 18f)
        text("Version ${BuildConfig.VERSION_NAME}", 13f)
        updates = Button(this).apply { text = "Check for updates"; panel.addView(this); setOnClickListener {
            if (availableUpdate == null) checkUpdates(true) else offerUpdate(availableUpdate!!)
        } }
        Button(this).apply { text = "Diagnostics"; panel.addView(this); setOnClickListener { showDiagnostics() } }
        lifecycleScope.launch {
            BragiService.state.collect { state ->
                val disabled = state.running
                start.isEnabled = !disabled
                stop.isEnabled = disabled
                server.isEnabled = !disabled; name.isEnabled = !disabled; ip.isEnabled = !disabled
                for (i in 0 until modes.childCount) modes.getChildAt(i).isEnabled = !disabled
                status.text = listOfNotNull(state.connection,
                    if (state.running) "${if (state.sending) "Sending" else "Sender paused"} · ${if (state.receiving) "Listening" else "Receiver paused"}" else null,
                    state.error).joinToString("\n")
            }
        }
        if (System.currentTimeMillis() - preferences.getLong("updateCheck", 0) >= 24 * 60 * 60 * 1000L) checkUpdates(false)
    }

    private fun showDiagnostics() {
        val logs = Diagnostics.read()
        val text = TextView(this).apply {
            this.text = logs
            textSize = 12f
            setTextIsSelectable(true)
            setPadding(dp(16), dp(16), dp(16), dp(16))
        }
        AlertDialog.Builder(this).setTitle("Diagnostics")
            .setView(ScrollView(this).apply { addView(text) })
            .setNeutralButton("Copy logs") { _, _ ->
                getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("Bragi diagnostics", logs))
                Toast.makeText(this, "Logs copied", Toast.LENGTH_SHORT).show()
            }.setPositiveButton("Close", null).show()
    }

    private fun mode() = if (modes.checkedRadioButtonId == deviceAudioId) CaptureMode.DEVICE_AUDIO else CaptureMode.MICROPHONE
    private fun prepareStart() {
        try {
            serverUrl(server.text.toString())
            require(validPeerName(name.text.toString())) { "Peer name must start with a letter and use lowercase letters, digits, or hyphens, up to 40 characters." }
            require(validTailnetIp(ip.text.toString())) { "Enter the phone's Tailscale IPv4 address (100.64.x.x through 100.127.x.x)." }
            preferences.edit().putString("server", server.text.toString()).putString("name", name.text.toString())
                .putString("ip", ip.text.toString()).putString("mode", mode().wireName).apply()
            val needed = mutableListOf<String>()
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) needed += Manifest.permission.RECORD_AUDIO
            if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) needed += Manifest.permission.POST_NOTIFICATIONS
            if (needed.isEmpty()) requestCapture() else permissions.launch(needed.toTypedArray())
        } catch (error: Exception) { status.text = error.message }
    }

    private fun requestCapture() {
        if (mode() == CaptureMode.DEVICE_AUDIO)
            projectionPermission.launch(getSystemService(MediaProjectionManager::class.java).createScreenCaptureIntent())
        else startAudioService(null)
    }

    private fun startAudioService(projection: Intent?) {
        try {
            val intent = Intent(this, BragiService::class.java).putExtra("server", server.text.toString())
                .putExtra("name", name.text.toString()).putExtra("ip", ip.text.toString()).putExtra("mode", mode().wireName)
            projection?.let { intent.putExtra("projection", it) }
            ContextCompat.startForegroundService(this, intent)
        } catch (error: Exception) { status.text = error.message }
    }

    private fun checkUpdates(manual: Boolean) {
        if (checking) return
        checking = true
        updates.isEnabled = false
        updates.text = "Checking…"
        lifecycleScope.launch {
            try {
                availableUpdate = updater.check()
                preferences.edit().putLong("updateCheck", System.currentTimeMillis()).apply()
                updates.text = availableUpdate?.let { "Update to ${it.version}" } ?: "Check for updates"
                if (manual) {
                    if (availableUpdate != null) offerUpdate(availableUpdate!!)
                    else AlertDialog.Builder(this@MainActivity).setMessage("No newer Android release is available.").setPositiveButton("OK", null).show()
                }
            } catch (error: CancellationException) { throw error }
            catch (error: Exception) {
                updates.text = "Check for updates"
                if (manual) AlertDialog.Builder(this@MainActivity).setMessage(error.message).setPositiveButton("OK", null).show()
            } finally { checking = false; updates.isEnabled = true }
        }
    }

    private fun offerUpdate(update: AppUpdate) {
        AlertDialog.Builder(this).setTitle("Bragi ${update.version}")
            .setMessage("Download this update from GitHub? Android will ask you to confirm installation.")
            .setNegativeButton("Later", null).setPositiveButton("Download") { _, _ ->
                updates.isEnabled = false; updates.text = "Downloading…"
                lifecycleScope.launch {
                    try { install(updater.download(this@MainActivity, update)) }
                    catch (error: CancellationException) { throw error }
                    catch (error: Exception) { AlertDialog.Builder(this@MainActivity).setMessage(error.message).setPositiveButton("OK", null).show() }
                    finally { updates.isEnabled = true; updates.text = "Update to ${update.version}" }
                }
            }.show()
    }

    private fun install(file: File) {
        pendingInstall = file
        if (!packageManager.canRequestPackageInstalls()) {
            installPermission.launch(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName")))
            return
        }
        val uri = FileProvider.getUriForFile(this, "$packageName.updates", file)
        startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION))
        pendingInstall = null
    }

    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()
    private fun tailnetAddress(): String = runCatching {
        NetworkInterface.getNetworkInterfaces().toList().flatMap { it.inetAddresses.toList() }
            .firstOrNull { validTailnetIp(it.hostAddress ?: "") }?.hostAddress ?: ""
    }.getOrDefault("")
}
