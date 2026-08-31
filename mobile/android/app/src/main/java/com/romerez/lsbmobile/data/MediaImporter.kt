package com.romerez.lsbmobile.data

import android.content.ContentResolver
import android.net.Uri
import android.provider.OpenableColumns
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.io.FileOutputStream
import java.security.DigestOutputStream
import java.security.MessageDigest

/**
 * Copies picked/shared audio into app storage using the DESKTOP naming
 * convention `{stem}_{md5-8}{ext}` (mobile/README.md §5.3) — same-content
 * re-imports dedupe to the same file, and a future two-way sync speaks the
 * same naming language as the PC.
 */
class MediaImporter(
    private val resolver: ContentResolver,
    private val filesDir: File,
    private val cacheDir: File,
) {

    data class Imported(val packPath: String, val displayName: String)

    class ImportError(message: String) : Exception(message)

    suspend fun import(uri: Uri): Imported = withContext(Dispatchers.IO) {
        val rawName = queryDisplayName(uri) ?: "sound"
        val stem = sanitizeStem(rawName.substringBeforeLast('.'))
        val ext = rawName.substringAfterLast('.', "").lowercase()
            .takeIf { it.length in 1..5 }?.let { ".$it" } ?: ".m4a"

        val tmp = File(cacheDir, "import_${System.nanoTime()}$ext")
        val digest = MessageDigest.getInstance("MD5")
        val input = resolver.openInputStream(uri)
            ?: throw ImportError("Could not read the selected file.")
        var size = 0L
        input.use { ins ->
            DigestOutputStream(FileOutputStream(tmp), digest).use { out ->
                val buf = ByteArray(1024 * 256)
                while (true) {
                    val n = ins.read(buf)
                    if (n < 0) break
                    out.write(buf, 0, n)
                    size += n
                }
            }
        }
        if (size == 0L) {
            tmp.delete()
            throw ImportError("The selected file is empty.")
        }
        val md5 = digest.digest().joinToString("") { "%02x".format(it) }
        val fileName = "${stem}_${md5.take(8)}$ext"
        val dest = File(File(filesDir, "sounds"), fileName)
        dest.parentFile?.mkdirs()
        if (dest.isFile) {
            tmp.delete() // same content already imported — reuse
        } else if (!tmp.renameTo(dest)) {
            tmp.copyTo(dest, overwrite = true)
            tmp.delete()
        }
        Imported(packPath = "sounds/$fileName", displayName = stem)
    }

    /** Also used by the trim editor + web downloader for local files. */
    suspend fun importLocalFile(src: File, displayName: String? = null): Imported =
        withContext(Dispatchers.IO) {
            import(Uri.fromFile(src)).let {
                if (displayName != null) it.copy(displayName = displayName) else it
            }
        }

    private fun queryDisplayName(uri: Uri): String? {
        if (uri.scheme == "file") return uri.lastPathSegment
        return runCatching {
            resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)
                ?.use { c -> if (c.moveToFirst()) c.getString(0) else null }
        }.getOrNull()
    }

    companion object {
        fun sanitizeStem(raw: String): String {
            val cleaned = raw.replace(Regex("[<>:\"/\\\\|?*]"), "_").trim()
            return (if (cleaned.length > 80) cleaned.take(80) else cleaned)
                .ifEmpty { "sound" }
        }
    }
}
