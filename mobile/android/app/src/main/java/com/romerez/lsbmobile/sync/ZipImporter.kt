package com.romerez.lsbmobile.sync

import android.content.ContentResolver
import android.net.Uri
import com.romerez.lsbmobile.data.PackManifest
import com.romerez.lsbmobile.data.SUPPORTED_FORMAT_VERSION
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import java.io.BufferedInputStream
import java.io.File
import java.io.FileOutputStream
import java.security.DigestOutputStream
import java.security.MessageDigest
import java.util.zip.ZipInputStream

/**
 * SoundPack `.zip` import (mobile/README.md §6.5): zip root = manifest.json
 * (config embedded, authoritative) + sounds/… + images/… mirroring
 * files[].path. Same verify/apply pipeline the Wi-Fi puller will use —
 * only the transport differs.
 *
 * Kill-safety: everything streams into cacheDir/incoming first; media files
 * move into place and the config is written only after every md5 verifies;
 * refcount deletes run LAST, after the new config is on disk. Killing the
 * app mid-import leaves at worst stale staging files (next import clears
 * them) or harmless orphan media (next sync's refcount pass removes them).
 */
class ZipImporter(
    private val resolver: ContentResolver,
    private val cacheDir: File,
    private val applier: SyncApplier,
) {

    data class Progress(
        val phase: Phase,
        val bytesDone: Long,
        val bytesTotal: Long, // 0 until manifest.json has been read
        val detail: String,
    )

    enum class Phase { STAGING, APPLYING, DONE }

    data class Summary(val files: Int, val bytes: Long, val skipped: Int, val deleted: Int)

    class ImportException(message: String) : Exception(message)

    private val json = Json { ignoreUnknownKeys = true }

    suspend fun import(uri: Uri, onProgress: (Progress) -> Unit): Summary =
        withContext(Dispatchers.IO) {
            val staging = File(cacheDir, "incoming")
            staging.deleteRecursively()
            staging.mkdirs()
            try {
                importInto(uri, staging, onProgress)
            } finally {
                staging.deleteRecursively()
            }
        }

    private fun importInto(
        uri: Uri,
        staging: File,
        onProgress: (Progress) -> Unit,
    ): Summary {
        var manifest: PackManifest? = null
        val stagedMd5 = mutableMapOf<String, Pair<String, Long>>() // path → (md5, size)
        val stagedFiles = mutableMapOf<String, File>()
        var bytesDone = 0L

        val input = resolver.openInputStream(uri)
            ?: throw ImportException("Could not open the selected file.")
        ZipInputStream(BufferedInputStream(input)).use { zip ->
            while (true) {
                val entry = zip.nextEntry ?: break
                if (entry.isDirectory) continue
                val name = entry.name.replace('\\', '/')
                if (name == "manifest.json") {
                    val text = zip.readBytes().toString(Charsets.UTF_8)
                    manifest = json.decodeFromString(PackManifest.serializer(), text)
                    val m = manifest!!
                    if (m.formatVersion > SUPPORTED_FORMAT_VERSION) {
                        throw ImportException(
                            "This pack uses format v${m.formatVersion} — update the app."
                        )
                    }
                    continue
                }
                // zip-slip guard + only expected top-level dirs
                if (name.startsWith("/") || ".." in name.split("/")) {
                    throw ImportException("Unsafe path in pack: $name")
                }
                if (!name.startsWith("sounds/") && !name.startsWith("images/")) continue

                val dest = File(staging, name)
                dest.parentFile?.mkdirs()
                val digest = MessageDigest.getInstance("MD5")
                var size = 0L
                DigestOutputStream(FileOutputStream(dest), digest).use { out ->
                    val buf = ByteArray(1024 * 256)
                    while (true) {
                        val n = zip.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        size += n
                        bytesDone += n
                        if (size % (8L * 1024 * 1024) < buf.size) {
                            onProgress(Progress(Phase.STAGING, bytesDone,
                                manifest?.totals?.bytes ?: 0L, name))
                        }
                    }
                }
                val md5 = digest.digest().joinToString("") { "%02x".format(it) }
                stagedMd5[name] = md5 to size
                stagedFiles[name] = dest
                onProgress(Progress(Phase.STAGING, bytesDone,
                    manifest?.totals?.bytes ?: 0L, name))
            }
        }

        val m = manifest
            ?: throw ImportException("Not a SoundPack: manifest.json missing from the zip.")

        // Verify every manifest entry arrived intact.
        for (entry in m.files) {
            val staged = stagedMd5[entry.path]
                ?: throw ImportException("Pack incomplete: missing ${entry.path}")
            if (staged.first != entry.md5 || staged.second != entry.size) {
                throw ImportException("Corrupt file in pack: ${entry.path}")
            }
        }

        onProgress(Progress(Phase.APPLYING, bytesDone, m.totals.bytes, ""))
        val deleted = applier.apply(m, stagedFiles) { done, total, detail ->
            onProgress(Progress(Phase.APPLYING, done.toLong(), total.toLong(), detail))
        }
        onProgress(Progress(Phase.DONE, bytesDone, m.totals.bytes, ""))
        return Summary(m.files.size, m.totals.bytes, m.skipped.size, deleted)
    }
}
