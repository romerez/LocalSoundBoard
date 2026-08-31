package com.romerez.lsbmobile.audio

import java.io.DataOutputStream
import java.io.File
import java.io.FileOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Minimal RIFF/PCM16 writer for trim-editor cuts (desktop parity: the PC's
 * editor also saves cuts as PCM16 WAV — mobile/README.md §7.6). Keeps the
 * source's channel count + sample rate; no codecs involved.
 */
object WavWriter {

    fun write(
        dest: File,
        samples: ShortArray,
        startSample: Int,      // inclusive, in SHORTS (already channel-aligned)
        endSample: Int,        // exclusive
        channels: Int,
        sampleRate: Int,
    ) {
        val count = (endSample - startSample).coerceAtLeast(0)
        val dataBytes = count * 2
        dest.parentFile?.mkdirs()
        val tmp = File(dest.parentFile, dest.name + ".tmp")
        DataOutputStream(FileOutputStream(tmp).buffered()).use { out ->
            val header = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN)
            header.put("RIFF".toByteArray())
            header.putInt(36 + dataBytes)
            header.put("WAVE".toByteArray())
            header.put("fmt ".toByteArray())
            header.putInt(16)                        // fmt chunk size
            header.putShort(1)                       // PCM
            header.putShort(channels.toShort())
            header.putInt(sampleRate)
            header.putInt(sampleRate * channels * 2) // byte rate
            header.putShort((channels * 2).toShort())// block align
            header.putShort(16)                      // bits per sample
            header.put("data".toByteArray())
            header.putInt(dataBytes)
            out.write(header.array())

            val buf = ByteBuffer.allocate(64 * 1024).order(ByteOrder.LITTLE_ENDIAN)
            var i = startSample
            while (i < endSample) {
                buf.clear()
                val n = minOf(buf.capacity() / 2, endSample - i)
                for (j in 0 until n) buf.putShort(samples[i + j])
                out.write(buf.array(), 0, n * 2)
                i += n
            }
        }
        if (!tmp.renameTo(dest)) {
            tmp.copyTo(dest, overwrite = true)
            tmp.delete()
        }
    }
}
