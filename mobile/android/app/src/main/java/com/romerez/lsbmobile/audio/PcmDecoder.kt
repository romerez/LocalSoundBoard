package com.romerez.lsbmobile.audio

import android.media.AudioFormat
import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import java.io.File
import java.nio.ByteOrder

/**
 * Whole-file audio → PCM16 shorts via MediaExtractor + MediaCodec, for the
 * trim editor. Capped at 10 minutes (mobile/README.md §7.6): a 10-min 48kHz
 * stereo file is ~115MB of shorts — fine with largeHeap; longer files must
 * be trimmed on the PC (its LongAudioPicker handles hours-long recordings).
 */
object PcmDecoder {

    class DecodeError(message: String) : Exception(message)

    class Pcm(val samples: ShortArray, val channels: Int, val sampleRate: Int) {
        val frameCount: Int get() = samples.size / channels
        val durationMs: Long get() = frameCount * 1000L / sampleRate
    }

    const val MAX_DURATION_MS = 10 * 60 * 1000L

    fun decode(file: File): Pcm {
        if (!file.isFile) throw DecodeError("Audio file not found on the phone.")
        val extractor = MediaExtractor()
        try {
            extractor.setDataSource(file.absolutePath)
            val trackIndex = (0 until extractor.trackCount).firstOrNull {
                extractor.getTrackFormat(it).getString(MediaFormat.KEY_MIME)
                    ?.startsWith("audio/") == true
            } ?: throw DecodeError("No audio track in this file.")
            val format = extractor.getTrackFormat(trackIndex)
            if (format.containsKey(MediaFormat.KEY_DURATION) &&
                format.getLong(MediaFormat.KEY_DURATION) > MAX_DURATION_MS * 1000
            ) {
                throw DecodeError("Longer than 10 minutes — trim this one on the PC.")
            }
            extractor.selectTrack(trackIndex)
            val mime = format.getString(MediaFormat.KEY_MIME)!!
            val codec = MediaCodec.createDecoderByType(mime)
            try {
                codec.configure(format, null, null, 0)
                codec.start()
                return drainCodec(codec, extractor)
            } finally {
                runCatching { codec.stop() }
                codec.release()
            }
        } finally {
            extractor.release()
        }
    }

    private fun drainCodec(codec: MediaCodec, extractor: MediaExtractor): Pcm {
        val chunks = ArrayList<ShortArray>()
        var totalShorts = 0L
        var outChannels = 2
        var outRate = 48000
        var floatPcm = false
        var inputDone = false
        var outputDone = false
        val info = MediaCodec.BufferInfo()

        fun captureFormat(f: MediaFormat) {
            outChannels = f.getInteger(MediaFormat.KEY_CHANNEL_COUNT)
            outRate = f.getInteger(MediaFormat.KEY_SAMPLE_RATE)
            floatPcm = f.containsKey(MediaFormat.KEY_PCM_ENCODING) &&
                f.getInteger(MediaFormat.KEY_PCM_ENCODING) == AudioFormat.ENCODING_PCM_FLOAT
        }
        captureFormat(codec.outputFormat)

        while (!outputDone) {
            if (!inputDone) {
                val inIndex = codec.dequeueInputBuffer(10_000)
                if (inIndex >= 0) {
                    val buf = codec.getInputBuffer(inIndex)!!
                    val sampleSize = extractor.readSampleData(buf, 0)
                    if (sampleSize < 0) {
                        codec.queueInputBuffer(inIndex, 0, 0, 0,
                            MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                        inputDone = true
                    } else {
                        codec.queueInputBuffer(inIndex, 0, sampleSize,
                            extractor.sampleTime, 0)
                        extractor.advance()
                    }
                }
            }
            when (val outIndex = codec.dequeueOutputBuffer(info, 10_000)) {
                MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> captureFormat(codec.outputFormat)
                MediaCodec.INFO_TRY_AGAIN_LATER -> Unit
                else -> if (outIndex >= 0) {
                    if (info.size > 0) {
                        val buf = codec.getOutputBuffer(outIndex)!!
                        buf.position(info.offset)
                        buf.limit(info.offset + info.size)
                        val chunk: ShortArray = if (floatPcm) {
                            val fb = buf.order(ByteOrder.nativeOrder()).asFloatBuffer()
                            ShortArray(fb.remaining()) {
                                (fb.get() * 32767f).coerceIn(-32768f, 32767f).toInt().toShort()
                            }
                        } else {
                            val sb = buf.order(ByteOrder.nativeOrder()).asShortBuffer()
                            ShortArray(sb.remaining()).also { sb.get(it) }
                        }
                        chunks.add(chunk)
                        totalShorts += chunk.size
                        if (totalShorts * 2 > 250L * 1024 * 1024) {
                            throw DecodeError("File too large to trim on the phone.")
                        }
                    }
                    codec.releaseOutputBuffer(outIndex, false)
                    if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) {
                        outputDone = true
                    }
                }
            }
        }
        if (totalShorts == 0L) throw DecodeError("Could not decode any audio.")
        val all = ShortArray(totalShorts.toInt())
        var pos = 0
        for (chunk in chunks) {
            System.arraycopy(chunk, 0, all, pos, chunk.size)
            pos += chunk.size
        }
        return Pcm(all, outChannels, outRate)
    }
}
