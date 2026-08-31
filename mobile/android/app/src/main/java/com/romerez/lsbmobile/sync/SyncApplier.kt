package com.romerez.lsbmobile.sync

import android.util.Log
import com.romerez.lsbmobile.data.ConfigStore
import com.romerez.lsbmobile.data.PackManifest
import com.romerez.lsbmobile.data.asStringOrNull
import com.romerez.lsbmobile.data.normalizePackPath
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.io.File

/**
 * Applies an incoming desktop pack to the phone library — the §5.4 policy,
 * shared by the zip importer (Phase 1) and the Wi-Fi puller (Phase 2):
 *
 *  | key              | rule                                              |
 *  |------------------|---------------------------------------------------|
 *  | tabs             | desktop-origin tabs replaced wholesale, desktop   |
 *  |                  | order; phone-origin tabs appended, prior order    |
 *  | persons,         | desktop wins (no phone UI edits them before       |
 *  | favorites_board  | Phase 4)                                          |
 *  | custom_groups    | union: desktop set ∪ phone-created tags           |
 *  | everything else  | desktop values carried verbatim (round-trip)      |
 *
 * File deletion follows the §5.1 reference-set refcount: a media file is
 * removed only when NO path field anywhere (file_path, source_file_path,
 * image_path, avatars — tabs + persons + favorites) points at it.
 */
class SyncApplier(private val filesDir: File, private val store: ConfigStore) {

    companion object {
        private const val TAG = "SyncApplier"
        private val PATH_FIELDS = com.romerez.lsbmobile.data.ConfigRefs.PATH_FIELDS
        private const val INDEX_FLUSH_EVERY = 50
    }

    /**
     * @param staged  maps manifest path → staged temp file (md5 already verified)
     * @return number of stale desktop files deleted by the refcount pass
     */
    fun apply(
        manifest: PackManifest,
        staged: Map<String, File>,
        onProgress: (done: Int, total: Int, detail: String) -> Unit,
    ): Int {
        // 1. Move verified files into place (atomic per file — cacheDir and
        //    filesDir share a filesystem). Index updates incrementally so an
        //    interrupted apply is resumable and never lies about disk state.
        val index = store.readSyncIndex()
        var moved = 0
        for (entry in manifest.files) {
            val src = staged[entry.path] ?: continue
            val dest = File(filesDir, entry.path)
            dest.parentFile?.mkdirs()
            if (!src.renameTo(dest)) {
                src.copyTo(dest, overwrite = true)
                src.delete()
            }
            index[entry.path] = entry.md5 to entry.size
            moved++
            if (moved % INDEX_FLUSH_EVERY == 0) store.writeSyncIndex(index)
            onProgress(moved, manifest.files.size, entry.path)
        }

        // 2. Config: normalize + tag origin, then merge with phone state.
        //    A corrupt existing config loads as null → treated as first-run;
        //    saveAuthoritative below recovers the store (and preserves the
        //    broken file in backups/).
        val incoming = transformDesktopConfig(manifest.config)
        val merged = mergeConfigs(incoming, store.load())

        // 3. Persist BEFORE deleting anything: if we die between these
        //    steps the worst leftover is a harmless orphan file the next
        //    sync removes — never a config referencing deleted media.
        //    saveAuthoritative (not save): the pack is the authority (§5.4),
        //    the §7.5 clobber guards must not resurrect removed persons.
        store.writeLastSyncBase(manifest.config) // verbatim, the 3-way merge base
        store.saveAuthoritative(merged)
        store.writeSyncIndex(index)

        // 4. Refcount pass (idempotent, safe to re-run): desktop files that
        //    left the manifest AND are unreferenced by the config we just
        //    wrote get deleted.
        val manifestPaths = manifest.files.mapTo(mutableSetOf()) { it.path }
        val referenced = collectReferencedPaths(merged)
        var deleted = 0
        for (path in index.keys.toList()) {
            if (path in manifestPaths) continue
            if (path in referenced) continue // §5.3: entry lives while the file does
            val f = File(filesDir, path)
            if (!f.isFile || f.delete()) {
                index.remove(path)
                deleted++
            }
        }
        Log.i(TAG, "apply: moved=$moved deleted=$deleted")
        store.writeSyncIndex(index)
        return deleted
    }

    // ---------- config transforms ----------

    private fun JsonObject.withNormalizedPaths(): JsonObject {
        var changed = false
        val out = toMutableMap()
        for (field in PATH_FIELDS) {
            val v = out[field].asStringOrNull() ?: continue
            val norm = v.normalizePackPath()
            if (norm != v) {
                out[field] = JsonPrimitive(norm)
                changed = true
            }
        }
        return if (changed) JsonObject(out) else this
    }

    private fun transformPersonLike(person: JsonObject): JsonObject {
        val out = person.withNormalizedPaths().toMutableMap()
        val groups = out["groups"] as? JsonArray
        if (groups != null) {
            out["groups"] = JsonArray(groups.map { g ->
                val group = g as? JsonObject ?: return@map g
                val sounds = group["sounds"] as? JsonArray ?: return@map g
                JsonObject(group.toMutableMap().apply {
                    put("sounds", JsonArray(sounds.map { s ->
                        (s as? JsonObject)?.withNormalizedPaths() ?: s
                    }))
                })
            })
        }
        return JsonObject(out)
    }

    /** Normalize every path field; stamp `origin: "desktop"` on each tab. */
    fun transformDesktopConfig(config: JsonObject): JsonObject {
        val out = config.toMutableMap()
        (out["tabs"] as? JsonArray)?.let { tabs ->
            out["tabs"] = JsonArray(tabs.map { t ->
                val tab = t as? JsonObject ?: return@map t
                val tabOut = tab.toMutableMap()
                tabOut["origin"] = JsonPrimitive("desktop")
                (tabOut["slots"] as? JsonObject)?.let { slots ->
                    tabOut["slots"] = JsonObject(slots.mapValues { (_, s) ->
                        (s as? JsonObject)?.withNormalizedPaths() ?: s
                    })
                }
                JsonObject(tabOut)
            })
        }
        (out["persons"] as? JsonArray)?.let { persons ->
            out["persons"] = JsonArray(persons.map { p ->
                (p as? JsonObject)?.let(::transformPersonLike) ?: p
            })
        }
        (out["favorites_board"] as? JsonObject)?.let {
            out["favorites_board"] = transformPersonLike(it)
        }
        return JsonObject(out)
    }

    /** §5.4 merge: incoming desktop config + phone-owned content. */
    fun mergeConfigs(incoming: JsonObject, existing: JsonObject?): JsonObject {
        if (existing == null) return incoming
        val out = incoming.toMutableMap()

        val phoneTabs = (existing["tabs"] as? JsonArray)
            ?.filter { (it as? JsonObject)?.get("origin").asStringOrNull() == "phone" }
            ?: emptyList()
        if (phoneTabs.isNotEmpty()) {
            val desktopTabs = out["tabs"] as? JsonArray ?: JsonArray(emptyList())
            out["tabs"] = JsonArray(desktopTabs + phoneTabs)
        }

        val incomingGroups = (incoming["custom_groups"] as? JsonArray)
            ?.mapNotNull { it.asStringOrNull() } ?: emptyList()
        val phoneGroups = (existing["custom_groups"] as? JsonArray)
            ?.mapNotNull { it.asStringOrNull() } ?: emptyList()
        val union = incomingGroups + phoneGroups.filter { it !in incomingGroups }
        if (union.isNotEmpty()) {
            out["custom_groups"] = JsonArray(union.map(::JsonPrimitive))
        }
        return JsonObject(out)
    }

    /** The §5.1 reference set (shared walker), used by the refcount delete. */
    fun collectReferencedPaths(config: JsonObject): Set<String> =
        com.romerez.lsbmobile.data.ConfigRefs.collectReferencedPaths(config)
}
