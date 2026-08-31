package com.romerez.lsbmobile.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull

/*
 * Read-only views over the desktop config JSON (mobile/README.md §5.1).
 *
 * The phone keeps the desktop schema VERBATIM: every object retains its raw
 * JsonObject so unknown desktop-only fields (hotkey, ptt_*, voice_changer…)
 * round-trip untouched. These classes only PROJECT known fields for the UI;
 * mutations happen at the JsonObject level (SyncApplier / future editors).
 *
 * Tolerance rules (§5.2, all verified against the real config):
 * backslash paths, sparse string-keyed slot dicts, dangling refs, absent
 * top-level keys, volume up to 2.0, null favorites_board.
 */

// ---------- tolerant JsonElement accessors ----------

internal fun JsonElement?.asStringOrNull(): String? {
    val p = this as? JsonPrimitive ?: return null
    if (p is JsonNull) return null
    return p.content
}

internal fun JsonElement?.asFloatOr(default: Float): Float =
    (this as? JsonPrimitive)?.doubleOrNull?.toFloat() ?: default

internal fun JsonElement?.asIntOr(default: Int): Int =
    (this as? JsonPrimitive)?.let { it.intOrNull ?: it.doubleOrNull?.toInt() } ?: default

internal fun JsonElement?.asBoolOr(default: Boolean): Boolean =
    (this as? JsonPrimitive)?.booleanOrNull ?: default

/** Desktop stores Windows-style relative paths ("sounds\x.wav"). */
internal fun String.normalizePackPath(): String = replace('\\', '/')

// ---------- config views ----------

class SoundSlot(val raw: JsonObject) {
    val name: String = raw["name"].asStringOrNull() ?: ""
    val filePath: String? = raw["file_path"].asStringOrNull()?.normalizePackPath()
    val sourceFilePath: String? = raw["source_file_path"].asStringOrNull()?.normalizePackPath()
    val imagePath: String? = raw["image_path"].asStringOrNull()?.normalizePackPath()
    val emoji: String? = raw["emoji"].asStringOrNull()?.takeIf { it.isNotBlank() }
    val colorHex: String? = raw["color"].asStringOrNull()
    val volume: Float = raw["volume"].asFloatOr(1f).coerceIn(0f, 2f)
    val speed: Float = raw["speed"].asFloatOr(1f).coerceIn(0.5f, 2f)
    val preservePitch: Boolean = raw["preserve_pitch"].asBoolOr(true)
    val loop: Boolean = raw["loop"].asBoolOr(false)

    /** 0 = infinite (desktop sentinel; the player maps it to -1 internally). */
    val loopCount: Int = raw["loop_count"].asIntOr(0)
    val loopDelaySeconds: Float = raw["loop_delay"].asFloatOr(0f).coerceIn(0f, 10f)
    val groups: List<String> =
        (raw["groups"] as? JsonArray)?.mapNotNull { it.asStringOrNull() } ?: emptyList()

    val isEmpty: Boolean get() = filePath.isNullOrBlank()
}

class SoundTab(val raw: JsonObject) {
    val name: String = raw["name"].asStringOrNull() ?: "Tab"
    val emoji: String? = raw["emoji"].asStringOrNull()?.takeIf { it.isNotBlank() }
    val colorHex: String? = raw["color"].asStringOrNull()

    /** "desktop" (mirrored, replaced on sync) or "phone" (never touched). §5.4 */
    val origin: String = raw["origin"].asStringOrNull() ?: "desktop"

    /**
     * Master section, assigned ON THE PC: "phone" tabs are what this app
     * shows by default; "pc" tabs are visible via the section switcher.
     * Orthogonal to `origin` (sync ownership).
     */
    val section: String = raw["section"].asStringOrNull() ?: "pc"

    /**
     * Sparse, string-keyed on disk ({"1": …, "4": …}); keys are GRID
     * POSITIONS and must never be renumbered (§5.2 rule 2).
     */
    val slots: Map<Int, SoundSlot> = buildMap {
        val obj = raw["slots"] as? JsonObject ?: return@buildMap
        for ((key, value) in obj) {
            val idx = key.toIntOrNull() ?: continue
            val slot = (value as? JsonObject)?.let(::SoundSlot) ?: continue
            put(idx, slot)
        }
    }

    val maxSlotIndex: Int = slots.keys.maxOrNull() ?: -1
    val soundCount: Int = slots.values.count { !it.isEmpty }
}

class LibraryConfig(val raw: JsonObject) {
    val tabs: List<SoundTab> =
        (raw["tabs"] as? JsonArray)?.mapNotNull { (it as? JsonObject)?.let(::SoundTab) }
            ?: emptyList()
    val customGroups: List<String> =
        (raw["custom_groups"] as? JsonArray)?.mapNotNull { it.asStringOrNull() } ?: emptyList()
    val currentTab: Int = raw["current_tab"].asIntOr(0).coerceIn(0, (tabs.size - 1).coerceAtLeast(0))
}

// ---------- SoundPack manifest (mobile/README.md §6.3) ----------

const val SUPPORTED_FORMAT_VERSION = 1

@Serializable
data class PackFileEntry(val i: Int, val path: String, val size: Long, val md5: String)

@Serializable
data class PackSkipped(val path: String, val reason: String)

@Serializable
data class PackTotals(val files: Int = 0, val bytes: Long = 0)

@Serializable
data class PackAppInfo(
    @SerialName("version_code") val versionCode: Int = 0,
    @SerialName("version_name") val versionName: String = "",
    @SerialName("apk_md5") val apkMd5: String = "",
    @SerialName("apk_size") val apkSize: Long = 0,
)

@Serializable
data class PackManifest(
    @SerialName("format_version") val formatVersion: Int,
    @SerialName("exported_at") val exportedAt: String = "",
    @SerialName("desktop_version") val desktopVersion: String = "",
    val config: JsonObject,
    val files: List<PackFileEntry> = emptyList(),
    val skipped: List<PackSkipped> = emptyList(),
    val totals: PackTotals = PackTotals(),
    /** Desktop's built APK version — drives the in-app updater. */
    val app: PackAppInfo? = null,
)
