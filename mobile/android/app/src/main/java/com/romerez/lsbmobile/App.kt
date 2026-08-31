package com.romerez.lsbmobile

import android.app.Application
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.core.content.FileProvider
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.floatPreferencesKey
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.romerez.lsbmobile.audio.PcmDecoder
import com.romerez.lsbmobile.audio.PlayerPool
import com.romerez.lsbmobile.audio.WavWriter
import com.romerez.lsbmobile.data.ConfigStore
import com.romerez.lsbmobile.data.LibraryRepository
import com.romerez.lsbmobile.data.MediaImporter
import com.romerez.lsbmobile.data.SoundSlot
import com.romerez.lsbmobile.sync.SyncApplier
import com.romerez.lsbmobile.sync.WebDownloader
import com.romerez.lsbmobile.sync.WifiPuller
import com.romerez.lsbmobile.sync.ZipImporter
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonPrimitive
import java.io.File

private val Context.dataStore by preferencesDataStore(name = "settings")

/**
 * Single ViewModel for the whole Phase 1 app — manual DI, no framework
 * (mobile/README.md §7.1). Phone-local UI prefs (grid columns, master
 * volume) live in DataStore, NEVER in the synced config (§5.3).
 */
class AppViewModel(app: Application) : AndroidViewModel(app) {

    private val store = ConfigStore(app.filesDir)
    val repo = LibraryRepository(store, app.filesDir, viewModelScope)
    val players = PlayerPool(app, viewModelScope)
    private val applier = SyncApplier(app.filesDir, store)
    private val importer = ZipImporter(app.contentResolver, app.cacheDir, applier)
    private val wifiPuller = WifiPuller(app.cacheDir, app.filesDir, store, applier)
    val mediaImporter = MediaImporter(app.contentResolver, app.filesDir, app.cacheDir)
    private val webDownloader = WebDownloader(app, app.cacheDir)

    /** One-shot user-facing notices (shown as Toasts by the UI). */
    val toast = MutableStateFlow<String?>(null)
    fun consumeToast() { toast.value = null }

    // ---- UI state ----
    val currentTab = MutableStateFlow(0)
    val searchQuery = MutableStateFlow("")
    val groupFilter = MutableStateFlow<String?>(null)
    val gridColumns = MutableStateFlow(3)
    val masterVolumePct = MutableStateFlow(100)

    /** 💻/📱 master section. The phone DEFAULTS to its own "phone" section
     *  ("see only phone-related sounds, with the ability to see others"). */
    val activeSection = MutableStateFlow("phone")
    val importState = MutableStateFlow<ImportState>(ImportState.Idle)
    val wifiState = MutableStateFlow<WifiState>(WifiState.Idle)

    sealed interface ImportState {
        data object Idle : ImportState
        data class Running(val progress: ZipImporter.Progress) : ImportState
        data class Done(val summary: ZipImporter.Summary) : ImportState
        data class Failed(val message: String) : ImportState
    }

    sealed interface WifiState {
        data object Idle : WifiState
        data class Running(val progress: WifiPuller.Progress) : WifiState
        data class Done(val summary: WifiPuller.Summary) : WifiState
        data class Failed(val message: String) : WifiState
    }

    /** In-app APK update, fed by the manifest's `app` block after a scan. */
    val updateState = MutableStateFlow<UpdateState>(UpdateState.None)

    sealed interface UpdateState {
        data object None : UpdateState
        data class Available(val update: WifiPuller.AppUpdate) : UpdateState
        data class Downloading(val bytesDone: Long, val bytesTotal: Long) : UpdateState
        data class Failed(val message: String) : UpdateState
    }

    private val gridColumnsKey = intPreferencesKey("grid_columns")
    private val masterVolumeKey = floatPreferencesKey("master_volume")
    private val activeSectionKey = stringPreferencesKey("active_section")
    private var restoredTab = false

    init {
        viewModelScope.launch {
            val prefs = getApplication<Application>().dataStore.data.first()
            gridColumns.value = (prefs[gridColumnsKey] ?: 3).coerceIn(2, 6)
            val vol = (prefs[masterVolumeKey] ?: 1f).coerceIn(0f, 1.5f)
            masterVolumePct.value = (vol * 100).toInt()
            players.masterVolume = vol
            activeSection.value = prefs[activeSectionKey] ?: "phone"
        }
        viewModelScope.launch {
            repo.library.collect { lib ->
                if (lib == null) return@collect
                if (!restoredTab) {
                    restoredTab = true
                    currentTab.value = lib.config.currentTab
                } else {
                    // An import can shrink the tab list; an out-of-range
                    // index would render a blank board.
                    val max = (lib.config.tabs.size - 1).coerceAtLeast(0)
                    if (currentTab.value > max) currentTab.value = max
                }
            }
        }
    }

    // ---- playback ----

    fun playSlot(tabIndex: Int, slotIndex: Int, slot: SoundSlot) {
        val lib = repo.library.value ?: return
        val path = slot.filePath ?: return
        if (lib.playable[path] != true) return // dangling ref: visible, unplayable
        players.play(
            slotId = "t$tabIndex:$slotIndex",
            name = slot.name,
            file = repo.resolve(path),
            volume = slot.volume,
            speed = slot.speed,
            preservePitch = slot.preservePitch,
            loop = slot.loop,
            loopCount = slot.loopCount,
            loopDelaySeconds = slot.loopDelaySeconds,
        )
    }

    fun stopSlot(tabIndex: Int, slotIndex: Int) = players.stopSlot("t$tabIndex:$slotIndex")

    fun stopAll() = players.stopAll()

    // ---- prefs ----

    fun selectTab(index: Int) {
        currentTab.value = index
        // Jumping to a tab of the other section (via search) flips the view.
        repo.library.value?.config?.tabs?.getOrNull(index)?.let { tab ->
            if (tab.section != activeSection.value) {
                activeSection.value = tab.section
                persistSection(tab.section)
            }
        }
        repo.persistCurrentTab(index)
    }

    fun setActiveSection(section: String) {
        if (section == activeSection.value) return
        activeSection.value = section
        persistSection(section)
        // Land on a tab that's actually in this section.
        val tabs = repo.library.value?.config?.tabs ?: return
        if (tabs.getOrNull(currentTab.value)?.section != section) {
            val target = tabs.indexOfFirst { it.section == section }
            if (target >= 0) {
                currentTab.value = target
                repo.persistCurrentTab(target)
            }
        }
    }

    private fun persistSection(section: String) {
        viewModelScope.launch(Dispatchers.IO) {
            getApplication<Application>().dataStore.edit { it[activeSectionKey] = section }
        }
    }

    fun setGridColumns(columns: Int) {
        val c = columns.coerceIn(2, 6)
        gridColumns.value = c
        viewModelScope.launch(Dispatchers.IO) {
            getApplication<Application>().dataStore.edit { it[gridColumnsKey] = c }
        }
    }

    fun setMasterVolumePct(pct: Int) {
        val p = pct.coerceIn(0, 150)
        masterVolumePct.value = p
        players.masterVolume = p / 100f
        viewModelScope.launch(Dispatchers.IO) {
            getApplication<Application>().dataStore.edit { it[masterVolumeKey] = p / 100f }
        }
    }

    // ---- import ----

    fun importZip(uri: Uri) {
        if (importState.value is ImportState.Running) return
        viewModelScope.launch {
            try {
                val summary = importer.import(uri) { progress ->
                    importState.value = ImportState.Running(progress)
                }
                importState.value = ImportState.Done(summary)
            } catch (t: Throwable) {
                importState.value = ImportState.Failed(t.message ?: "Import failed.")
            }
            repo.reload()
        }
    }

    fun dismissImportResult() {
        if (importState.value !is ImportState.Running) importState.value = ImportState.Idle
    }

    // ---- Wi-Fi QR sync ----

    fun wifiSync(scannedUrl: String) {
        if (wifiState.value is WifiState.Running) return
        viewModelScope.launch {
            try {
                val summary = wifiPuller.pull(scannedUrl) { progress ->
                    wifiState.value = WifiState.Running(progress)
                }
                wifiState.value = WifiState.Done(summary)
                summary.appUpdate?.let { updateState.value = UpdateState.Available(it) }
            } catch (t: Throwable) {
                wifiState.value = WifiState.Failed(t.message ?: "Sync failed.")
            }
            repo.reload()
        }
    }

    fun downloadAppUpdate() {
        val available = updateState.value as? UpdateState.Available ?: return
        viewModelScope.launch {
            updateState.value = UpdateState.Downloading(0, available.update.apkSize)
            try {
                val dest = File(getApplication<Application>().cacheDir,
                    "update/LSB-Mobile.apk")
                val file = wifiPuller.downloadApk(available.update, dest) { done, total ->
                    updateState.value = UpdateState.Downloading(done, total)
                }
                launchInstaller(file)
                updateState.value = UpdateState.None
            } catch (t: Throwable) {
                updateState.value = UpdateState.Failed(t.message ?: "Update failed.")
            }
        }
    }

    private fun launchInstaller(file: File) {
        val app = getApplication<Application>()
        val uri = FileProvider.getUriForFile(app, "${app.packageName}.fileprovider", file)
        // Android's package installer takes over — same signature + package,
        // so it installs as an UPDATE and all app data survives.
        app.startActivity(
            Intent(Intent.ACTION_VIEW)
                .setDataAndType(uri, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or
                    Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }

    fun dismissUpdate() {
        if (updateState.value !is UpdateState.Downloading) {
            updateState.value = UpdateState.None
        }
    }

    // ---- Phase 3: build the library on the phone (§5.4: phone tabs only) ----

    private fun tabOrigin(tabIndex: Int): String? =
        repo.library.value?.config?.tabs?.getOrNull(tabIndex)?.origin

    fun isPhoneTab(tabIndex: Int): Boolean = tabOrigin(tabIndex) == "phone"

    fun createPhoneTab(name: String) {
        val trimmed = name.trim().ifEmpty { "Tab" }
        val index = repo.addPhoneTab(trimmed)
        if (index >= 0) {
            if (activeSection.value != "phone") {
                activeSection.value = "phone"
                persistSection("phone")
            }
            currentTab.value = index
            repo.persistCurrentTab(index)
        }
    }

    fun renamePhoneTab(tabIndex: Int, name: String) {
        if (!isPhoneTab(tabIndex)) return
        repo.renameTab(tabIndex, name.trim().ifEmpty { "Tab" })
    }

    fun deletePhoneTab(tabIndex: Int) {
        if (!isPhoneTab(tabIndex)) return
        repo.deleteTab(tabIndex)
        toast.value = "Tab deleted"
    }

    fun addSoundFromUri(tabIndex: Int, slotIndex: Int, uri: Uri) {
        if (!isPhoneTab(tabIndex)) {
            toast.value = "PC tabs are managed on the PC — add into a 📱 tab"
            return
        }
        viewModelScope.launch {
            try {
                val imported = mediaImporter.import(uri)
                repo.addSlot(tabIndex, slotIndex, imported.packPath, imported.displayName)
                toast.value = "Added: ${imported.displayName}"
            } catch (t: Throwable) {
                toast.value = t.message ?: "Could not add that file."
            }
        }
    }

    /** Audio shared from other apps ("Share to LSB Mobile"). */
    fun handleSharedUris(uris: List<Uri>) {
        if (uris.isEmpty()) return
        viewModelScope.launch {
            val tabs = repo.library.value?.config?.tabs ?: return@launch
            var target = tabs.indexOfFirst { it.origin == "phone" }
            if (target < 0) {
                target = repo.addPhoneTab("Shared")
                if (target < 0) return@launch
            }
            var added = 0
            for (uri in uris) {
                try {
                    val imported = mediaImporter.import(uri)
                    repo.addSlot(target, -1, imported.packPath, imported.displayName)
                    added++
                } catch (t: Throwable) {
                    toast.value = t.message ?: "One shared file failed."
                }
            }
            if (added > 0) {
                activeSection.value = "phone"
                persistSection("phone")
                currentTab.value = target
                toast.value = "Added $added shared sound" + if (added > 1) "s" else ""
            }
        }
    }

    fun deleteSlot(tabIndex: Int, slotIndex: Int) {
        if (!isPhoneTab(tabIndex)) {
            toast.value = "PC sounds can only be removed on the PC"
            return
        }
        players.stopSlot("t$tabIndex:$slotIndex")
        repo.deleteSlot(tabIndex, slotIndex)
        toast.value = "Sound removed"
    }

    // ---- web / YouTube download (Phase 3; §7.7 no-ffmpeg strategy) ----

    val webDlState = MutableStateFlow<WebDlState>(WebDlState.Idle)

    sealed interface WebDlState {
        data object Idle : WebDlState
        data class Running(val percent: Float, val line: String) : WebDlState
        data class Failed(val message: String) : WebDlState
    }

    fun webDownload(url: String) {
        if (webDlState.value is WebDlState.Running) return
        if (url.isBlank()) return
        viewModelScope.launch {
            webDlState.value = WebDlState.Running(0f, "Starting…")
            try {
                val file = webDownloader.download(url.trim()) { percent, line ->
                    webDlState.value = WebDlState.Running(percent, line.take(80))
                }
                val imported = mediaImporter.importLocalFile(
                    file, displayName = MediaImporter.sanitizeStem(file.nameWithoutExtension)
                )
                var target = if (isPhoneTab(currentTab.value)) currentTab.value else {
                    repo.library.value?.config?.tabs
                        ?.indexOfFirst { it.origin == "phone" } ?: -1
                }
                if (target < 0) target = repo.addPhoneTab("Web")
                if (target >= 0) {
                    repo.addSlot(target, -1, imported.packPath, imported.displayName)
                    toast.value = "Downloaded: ${imported.displayName}"
                }
                webDlState.value = WebDlState.Idle
            } catch (t: Throwable) {
                webDlState.value = WebDlState.Failed(t.message ?: "Download failed.")
            }
        }
    }

    fun dismissWebDlError() {
        if (webDlState.value !is WebDlState.Running) webDlState.value = WebDlState.Idle
    }

    // ---- trim editor ----

    fun previewTrim(pcm: PcmDecoder.Pcm, startFrame: Int, endFrame: Int) {
        viewModelScope.launch(Dispatchers.IO) {
            val f = File(getApplication<Application>().cacheDir, "trim_preview.wav")
            WavWriter.write(f, pcm.samples, startFrame * pcm.channels,
                endFrame * pcm.channels, pcm.channels, pcm.sampleRate)
            withContext(Dispatchers.Main) {
                players.stopSlot("trim:preview")
                players.play("trim:preview", "✂ Preview", f, 1f, 1f,
                    preservePitch = true, loop = false, loopCount = 0,
                    loopDelaySeconds = 0f)
            }
        }
    }

    fun stopTrimPreview() = players.stopSlot("trim:preview")

    /**
     * Save a cut as a NEW wav (never destructive — desktop convention):
     * `{stem}_{timestamp8}.wav`, `source_file_path` pointing at the
     * original so re-trims stay possible. Cuts land in the source tab when
     * it's phone-owned, else in the first phone tab (created as "Cuts").
     */
    fun saveTrimmedCut(
        sourceTabIndex: Int, sourceSlot: SoundSlot, name: String,
        pcm: PcmDecoder.Pcm, startFrame: Int, endFrame: Int,
    ) {
        viewModelScope.launch {
            try {
                val stem = MediaImporter.sanitizeStem(name.trim().ifEmpty { sourceSlot.name })
                val ts8 = System.currentTimeMillis().toString().takeLast(8)
                val packPath = "sounds/${stem}_$ts8.wav"
                withContext(Dispatchers.IO) {
                    WavWriter.write(repo.resolve(packPath), pcm.samples,
                        startFrame * pcm.channels, endFrame * pcm.channels,
                        pcm.channels, pcm.sampleRate)
                }
                var target = if (isPhoneTab(sourceTabIndex)) sourceTabIndex else {
                    repo.library.value?.config?.tabs
                        ?.indexOfFirst { it.origin == "phone" } ?: -1
                }
                if (target < 0) target = repo.addPhoneTab("Cuts")
                if (target >= 0) {
                    repo.addSlot(
                        target, -1, packPath, stem,
                        sourcePackPath = sourceSlot.sourceFilePath ?: sourceSlot.filePath,
                    )
                    toast.value = "Saved: $stem"
                }
            } catch (t: Throwable) {
                toast.value = t.message ?: "Could not save the cut."
            }
        }
    }

    // ---- slot editing (basic, Phase-3 slice) ----

    fun editSlot(
        tabIndex: Int, slotIndex: Int, name: String, volume: Float, speed: Float,
        preservePitch: Boolean, loop: Boolean, loopCount: Int, loopDelay: Float,
    ) {
        repo.updateSlot(tabIndex, slotIndex, mapOf(
            "name" to JsonPrimitive(name),
            "volume" to JsonPrimitive(volume),
            "speed" to JsonPrimitive(speed),
            "preserve_pitch" to JsonPrimitive(preservePitch),
            "loop" to JsonPrimitive(loop),
            "loop_count" to JsonPrimitive(loopCount),
            "loop_delay" to JsonPrimitive(loopDelay),
        ))
    }

    fun wifiFailed(message: String) {
        if (wifiState.value !is WifiState.Running) wifiState.value = WifiState.Failed(message)
    }

    fun dismissWifiResult() {
        if (wifiState.value !is WifiState.Running) wifiState.value = WifiState.Idle
    }

    override fun onCleared() {
        players.release()
    }
}
