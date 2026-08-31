package com.romerez.lsbmobile.sync

import android.content.Context
import com.yausername.youtubedl_android.YoutubeDL
import com.yausername.youtubedl_android.YoutubeDLRequest
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File

/**
 * yt-dlp wrapper (mobile/README.md §7.7): downloads the best single audio
 * stream — `.m4a` (AAC) or `.webm` (Opus) — and NEVER transcodes (no ffmpeg
 * bundled; ExoPlayer plays both containers natively). The downloaded file
 * then goes through MediaImporter for the md5-suffixed copy into sounds/.
 */
class WebDownloader(private val appContext: Context, private val cacheDir: File) {

    class DownloadError(message: String) : Exception(message)

    @Volatile
    private var initialized = false
    private val initLock = Any()

    private fun ensureInit() {
        synchronized(initLock) {
            if (!initialized) {
                try {
                    YoutubeDL.getInstance().init(appContext)
                    initialized = true
                } catch (t: Throwable) {
                    throw DownloadError(
                        "Downloader engine failed to start: ${t.message ?: "unknown"}"
                    )
                }
            }
        }
    }

    suspend fun download(
        url: String,
        onProgress: (percent: Float, line: String) -> Unit,
    ): File = withContext(Dispatchers.IO) {
        ensureInit()
        val dir = File(cacheDir, "webdl")
        dir.deleteRecursively()
        dir.mkdirs()
        val request = YoutubeDLRequest(url).apply {
            addOption("-f", "bestaudio[ext=m4a]/bestaudio")
            addOption("-o", dir.absolutePath + "/%(title).80s.%(ext)s")
            addOption("--no-playlist")
            addOption("--no-mtime")
        }
        try {
            YoutubeDL.getInstance().execute(request, null) { progress, _, line ->
                onProgress(progress, line)
            }
        } catch (t: Throwable) {
            throw DownloadError(t.message?.take(300) ?: "Download failed.")
        }
        dir.listFiles()?.filter { it.isFile }?.maxByOrNull { it.length() }
            ?: throw DownloadError("The download produced no file.")
    }
}
