package com.romerez.lsbmobile.data

import android.util.Log
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Persistence for the library config — a direct port of the desktop's
 * data-safety pattern (mobile/README.md §7.5), which the desktop earned
 * through a real people-wipe data-loss incident:
 *
 *  - atomic writes (.tmp → rename), one-version .bak,
 *  - rotating timestamped snapshots in backups/ (phone keeps 10; desktop 20),
 *    written once per launch right after a CLEAN load,
 *  - refuse to save when the load failed,
 *  - never write an empty `tabs` list over non-empty disk state,
 *  - never let empty `persons`/`favorites_board` clobber non-empty disk data.
 */
class ConfigStore(private val filesDir: File) {

    companion object {
        private const val TAG = "ConfigStore"
        const val CONFIG_NAME = "soundboard_config.json"
        const val BASE_NAME = "last_sync_base.json"
        const val INDEX_NAME = "sync_index.json"
        private const val BACKUPS_KEEP = 10
    }

    val configFile = File(filesDir, CONFIG_NAME)
    private val backupsDir = File(filesDir, "backups")

    /** False after a failed parse — saves are refused until a clean load. */
    @Volatile
    var loadedOk: Boolean = false
        private set

    private var snapshotTaken = false

    /** Null on first run (no file yet) or on a failed parse (loadedOk=false). */
    fun load(): JsonObject? {
        if (!configFile.isFile) {
            loadedOk = true // nothing on disk to protect yet
            return null
        }
        return try {
            val root = Json.parseToJsonElement(configFile.readText(Charsets.UTF_8)).jsonObject
            loadedOk = true
            snapshotGoodConfig()
            root
        } catch (t: Throwable) {
            Log.e(TAG, "config load failed — saves disabled to protect the file", t)
            loadedOk = false
            null
        }
    }

    @Synchronized
    fun save(root: JsonObject) {
        if (!loadedOk) {
            Log.w(TAG, "save refused: config never loaded cleanly")
            return
        }
        var out = root

        // Guard: never write an empty tab list over existing data.
        val tabs = out["tabs"] as? JsonArray
        if ((tabs == null || tabs.isEmpty()) && configFile.isFile) {
            Log.w(TAG, "save refused: empty tabs over non-empty disk state")
            return
        }

        // Guard: empty persons/favorites in memory must not clobber disk data.
        val disk = runCatching {
            Json.parseToJsonElement(configFile.readText(Charsets.UTF_8)).jsonObject
        }.getOrNull()
        if (disk != null) {
            val persons = out["persons"] as? JsonArray
            val diskPersons = disk["persons"] as? JsonArray
            if ((persons == null || persons.isEmpty()) && !diskPersons.isNullOrEmpty()) {
                Log.w(TAG, "data-safety: preserving ${diskPersons.size} persons from disk")
                out = JsonObject(out.toMutableMap().apply { put("persons", diskPersons) })
            }
            val fav = out["favorites_board"]
            val diskFav = disk["favorites_board"] as? JsonObject
            if (fav !is JsonObject && diskFav != null) {
                out = JsonObject(out.toMutableMap().apply { put("favorites_board", diskFav) })
            }
        }
        writeConfig(out)
    }

    /**
     * Sync-apply save: an imported pack is AUTHORITATIVE (§5.4 "desktop
     * wins"), so the persons/favorites clobber guards must not resurrect
     * data the desktop deliberately removed. Also the recovery path for a
     * corrupt on-disk config: the broken file is preserved in backups/ and
     * the store returns to a clean, saveable state.
     */
    @Synchronized
    fun saveAuthoritative(root: JsonObject) {
        if (!loadedOk) {
            if (configFile.isFile) {
                runCatching {
                    backupsDir.mkdirs()
                    val stamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
                    configFile.copyTo(File(backupsDir, "corrupt_$stamp.json"), overwrite = true)
                    Log.w(TAG, "corrupt config preserved as corrupt_$stamp.json")
                }
            }
            loadedOk = true
        }
        writeConfig(root)
    }

    /**
     * Read-modify-write of `current_tab` against the DISK state — never a
     * cached blob, so a tab tap landing mid-import can only patch whatever
     * config is current, not overwrite a freshly merged one.
     */
    @Synchronized
    fun updateCurrentTab(index: Int) {
        if (!loadedOk || !configFile.isFile) return
        val disk = runCatching {
            Json.parseToJsonElement(configFile.readText(Charsets.UTF_8)).jsonObject
        }.getOrNull() ?: return
        writeConfig(JsonObject(disk.toMutableMap().apply {
            put("current_tab", kotlinx.serialization.json.JsonPrimitive(index))
        }))
    }

    private fun writeConfig(out: JsonObject) {
        val tmp = File(filesDir, "$CONFIG_NAME.tmp")
        tmp.writeText(out.toString(), Charsets.UTF_8)
        if (configFile.isFile) {
            runCatching { configFile.copyTo(File(filesDir, "$CONFIG_NAME.bak"), overwrite = true) }
        }
        if (!tmp.renameTo(configFile)) {
            // rename() replaces atomically on Android/Linux; this fallback is
            // for the pathological case only.
            configFile.delete()
            if (!tmp.renameTo(configFile)) tmp.copyTo(configFile, overwrite = true)
        }
    }

    /** One rotating snapshot per launch, only after a clean load (§7.5). */
    private fun snapshotGoodConfig() {
        if (snapshotTaken) return
        snapshotTaken = true
        runCatching {
            backupsDir.mkdirs()
            val stamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
            configFile.copyTo(File(backupsDir, "config_$stamp.json"), overwrite = true)
            backupsDir.listFiles { f -> f.name.startsWith("config_") }
                ?.sortedByDescending { it.name }
                ?.drop(BACKUPS_KEEP)
                ?.forEach { it.delete() }
        }.onFailure { Log.w(TAG, "backup snapshot failed", it) }
    }

    // ---- sibling state files (§5.3) ----

    fun writeLastSyncBase(config: JsonObject) {
        atomicWrite(File(filesDir, BASE_NAME), config.toString())
    }

    fun readSyncIndex(): MutableMap<String, Pair<String, Long>> {
        val f = File(filesDir, INDEX_NAME)
        val out = mutableMapOf<String, Pair<String, Long>>()
        if (!f.isFile) return out
        runCatching {
            val root = Json.parseToJsonElement(f.readText(Charsets.UTF_8)).jsonObject
            for ((path, v) in root) {
                val obj = v as? JsonObject ?: continue
                val md5 = obj["md5"].asStringOrNull() ?: continue
                val size = obj["size"].asIntOr(0).toLong()
                out[path] = md5 to size
            }
        }
        return out
    }

    fun writeSyncIndex(index: Map<String, Pair<String, Long>>) {
        val root = kotlinx.serialization.json.buildJsonObject {
            for ((path, entry) in index) {
                put(path, kotlinx.serialization.json.buildJsonObject {
                    put("md5", kotlinx.serialization.json.JsonPrimitive(entry.first))
                    put("size", kotlinx.serialization.json.JsonPrimitive(entry.second))
                })
            }
        }
        atomicWrite(File(filesDir, INDEX_NAME), root.toString())
    }

    private fun atomicWrite(target: File, text: String) {
        val tmp = File(target.parentFile, target.name + ".tmp")
        tmp.writeText(text, Charsets.UTF_8)
        if (!tmp.renameTo(target)) {
            target.delete()
            if (!tmp.renameTo(target)) tmp.copyTo(target, overwrite = true)
        }
    }
}
