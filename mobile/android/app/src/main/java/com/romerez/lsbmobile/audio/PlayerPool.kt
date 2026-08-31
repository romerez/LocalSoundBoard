package com.romerez.lsbmobile.audio

import android.content.Context
import android.media.AudioManager
import android.media.audiofx.LoudnessEnhancer
import android.net.Uri
import android.util.Log
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackParameters
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.io.File
import kotlin.math.log10
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * Overlapping playback via a pool of ExoPlayer instances (mobile/README.md
 * §7.3). Desktop parity: per-sound volume 0–2.0, speed 0.5–2.0 with
 * pitch-preserve (Sonic) or chipmunk resample, loop count (0 = ∞) with
 * inter-loop delay, master volume, per-sound + global stop.
 *
 * Volume above 1.0: ExoPlayer.volume caps at 1.0, so the extra gain comes
 * from a per-player LoudnessEnhancer — target gain in MILLIBELS,
 * round(2000·log10(v)) (1.5× → 352 mB, 2.0× → 602 mB). Each player gets an
 * EXPLICIT audio session id at creation; reading player.audioSessionId
 * before the AudioTrack exists can return 0 (the global output mix).
 *
 * Threading: every public method must be called from the MAIN thread
 * (ExoPlayer's application thread). The ticker + loop delays run on
 * Dispatchers.Main via the provided scope.
 */
class PlayerPool(private val context: Context, private val scope: CoroutineScope) {

    companion object {
        private const val TAG = "PlayerPool"
        private const val MAX_VOICES = 12
        private const val TICK_MS = 50L
    }

    data class PlayingSound(
        val playbackId: Long,
        val slotId: String,
        val name: String,
        val progress: Float,
        val elapsedMs: Long,
        val totalMs: Long,
        val looping: Boolean,
    )

    private inner class Voice {
        val player: ExoPlayer
        val enhancer: LoudnessEnhancer?

        var playbackId = 0L
        var slotId = ""
        var name = ""
        var baseVolume = 1f
        var looping = false
        var loopsRemaining = 0 // -1 = infinite (config loop_count 0 maps here)
        var loopDelayMs = 0L
        var active = false
        var delayJob: Job? = null

        init {
            val sessionId = context.getSystemService(AudioManager::class.java)
                .generateAudioSessionId()
            player = ExoPlayer.Builder(context).build().apply {
                setAudioSessionId(sessionId)
                // handleAudioFocus=false: a soundboard overlays other audio
                // (don't pause Spotify/YouTube), matching desktop behavior.
                setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(C.USAGE_MEDIA)
                        .setContentType(C.AUDIO_CONTENT_TYPE_MUSIC)
                        .build(),
                    /* handleAudioFocus = */ false,
                )
            }
            enhancer = runCatching { LoudnessEnhancer(sessionId) }
                .onFailure { Log.w(TAG, "LoudnessEnhancer unavailable", it) }
                .getOrNull()
            player.addListener(object : Player.Listener {
                override fun onPlaybackStateChanged(playbackState: Int) {
                    if (playbackState == Player.STATE_ENDED) onVoiceEnded(this@Voice)
                }
            })
        }
    }

    private val voices = mutableListOf<Voice>()
    private var nextPlaybackId = 1L
    private var tickerJob: Job? = null

    private val _playing = MutableStateFlow<List<PlayingSound>>(emptyList())
    val playing: StateFlow<List<PlayingSound>> = _playing

    /** 0.0–1.5, multiplies every sound (desktop master_volume). */
    var masterVolume: Float = 1f
        set(value) {
            field = value.coerceIn(0f, 1.5f)
            voices.filter { it.active }.forEach { applyGain(it) }
        }

    fun play(
        slotId: String,
        name: String,
        file: File,
        volume: Float,
        speed: Float,
        preservePitch: Boolean,
        loop: Boolean,
        loopCount: Int,
        loopDelaySeconds: Float,
    ) {
        val v = acquireVoice() ?: return
        v.delayJob?.cancel()
        v.playbackId = nextPlaybackId++
        v.slotId = slotId
        v.name = name
        v.baseVolume = volume.coerceIn(0f, 2f)
        v.looping = loop
        // Desktop sentinel mapping (§7.3): config 0 = infinite → internal -1.
        // NOTE loop_count=N semantics (N total plays vs N repeats after the
        // first) must be verified against desktop audio.py side-by-side —
        // Phase 1 acceptance test. Current reading: N additional repeats.
        v.loopsRemaining = if (!loop) 0 else if (loopCount == 0) -1 else loopCount
        v.loopDelayMs = (loopDelaySeconds.coerceIn(0f, 10f) * 1000).toLong()
        v.active = true

        v.player.setMediaItem(MediaItem.fromUri(Uri.fromFile(file)))
        // preserve_pitch=true → Sonic time-stretch (pitch stays 1.0);
        // false → chipmunk/deep resample, matching desktop's two modes.
        v.player.playbackParameters =
            PlaybackParameters(speed.coerceIn(0.5f, 2f),
                if (preservePitch) 1f else speed.coerceIn(0.5f, 2f))
        applyGain(v)
        v.player.prepare()
        v.player.play()
        ensureTicker()
    }

    fun stopSlot(slotId: String) {
        voices.filter { it.active && it.slotId == slotId }.forEach { stopVoice(it) }
        publishSnapshot()
    }

    fun stopAll() {
        voices.filter { it.active }.forEach { stopVoice(it) }
        publishSnapshot()
    }

    fun release() {
        stopAll()
        tickerJob?.cancel()
        voices.forEach {
            runCatching { it.enhancer?.release() }
            it.player.release()
        }
        voices.clear()
    }

    // ---------- internals ----------

    private fun acquireVoice(): Voice? {
        voices.firstOrNull { !it.active }?.let { return it }
        if (voices.size < MAX_VOICES) {
            return runCatching { Voice() }
                .onFailure { Log.e(TAG, "voice creation failed", it) }
                .getOrNull()
                ?.also { voices += it }
        }
        // Pool exhausted: steal the oldest non-looping voice (loops are the
        // sounds the user deliberately keeps running), else the oldest.
        val victim = voices.filter { !it.looping }.minByOrNull { it.playbackId }
            ?: voices.minByOrNull { it.playbackId }
        victim?.let { stopVoice(it) }
        return victim
    }

    private fun stopVoice(v: Voice) {
        v.delayJob?.cancel()
        v.delayJob = null
        v.active = false
        v.player.stop()
        v.player.clearMediaItems()
    }

    private fun onVoiceEnded(v: Voice) {
        if (!v.active) return
        if (v.loopsRemaining != 0) {
            if (v.loopsRemaining > 0) v.loopsRemaining--
            v.delayJob = scope.launch(Dispatchers.Main) {
                if (v.loopDelayMs > 0) delay(v.loopDelayMs)
                if (v.active) {
                    v.player.seekTo(0)
                    v.player.play()
                }
            }
        } else {
            v.active = false
            publishSnapshot()
        }
    }

    private fun applyGain(v: Voice) {
        val effective = v.baseVolume * masterVolume
        v.player.volume = min(1f, effective)
        val gainMb = if (effective > 1f) (2000 * log10(effective.toDouble())).roundToInt() else 0
        runCatching {
            v.enhancer?.setTargetGain(gainMb)
            v.enhancer?.enabled = gainMb > 0
        }
    }

    private fun ensureTicker() {
        if (tickerJob?.isActive == true) return
        tickerJob = scope.launch(Dispatchers.Main) {
            while (isActive) {
                publishSnapshot()
                if (voices.none { it.active }) break
                delay(TICK_MS)
            }
        }
    }

    private fun publishSnapshot() {
        _playing.value = voices.filter { it.active }.map { v ->
            val duration = v.player.duration.takeIf { it != C.TIME_UNSET } ?: 0L
            val position = v.player.currentPosition
            PlayingSound(
                playbackId = v.playbackId,
                slotId = v.slotId,
                name = v.name,
                progress = if (duration > 0) {
                    (position.toFloat() / duration).coerceIn(0f, 1f)
                } else 0f,
                elapsedMs = position,
                totalMs = duration,
                looping = v.looping,
            )
        }
    }
}
