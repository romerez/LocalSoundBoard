package com.romerez.lsbmobile.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDirection
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.romerez.lsbmobile.AppViewModel
import com.romerez.lsbmobile.audio.PcmDecoder
import com.romerez.lsbmobile.ui.theme.Lsb
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlin.math.abs

/**
 * Touch trim editor (Phase 3): waveform + draggable start/end handles,
 * preview of the selection, save-as-new (never destructive — the original
 * file stays; `source_file_path` records provenance for re-trims).
 */
@Composable
fun TrimScreen(vm: AppViewModel, tabIndex: Int, slotIndex: Int, onBack: () -> Unit) {
    val library by vm.repo.library.collectAsStateWithLifecycle()
    val slot = library?.config?.tabs?.getOrNull(tabIndex)?.slots?.get(slotIndex)

    var pcmResult by remember { mutableStateOf<Result<PcmDecoder.Pcm>?>(null) }
    LaunchedEffect(tabIndex, slotIndex) {
        val s = slot ?: return@LaunchedEffect
        pcmResult = withContext(Dispatchers.IO) {
            runCatching {
                // Re-trim from the ORIGINAL when we have it (desktop "Clone").
                val path = s.sourceFilePath?.takeIf { vm.repo.resolve(it).isFile }
                    ?: s.filePath ?: throw PcmDecoder.DecodeError("No audio file.")
                PcmDecoder.decode(vm.repo.resolve(path))
            }
        }
    }
    DisposableEffect(Unit) { onDispose { vm.stopTrimPreview() } }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Lsb.BgDarkest)
            .statusBarsPadding()
            .navigationBarsPadding()
            .padding(16.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                "←", color = Lsb.TextSecondary, fontSize = 22.sp,
                modifier = Modifier
                    .clip(RoundedCornerShape(8.dp))
                    .clickable(onClick = onBack)
                    .padding(horizontal = 10.dp, vertical = 4.dp),
            )
            Text(
                "✂ " + (slot?.name ?: "…"),
                color = Lsb.TextPrimary, fontSize = 17.sp,
                fontWeight = FontWeight.Bold,
                maxLines = 1, overflow = TextOverflow.Ellipsis,
                style = TextStyle(textDirection = TextDirection.Content),
                modifier = Modifier.padding(start = 8.dp),
            )
        }
        Spacer(Modifier.height(14.dp))

        val result = pcmResult
        when {
            slot == null -> Text("Sound not found.", color = Lsb.TextSecondary)
            result == null -> {
                Text("Decoding audio…", color = Lsb.TextSecondary, fontSize = 13.sp)
                Spacer(Modifier.height(8.dp))
                LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            }
            result.isFailure -> Text(
                result.exceptionOrNull()?.message ?: "Could not decode this file.",
                color = Lsb.Red, fontSize = 14.sp,
            )
            else -> TrimEditor(vm, tabIndex, slot, result.getOrThrow())
        }
    }
}

@Composable
private fun TrimEditor(
    vm: AppViewModel,
    tabIndex: Int,
    slot: com.romerez.lsbmobile.data.SoundSlot,
    pcm: PcmDecoder.Pcm,
) {
    var selStart by remember { mutableStateOf(0f) } // fractions of the file
    var selEnd by remember { mutableStateOf(1f) }
    var showSave by remember { mutableStateOf(false) }
    val durationMs = pcm.durationMs
    val minFrac = (200f / durationMs).coerceAtMost(0.5f) // ≥200ms selection

    var peaks by remember(pcm) { mutableStateOf<FloatArray?>(null) }
    LaunchedEffect(pcm) {
        peaks = withContext(Dispatchers.Default) { buildPeaks(pcm, 720) }
    }

    Column {
        // ---- waveform + handles ----
        var draggingStart by remember { mutableStateOf(true) }
        Canvas(
            modifier = Modifier
                .fillMaxWidth()
                .height(190.dp)
                .clip(RoundedCornerShape(10.dp))
                .background(Lsb.BgDark)
                .pointerInput(pcm) {
                    detectDragGestures(
                        onDragStart = { offset: Offset ->
                            val frac = (offset.x / size.width).coerceIn(0f, 1f)
                            draggingStart = abs(frac - selStart) <= abs(frac - selEnd)
                        },
                        onDrag = { change, _ ->
                            val frac = (change.position.x / size.width).coerceIn(0f, 1f)
                            if (draggingStart) {
                                selStart = frac.coerceAtMost(selEnd - minFrac)
                            } else {
                                selEnd = frac.coerceAtLeast(selStart + minFrac)
                            }
                            change.consume()
                        },
                    )
                },
        ) {
            val w = size.width
            val h = size.height
            val mid = h / 2f
            peaks?.let { p ->
                val buckets = p.size / 2
                val step = w / buckets
                for (i in 0 until buckets) {
                    val x = i * step
                    val inSel = x / w in selStart..selEnd
                    drawLine(
                        color = if (inSel) Lsb.Blurple else Lsb.BgLighter,
                        start = Offset(x, mid - p[i * 2 + 1] * mid * 0.92f),
                        end = Offset(x, mid - p[i * 2] * mid * 0.92f),
                        strokeWidth = step.coerceAtLeast(1f),
                    )
                }
            }
            // selection bounds
            for ((frac, isStart) in listOf(selStart to true, selEnd to false)) {
                val x = frac * w
                drawLine(
                    color = if (isStart) Lsb.Green else Lsb.Red,
                    start = Offset(x, 0f), end = Offset(x, h), strokeWidth = 3f,
                )
                drawCircle(
                    color = if (isStart) Lsb.Green else Lsb.Red,
                    radius = 12f, center = Offset(x, h - 14f),
                )
            }
        }
        Spacer(Modifier.height(6.dp))
        Text(
            "${fmtMs((selStart * durationMs).toLong())}  →  " +
                "${fmtMs((selEnd * durationMs).toLong())}   " +
                "(${fmtMs(((selEnd - selStart) * durationMs).toLong())})",
            color = Lsb.TextSecondary, fontSize = 13.sp,
        )
        Text(
            "Drag near a handle to move it · green = start, red = end",
            color = Lsb.TextMuted, fontSize = 11.sp,
        )
        Spacer(Modifier.height(14.dp))

        val startFrame = (selStart * pcm.frameCount).toInt()
        val endFrame = (selEnd * pcm.frameCount).toInt().coerceAtMost(pcm.frameCount)
        Row {
            Button(
                onClick = { vm.previewTrim(pcm, startFrame, endFrame) },
                colors = ButtonDefaults.buttonColors(containerColor = Lsb.Green),
            ) { Text("▶ Preview") }
            TextButton(
                onClick = vm::stopTrimPreview,
                modifier = Modifier.padding(start = 8.dp),
            ) { Text("⏹ Stop", color = Lsb.Red) }
        }
        Spacer(Modifier.height(8.dp))
        Button(
            onClick = { showSave = true },
            colors = ButtonDefaults.buttonColors(containerColor = Lsb.Blurple),
        ) { Text("💾 Save as new sound") }
        Text(
            "The original stays untouched — save as many cuts as you like.",
            color = Lsb.TextMuted, fontSize = 11.sp,
            modifier = Modifier.padding(top = 6.dp),
        )
    }

    if (showSave) {
        var name by remember { mutableStateOf(slot.name) }
        AlertDialog(
            onDismissRequest = { showSave = false },
            title = { Text("Save cut") },
            confirmButton = {
                TextButton(
                    onClick = {
                        showSave = false
                        vm.saveTrimmedCut(tabIndex, slot, name, pcm,
                            (selStart * pcm.frameCount).toInt(),
                            (selEnd * pcm.frameCount).toInt().coerceAtMost(pcm.frameCount))
                    },
                    enabled = name.isNotBlank(),
                ) { Text("Save", color = Lsb.Green) }
            },
            dismissButton = {
                TextButton(onClick = { showSave = false }) {
                    Text("Cancel", color = Lsb.TextMuted)
                }
            },
            text = {
                OutlinedTextField(
                    value = name, onValueChange = { name = it },
                    label = { Text("Name") }, singleLine = true,
                    textStyle = TextStyle(color = Lsb.TextPrimary,
                        textDirection = TextDirection.Content),
                    modifier = Modifier.fillMaxWidth(),
                )
            },
        )
    }
}

private fun buildPeaks(pcm: PcmDecoder.Pcm, buckets: Int): FloatArray {
    val frames = pcm.frameCount
    val out = FloatArray(buckets * 2)
    if (frames == 0) return out
    val step = (frames / buckets).coerceAtLeast(1)
    for (b in 0 until buckets) {
        val from = b * step
        val to = ((b + 1) * step).coerceAtMost(frames)
        var min = 0f
        var max = 0f
        var i = from
        while (i < to) {
            // mix channels by taking the widest excursion in the frame
            for (c in 0 until pcm.channels) {
                val v = pcm.samples[i * pcm.channels + c] / 32768f
                if (v < min) min = v
                if (v > max) max = v
            }
            i += (step / 200).coerceAtLeast(1) // sparse scan inside big buckets
        }
        out[b * 2] = min
        out[b * 2 + 1] = max
    }
    return out
}

private fun fmtMs(ms: Long): String {
    val totalSec = ms / 1000
    return "%d:%02d.%d".format(totalSec / 60, totalSec % 60, (ms % 1000) / 100)
}
