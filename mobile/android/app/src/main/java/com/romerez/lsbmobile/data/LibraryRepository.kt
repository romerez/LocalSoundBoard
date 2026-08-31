package com.romerez.lsbmobile.data

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import java.io.File

/**
 * In-memory library state: the parsed config plus a file-existence map so
 * dangling references (2 exist in the real desktop library — §5.2 rule 3)
 * render as visible-but-unplayable tiles without any I/O on the UI thread.
 */
class LibraryRepository(
    private val store: ConfigStore,
    private val filesDir: File,
    private val scope: CoroutineScope,
) {

    data class Library(
        val config: LibraryConfig,
        /** normalized pack path → file exists on disk */
        val playable: Map<String, Boolean>,
    )

    private val _library = MutableStateFlow<Library?>(null)
    val library: StateFlow<Library?> = _library

    private val _loadFailed = MutableStateFlow(false)
    val loadFailed: StateFlow<Boolean> = _loadFailed

    init {
        scope.launch(Dispatchers.IO) { reload() }
    }

    fun resolve(packPath: String): File = File(filesDir, packPath)

    suspend fun reload() = withContext(Dispatchers.IO) {
        val root = store.load()
        _loadFailed.value = !store.loadedOk
        if (root == null) {
            _library.value = null
            return@withContext
        }
        val config = LibraryConfig(root)
        _library.value = Library(config, buildPlayableMap(config))
    }

    private fun buildPlayableMap(config: LibraryConfig): Map<String, Boolean> = buildMap {
        for (tab in config.tabs) {
            for (slot in tab.slots.values) {
                val p = slot.filePath ?: continue
                if (!containsKey(p)) put(p, File(filesDir, p).isFile)
            }
        }
    }

    /** FIFO lane so concurrent mutations can never invert save order. */
    @kotlinx.coroutines.ExperimentalCoroutinesApi
    private val saveLane = Dispatchers.IO.limitedParallelism(1)

    /**
     * Single mutation path. The new state publishes SYNCHRONOUSLY (so
     * back-to-back mutations — create tab, then add into it — see each
     * other); the playable-map refresh and the disk save follow on a
     * single-lane dispatcher, guarded against clobbering newer state.
     */
    @kotlinx.coroutines.ExperimentalCoroutinesApi
    private fun mutateConfig(transform: (JsonObject) -> JsonObject?) {
        val lib = _library.value ?: return
        val newRoot = transform(lib.config.raw) ?: return
        val config = LibraryConfig(newRoot)
        _library.value = Library(config, lib.playable) // stale playable, refreshed below
        scope.launch(saveLane) {
            val playable = buildPlayableMap(config)
            // Only touch state if no NEWER mutation published meanwhile.
            if (_library.value?.config === config) {
                _library.value = Library(config, playable)
            }
            store.save(newRoot)
        }
    }

    /** Delete files not referenced by `config`, dropping sync-index entries. */
    private fun refcountDelete(config: JsonObject, candidatePaths: Set<String>) {
        if (candidatePaths.isEmpty()) return
        scope.launch(Dispatchers.IO) {
            val referenced = ConfigRefs.collectReferencedPaths(config)
            val index = store.readSyncIndex()
            var indexChanged = false
            for (path in candidatePaths) {
                if (path in referenced) continue
                runCatching { File(filesDir, path).delete() }
                if (index.remove(path) != null) indexChanged = true
            }
            if (indexChanged) store.writeSyncIndex(index)
        }
    }

    private var tabSaveJob: kotlinx.coroutines.Job? = null

    /**
     * Debounced (400 ms, §7.5) read-modify-write via ConfigStore — never a
     * cached full-config blob, which could overwrite a concurrent import's
     * freshly merged config with stale pre-import state.
     */
    fun persistCurrentTab(index: Int) {
        tabSaveJob?.cancel()
        tabSaveJob = scope.launch(Dispatchers.IO) {
            kotlinx.coroutines.delay(400)
            store.updateCurrentTab(index)
        }
    }

    // ---- phone-side library building (Phase 3). §5.4: mutations target
    // phone-origin tabs; the VM enforces the ownership rule. ----

    private fun JsonObject.withTab(
        tabIndex: Int, transform: (JsonObject) -> JsonObject?,
    ): JsonObject? {
        val tabs = this["tabs"] as? JsonArray ?: return null
        val tab = tabs.getOrNull(tabIndex) as? JsonObject ?: return null
        val newTab = transform(tab) ?: return null
        return JsonObject(toMutableMap().apply {
            put("tabs", JsonArray(tabs.toMutableList().apply { set(tabIndex, newTab) }))
        })
    }

    /**
     * Basic slot edit: overlay `overrides` onto the slot's raw JsonObject —
     * unknown desktop fields untouched (§5.1 round-trip). §5.4: edits on
     * desktop-origin tabs are overwritten by the next sync (dialog warns).
     */
    fun updateSlot(tabIndex: Int, slotIndex: Int, overrides: Map<String, JsonElement>) {
        mutateConfig { root ->
            root.withTab(tabIndex) { tab ->
                val slots = tab["slots"] as? JsonObject ?: return@withTab null
                val key = slotIndex.toString()
                val slot = slots[key] as? JsonObject ?: return@withTab null
                JsonObject(tab.toMutableMap().apply {
                    put("slots", JsonObject(slots.toMutableMap().apply {
                        put(key, JsonObject(slot.toMutableMap().apply { putAll(overrides) }))
                    }))
                })
            }
        }
    }

    /** New phone-owned tab; returns its index (valid once the state lands). */
    fun addPhoneTab(name: String): Int {
        val newIndex = _library.value?.config?.tabs?.size ?: return -1
        mutateConfig { root ->
            val tabs = root["tabs"] as? JsonArray ?: JsonArray(emptyList())
            val tab = kotlinx.serialization.json.buildJsonObject {
                put("name", kotlinx.serialization.json.JsonPrimitive(name))
                put("emoji", kotlinx.serialization.json.JsonNull)
                put("color", kotlinx.serialization.json.JsonNull)
                put("section", kotlinx.serialization.json.JsonPrimitive("phone"))
                put("origin", kotlinx.serialization.json.JsonPrimitive("phone"))
                put("slots", JsonObject(emptyMap()))
            }
            JsonObject(root.toMutableMap().apply {
                put("tabs", JsonArray(tabs + tab))
            })
        }
        return newIndex
    }

    fun renameTab(tabIndex: Int, name: String) {
        mutateConfig { root ->
            root.withTab(tabIndex) { tab ->
                JsonObject(tab.toMutableMap().apply {
                    put("name", kotlinx.serialization.json.JsonPrimitive(name))
                })
            }
        }
    }

    fun deleteTab(tabIndex: Int) {
        val lib = _library.value ?: return
        val tabs = lib.config.raw["tabs"] as? JsonArray ?: return
        val tab = tabs.getOrNull(tabIndex) as? JsonObject ?: return
        val candidates = ((tab["slots"] as? JsonObject)?.values ?: emptyList())
            .filterIsInstance<JsonObject>()
            .flatMap { ConfigRefs.slotPaths(it) }
            .toSet()
        var newRootForRefcount: JsonObject? = null
        mutateConfig { root ->
            val rootTabs = root["tabs"] as? JsonArray ?: return@mutateConfig null
            if (rootTabs.size <= 1) return@mutateConfig null
            JsonObject(root.toMutableMap().apply {
                put("tabs", JsonArray(rootTabs.toMutableList().apply { removeAt(tabIndex) }))
            }).also { newRootForRefcount = it }
        }
        newRootForRefcount?.let { refcountDelete(it, candidates) }
    }

    /** Add a sound slot; slotIndex -1 = first free grid position. */
    fun addSlot(
        tabIndex: Int, slotIndex: Int, packPath: String, name: String,
        sourcePackPath: String? = null,
    ) {
        mutateConfig { root ->
            root.withTab(tabIndex) { tab ->
                val slots = tab["slots"] as? JsonObject ?: JsonObject(emptyMap())
                val used = slots.keys.mapNotNull { it.toIntOrNull() }.toSet()
                val index = if (slotIndex >= 0 && slotIndex !in used) slotIndex
                else generateSequence(0) { it + 1 }.first { it !in used }
                val slot = kotlinx.serialization.json.buildJsonObject {
                    put("name", kotlinx.serialization.json.JsonPrimitive(name))
                    put("file_path", kotlinx.serialization.json.JsonPrimitive(packPath))
                    put("volume", kotlinx.serialization.json.JsonPrimitive(1.0))
                    put("speed", kotlinx.serialization.json.JsonPrimitive(1.0))
                    put("preserve_pitch", kotlinx.serialization.json.JsonPrimitive(true))
                    put("loop", kotlinx.serialization.json.JsonPrimitive(false))
                    put("loop_count", kotlinx.serialization.json.JsonPrimitive(0))
                    put("loop_delay", kotlinx.serialization.json.JsonPrimitive(0.0))
                    put("groups", JsonArray(emptyList()))
                    if (sourcePackPath != null) {
                        put("source_file_path",
                            kotlinx.serialization.json.JsonPrimitive(sourcePackPath))
                    }
                }
                JsonObject(tab.toMutableMap().apply {
                    put("slots", JsonObject(slots.toMutableMap().apply {
                        put(index.toString(), slot)
                    }))
                })
            }
        }
    }

    fun deleteSlot(tabIndex: Int, slotIndex: Int) {
        val lib = _library.value ?: return
        val slot = lib.config.tabs.getOrNull(tabIndex)?.slots?.get(slotIndex) ?: return
        val candidates = ConfigRefs.slotPaths(slot.raw)
        var newRootForRefcount: JsonObject? = null
        mutateConfig { root ->
            root.withTab(tabIndex) { tab ->
                val slots = tab["slots"] as? JsonObject ?: return@withTab null
                JsonObject(tab.toMutableMap().apply {
                    put("slots", JsonObject(slots.toMutableMap().apply {
                        remove(slotIndex.toString())
                    }))
                })
            }?.also { newRootForRefcount = it }
        }
        newRootForRefcount?.let { refcountDelete(it, candidates) }
    }

    /** Cross-tab search over name / emoji / group tags (desktop parity). */
    fun search(query: String, groupFilter: String?): List<SearchHit> {
        val lib = _library.value ?: return emptyList()
        val q = query.trim()
        if (q.isEmpty() && groupFilter == null) return emptyList()
        val hits = mutableListOf<SearchHit>()
        lib.config.tabs.forEachIndexed { tabIdx, tab ->
            for ((slotIdx, slot) in tab.slots) {
                if (slot.isEmpty) continue
                if (groupFilter != null && groupFilter !in slot.groups) continue
                if (q.isNotEmpty()) {
                    val hay = buildString {
                        append(slot.name)
                        slot.emoji?.let { append(' ').append(it) }
                        slot.groups.forEach { append(' ').append(it) }
                    }
                    if (!hay.contains(q, ignoreCase = true)) continue
                }
                hits += SearchHit(tabIdx, tab.name, slotIdx, slot)
            }
        }
        return hits
    }

    data class SearchHit(
        val tabIndex: Int,
        val tabName: String,
        val slotIndex: Int,
        val slot: SoundSlot,
    )
}
