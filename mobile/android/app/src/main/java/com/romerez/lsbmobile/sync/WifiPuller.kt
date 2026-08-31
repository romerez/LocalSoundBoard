package com.romerez.lsbmobile.sync

import com.romerez.lsbmobile.BuildConfig
import com.romerez.lsbmobile.data.ConfigStore
import com.romerez.lsbmobile.data.PackManifest
import com.romerez.lsbmobile.data.SUPPORTED_FORMAT_VERSION
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.FileOutputStream
import java.security.DigestOutputStream
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

/**
 * Wi-Fi delta sync (mobile/README.md §6.4): scan the desktop's QR → pull the
 * manifest → download ONLY new/changed files (by path+md5 against
 * sync_index.json and what's actually on disk) → hand the staged set to the
 * same SyncApplier the zip importer uses → POST /v1/complete.
 *
 * Accepts the QR's landing URL (`http://ip:port/?token=…`) or a manifest URL
 * — anything with a host, port and token. Failure semantics per §6.4: each
 * file retries 3×, then is recorded as a warning; the sync completes with
 * warnings and the next sync picks the stragglers up.
 */
class WifiPuller(
    private val cacheDir: File,
    private val filesDir: File,
    private val store: ConfigStore,
    private val applier: SyncApplier,
) {

    data class Progress(
        val phase: Phase,
        val filesDone: Int,
        val filesTotal: Int,
        val bytesDone: Long,
        val bytesTotal: Long,
        val detail: String,
    )

    enum class Phase { CONNECTING, DOWNLOADING, APPLYING, DONE }

    data class Summary(
        val downloadedFiles: Int,
        val downloadedBytes: Long,
        val upToDateFiles: Int,
        val deleted: Int,
        val warnings: List<String>,
        /** Set when the PC serves a NEWER app than the one running. */
        val appUpdate: AppUpdate? = null,
    )

    /** Carries the still-valid server session so the APK can be fetched. */
    data class AppUpdate(
        val versionName: String,
        val versionCode: Int,
        val apkSize: Long,
        val apkMd5: String,
        val base: String,
        val token: String,
    )

    class SyncException(message: String) : Exception(message)

    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .build()
    private val json = Json { ignoreUnknownKeys = true }

    suspend fun pull(scannedUrl: String, onProgress: (Progress) -> Unit): Summary =
        withContext(Dispatchers.IO) {
            val (base, token) = parseScannedUrl(scannedUrl)
            onProgress(Progress(Phase.CONNECTING, 0, 0, 0, 0, base))

            val manifest = fetchManifest(base, token)
            if (manifest.formatVersion > SUPPORTED_FORMAT_VERSION) {
                throw SyncException(
                    "The PC uses pack format v${manifest.formatVersion} — update this app."
                )
            }

            // Diff: changed md5 OR file missing on disk → download.
            val index = store.readSyncIndex()
            val needed = manifest.files.filter { entry ->
                index[entry.path]?.first != entry.md5 ||
                    !File(filesDir, entry.path).isFile
            }
            val bytesTotal = needed.sumOf { it.size }

            val staging = File(cacheDir, "incoming")
            staging.deleteRecursively()
            staging.mkdirs()
            val staged = mutableMapOf<String, File>()
            val warnings = mutableListOf<String>()
            var bytesDone = 0L
            try {
                needed.forEachIndexed { i, entry ->
                    onProgress(Progress(Phase.DOWNLOADING, i, needed.size,
                        bytesDone, bytesTotal, entry.path))
                    val dest = File(staging, entry.path)
                    var ok = false
                    for (attempt in 1..3) {
                        if (downloadAndVerify(base, token, entry.i, entry.md5,
                                entry.size, dest)) {
                            ok = true
                            break
                        }
                    }
                    if (ok) {
                        staged[entry.path] = dest
                        bytesDone += entry.size
                    } else {
                        warnings += entry.path
                    }
                }

                onProgress(Progress(Phase.APPLYING, 0, needed.size,
                    bytesDone, bytesTotal, ""))
                val deleted = applier.apply(manifest, staged) { done, total, detail ->
                    onProgress(Progress(Phase.APPLYING, done, total,
                        bytesDone, bytesTotal, detail))
                }

                // Newer app on the PC? Keep the server session ALIVE (no
                // /complete) so the updater can fetch the APK next.
                val appInfo = manifest.app
                val update = if (appInfo != null &&
                    appInfo.versionCode > BuildConfig.VERSION_CODE
                ) {
                    AppUpdate(appInfo.versionName, appInfo.versionCode,
                        appInfo.apkSize, appInfo.apkMd5, base, token)
                } else null

                if (update == null) sendComplete(base, token)

                onProgress(Progress(Phase.DONE, needed.size, needed.size,
                    bytesDone, bytesTotal, ""))
                Summary(
                    downloadedFiles = staged.size,
                    downloadedBytes = bytesDone,
                    upToDateFiles = manifest.files.size - needed.size,
                    deleted = deleted,
                    warnings = warnings,
                    appUpdate = update,
                )
            } finally {
                staging.deleteRecursively()
            }
        }

    /** Fetch the APK for an in-app update; md5-verified. Sends /complete
     *  after, closing the loop the sync left open. */
    suspend fun downloadApk(
        update: AppUpdate,
        dest: File,
        onProgress: (bytesDone: Long, bytesTotal: Long) -> Unit,
    ): File = withContext(Dispatchers.IO) {
        dest.parentFile?.mkdirs()
        val request = Request.Builder()
            .url("${update.base}/v1/apk?token=${update.token}").build()
        http.newCall(request).execute().use { resp ->
            if (!resp.isSuccessful) throw SyncException(
                "PC answered ${resp.code} for the app file — is the 📱 dialog still open?")
            val digest = MessageDigest.getInstance("MD5")
            var size = 0L
            resp.body?.byteStream()?.use { input ->
                DigestOutputStream(FileOutputStream(dest), digest).use { out ->
                    val buf = ByteArray(1024 * 256)
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        size += n
                        if (size % (2L * 1024 * 1024) < buf.size) {
                            onProgress(size, update.apkSize)
                        }
                    }
                }
            } ?: throw SyncException("Empty app download.")
            val md5 = digest.digest().joinToString("") { "%02x".format(it) }
            if (md5 != update.apkMd5) {
                dest.delete()
                throw SyncException("App download corrupted — try again.")
            }
        }
        sendComplete(update.base, update.token)
        dest
    }

    // ---------- internals ----------

    private fun sendComplete(base: String, token: String) {
        // Best-effort: lets the desktop dialog show ✅ and shut down.
        runCatching {
            http.newCall(
                Request.Builder()
                    .url("$base/v1/complete?token=$token")
                    .post(okhttp3.RequestBody.create(null, ByteArray(0)))
                    .build()
            ).execute().close()
        }
    }

    private fun parseScannedUrl(raw: String): Pair<String, String> {
        val url = raw.trim().toHttpUrlOrNull()
            ?: throw SyncException("That QR/URL isn't a desktop sync link.")
        val token = url.queryParameter("token")
            ?: throw SyncException("The link has no token — rescan the PC's QR.")
        return "${url.scheme}://${url.host}:${url.port}" to token
    }

    private fun fetchManifest(base: String, token: String): PackManifest {
        val request = Request.Builder().url("$base/v1/manifest?token=$token").build()
        http.newCall(request).execute().use { resp ->
            if (resp.code == 403) throw SyncException(
                "The PC rejected the token — the dialog was probably reopened; scan the new QR.")
            if (!resp.isSuccessful) throw SyncException(
                "PC answered ${resp.code} — is the 📱 dialog still open?")
            val body = resp.body?.string()
                ?: throw SyncException("Empty answer from the PC.")
            return json.decodeFromString(PackManifest.serializer(), body)
        }
    }

    /** One download attempt; false on any mismatch/IO error (caller retries). */
    private fun downloadAndVerify(
        base: String, token: String, index: Int,
        expectedMd5: String, expectedSize: Long, dest: File,
    ): Boolean = runCatching {
        val request = Request.Builder().url("$base/v1/file/$index?token=$token").build()
        http.newCall(request).execute().use { resp ->
            if (!resp.isSuccessful) return false
            dest.parentFile?.mkdirs()
            val digest = MessageDigest.getInstance("MD5")
            var size = 0L
            resp.body?.byteStream()?.use { input ->
                DigestOutputStream(FileOutputStream(dest), digest).use { out ->
                    val buf = ByteArray(1024 * 256)
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        size += n
                    }
                }
            } ?: return false
            val md5 = digest.digest().joinToString("") { "%02x".format(it) }
            md5 == expectedMd5 && size == expectedSize
        }
    }.getOrDefault(false)
}
