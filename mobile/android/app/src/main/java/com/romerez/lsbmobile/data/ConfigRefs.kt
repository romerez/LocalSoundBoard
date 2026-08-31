package com.romerez.lsbmobile.data

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject

/**
 * The §5.1 reference-set walk, shared by the sync refcount pass and the
 * phone-side delete flows: a media file may be removed only when NO path
 * field anywhere (file_path, source_file_path, image_path, avatars — across
 * tabs + persons + favorites) points at it.
 */
object ConfigRefs {

    val PATH_FIELDS = listOf("file_path", "source_file_path", "image_path")

    fun collectReferencedPaths(config: JsonObject): Set<String> {
        val refs = mutableSetOf<String>()

        fun addSlot(slot: JsonObject) {
            for (field in PATH_FIELDS) {
                slot[field].asStringOrNull()?.let { refs += it.normalizePackPath() }
            }
        }

        fun addPersonLike(person: JsonObject) {
            person["image_path"].asStringOrNull()?.let { refs += it.normalizePackPath() }
            (person["groups"] as? JsonArray)?.forEach { g ->
                ((g as? JsonObject)?.get("sounds") as? JsonArray)?.forEach { s ->
                    (s as? JsonObject)?.let(::addSlot)
                }
            }
        }

        (config["tabs"] as? JsonArray)?.forEach { t ->
            ((t as? JsonObject)?.get("slots") as? JsonObject)?.values?.forEach { s ->
                (s as? JsonObject)?.let(::addSlot)
            }
        }
        (config["persons"] as? JsonArray)?.forEach { p ->
            (p as? JsonObject)?.let(::addPersonLike)
        }
        (config["favorites_board"] as? JsonObject)?.let(::addPersonLike)
        return refs
    }

    /** All path fields of ONE slot dict, normalized. */
    fun slotPaths(slot: JsonObject): Set<String> =
        PATH_FIELDS.mapNotNull { slot[it].asStringOrNull()?.normalizePackPath() }.toSet()
}
