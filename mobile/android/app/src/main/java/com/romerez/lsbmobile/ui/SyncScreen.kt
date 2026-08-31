package com.romerez.lsbmobile.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import com.romerez.lsbmobile.AppViewModel
import com.romerez.lsbmobile.sync.WifiPuller
import com.romerez.lsbmobile.sync.ZipImporter
import com.romerez.lsbmobile.ui.theme.Lsb

/**
 * Sync screen — Phase 1 ships zip import only; the QR/Wi-Fi puller lands in
 * Phase 2 and reuses the exact same verify/apply pipeline underneath.
 */
@Composable
fun SyncScreen(vm: AppViewModel, onBack: () -> Unit, autoScan: Boolean = false) {
    val importState by vm.importState.collectAsStateWithLifecycle()
    val wifiState by vm.wifiState.collectAsStateWithLifecycle()
    val updateState by vm.updateState.collectAsStateWithLifecycle()
    val library by vm.repo.library.collectAsStateWithLifecycle()
    val context = LocalContext.current

    // The board's 📷 button routes here with autoScan — scanner opens at once.
    androidx.compose.runtime.LaunchedEffect(Unit) {
        if (autoScan && vm.wifiState.value !is AppViewModel.WifiState.Running) {
            startQrScan(context, vm)
        }
    }

    val picker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri -> if (uri != null) vm.importZip(uri) }

    // A running sync must survive the screen dimming — cheap wakelock
    // substitute until the Phase-2 hardening moves this to a foreground
    // service (mobile/README.md §11).
    val busy = wifiState is AppViewModel.WifiState.Running ||
        importState is AppViewModel.ImportState.Running
    val view = LocalView.current
    DisposableEffect(busy) {
        view.keepScreenOn = busy
        onDispose { view.keepScreenOn = false }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Lsb.BgDarkest)
            .statusBarsPadding()
            .navigationBarsPadding()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                "←",
                color = Lsb.TextSecondary,
                fontSize = 22.sp,
                modifier = Modifier
                    .clip(RoundedCornerShape(8.dp))
                    .clickable(onClick = onBack)
                    .padding(horizontal = 10.dp, vertical = 4.dp),
            )
            Text(
                "Sync / Import",
                color = Lsb.TextPrimary,
                fontSize = 18.sp,
                fontWeight = FontWeight.Bold,
                modifier = Modifier.padding(start = 8.dp),
            )
        }
        Spacer(Modifier.height(16.dp))

        WifiSyncCard(vm = vm, wifiState = wifiState)

        if (updateState !is AppViewModel.UpdateState.None) {
            Spacer(Modifier.height(12.dp))
            AppUpdateCard(vm = vm, state = updateState)
        }

        Spacer(Modifier.height(12.dp))

        Card {
            Text("Fallback: zip file", fontWeight = FontWeight.SemiBold,
                color = Lsb.TextPrimary)
            Text(
                "On the PC: press the 📱 button → \"Export .zip\", move the file " +
                    "to this phone (USB / Drive), then pick it below.",
                color = Lsb.TextSecondary, fontSize = 13.sp,
                modifier = Modifier.padding(top = 4.dp, bottom = 10.dp),
            )
            Button(
                onClick = {
                    picker.launch(arrayOf(
                        "application/zip",
                        "application/x-zip-compressed",
                        "application/octet-stream",
                    ))
                },
                enabled = importState !is AppViewModel.ImportState.Running,
                colors = ButtonDefaults.buttonColors(containerColor = Lsb.Blurple),
            ) {
                Text("📦 Import SoundPack (.zip)")
            }
            Text(
                "Only needed when Wi-Fi sync isn't possible (different network).",
                color = Lsb.TextMuted, fontSize = 12.sp,
                modifier = Modifier.padding(top = 8.dp),
            )
        }

        Spacer(Modifier.height(12.dp))
        ImportStatusCard(importState, vm::dismissImportResult)

        val lib = library
        if (lib != null) {
            Spacer(Modifier.height(12.dp))
            Card {
                Text("Library on this phone", fontWeight = FontWeight.SemiBold,
                    color = Lsb.TextPrimary)
                val tabCount = lib.config.tabs.size
                val soundCount = lib.config.tabs.sumOf { it.soundCount }
                val missing = lib.playable.values.count { !it }
                Text(
                    "$tabCount tabs · $soundCount sounds" +
                        (if (missing > 0) " · $missing missing files" else ""),
                    color = Lsb.TextSecondary, fontSize = 13.sp,
                    modifier = Modifier.padding(top = 4.dp),
                )
            }
        }
    }
}

/** Zero-permission Google code scanner; non-composable so both the card
 *  button and the board's 📷 auto-scan route share it. */
private fun startQrScan(context: android.content.Context, vm: AppViewModel) {
    val options = GmsBarcodeScannerOptions.Builder()
        .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
        .build()
    GmsBarcodeScanning.getClient(context, options).startScan()
        .addOnSuccessListener { barcode -> barcode.rawValue?.let(vm::wifiSync) }
        .addOnFailureListener {
            vm.wifiFailed(
                "Scanner unavailable (${it.message ?: "module missing"}) " +
                    "— type the URL from under the PC's QR instead."
            )
        }
}

@Composable
private fun AppUpdateCard(vm: AppViewModel, state: AppViewModel.UpdateState) {
    Card {
        when (state) {
            is AppViewModel.UpdateState.None -> Unit
            is AppViewModel.UpdateState.Available -> {
                val u = state.update
                Text("⬆ App update available", color = Lsb.Yellow,
                    fontWeight = FontWeight.SemiBold)
                Text(
                    "v${u.versionName} · ${u.apkSize / (1024 * 1024)} MB — only the app " +
                        "downloads, your sounds stay untouched.",
                    color = Lsb.TextSecondary, fontSize = 13.sp,
                    modifier = Modifier.padding(top = 4.dp, bottom = 10.dp),
                )
                Row {
                    Button(
                        onClick = vm::downloadAppUpdate,
                        colors = ButtonDefaults.buttonColors(containerColor = Lsb.Blurple),
                    ) { Text("⬇ Update now") }
                    TextButton(onClick = vm::dismissUpdate,
                        modifier = Modifier.padding(start = 8.dp)) {
                        Text("Later", color = Lsb.TextMuted)
                    }
                }
                Text(
                    "Android will ask to confirm the install (first time: allow " +
                        "LSB Mobile to install apps).",
                    color = Lsb.TextMuted, fontSize = 12.sp,
                    modifier = Modifier.padding(top = 6.dp),
                )
            }
            is AppViewModel.UpdateState.Downloading -> {
                Text("⬇ Downloading app update…", color = Lsb.TextPrimary,
                    fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.height(8.dp))
                if (state.bytesTotal > 0) {
                    LinearProgressIndicator(
                        progress = {
                            (state.bytesDone.toFloat() / state.bytesTotal).coerceIn(0f, 1f)
                        },
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Text(
                        "${state.bytesDone / (1024 * 1024)} / ${state.bytesTotal / (1024 * 1024)} MB",
                        color = Lsb.TextMuted, fontSize = 12.sp,
                        modifier = Modifier.padding(top = 4.dp),
                    )
                } else {
                    LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                }
            }
            is AppViewModel.UpdateState.Failed -> {
                Text("❌ Update failed", color = Lsb.Red, fontWeight = FontWeight.SemiBold)
                Text(state.message, color = Lsb.TextSecondary, fontSize = 13.sp,
                    modifier = Modifier.padding(top = 4.dp, bottom = 8.dp))
                Button(onClick = vm::dismissUpdate,
                    colors = ButtonDefaults.buttonColors(containerColor = Lsb.BgLight)) {
                    Text("OK")
                }
            }
        }
    }
}

@Composable
private fun WifiSyncCard(vm: AppViewModel, wifiState: AppViewModel.WifiState) {
    val context = LocalContext.current
    var showUrlEntry by remember { mutableStateOf(false) }
    var manualUrl by remember { mutableStateOf("") }
    val running = wifiState is AppViewModel.WifiState.Running

    Card {
        Text("📶 Sync from the PC (Wi-Fi)", fontWeight = FontWeight.SemiBold,
            color = Lsb.TextPrimary)
        Text(
            "On the PC press 📱, then scan its QR — only what changed gets " +
                "downloaded, everything updates by itself.",
            color = Lsb.TextSecondary, fontSize = 13.sp,
            modifier = Modifier.padding(top = 4.dp, bottom = 10.dp),
        )
        Button(
            onClick = { startQrScan(context, vm) },
            enabled = !running,
            colors = ButtonDefaults.buttonColors(containerColor = Lsb.Green),
        ) {
            Text("📷 Scan the PC's QR")
        }
        TextButton(onClick = { showUrlEntry = !showUrlEntry }) {
            Text("…or type the URL shown under the QR",
                color = Lsb.TextMuted, fontSize = 12.sp)
        }
        if (showUrlEntry) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(
                    value = manualUrl,
                    onValueChange = { manualUrl = it },
                    modifier = Modifier.weight(1f),
                    placeholder = { Text("http://192.168.…:8765/?token=…",
                        color = Lsb.TextMuted, fontSize = 12.sp) },
                    singleLine = true,
                )
                Button(
                    onClick = { vm.wifiSync(manualUrl) },
                    enabled = !running && manualUrl.isNotBlank(),
                    modifier = Modifier.padding(start = 8.dp),
                    colors = ButtonDefaults.buttonColors(containerColor = Lsb.BgLight),
                ) { Text("Go") }
            }
        }

        when (wifiState) {
            is AppViewModel.WifiState.Idle -> Unit
            is AppViewModel.WifiState.Running -> {
                val p = wifiState.progress
                Spacer(Modifier.height(10.dp))
                val label = when (p.phase) {
                    WifiPuller.Phase.CONNECTING -> "Connecting to ${p.detail}…"
                    WifiPuller.Phase.DOWNLOADING ->
                        "Downloading ${p.filesDone + 1}/${p.filesTotal}…"
                    WifiPuller.Phase.APPLYING -> "Updating library…"
                    WifiPuller.Phase.DONE -> "Finishing…"
                }
                Text(label, color = Lsb.TextPrimary, fontSize = 13.sp)
                Spacer(Modifier.height(6.dp))
                if (p.bytesTotal > 0) {
                    LinearProgressIndicator(
                        progress = {
                            (p.bytesDone.toFloat() / p.bytesTotal).coerceIn(0f, 1f)
                        },
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Text(
                        "${p.bytesDone / (1024 * 1024)} / ${p.bytesTotal / (1024 * 1024)} MB",
                        color = Lsb.TextMuted, fontSize = 12.sp,
                        modifier = Modifier.padding(top = 4.dp),
                    )
                } else {
                    LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                }
            }
            is AppViewModel.WifiState.Done -> {
                val s = wifiState.summary
                Spacer(Modifier.height(10.dp))
                Text("✅ Up to date", color = Lsb.Green, fontWeight = FontWeight.SemiBold)
                Text(
                    buildString {
                        append("${s.downloadedFiles} new/changed files · ")
                        append("${s.downloadedBytes / (1024 * 1024)} MB · ")
                        append("${s.upToDateFiles} already current")
                        if (s.deleted > 0) append(" · ${s.deleted} stale removed")
                        if (s.warnings.isNotEmpty()) {
                            append("\n⚠ ${s.warnings.size} files failed — next sync retries them")
                        }
                    },
                    color = Lsb.TextSecondary, fontSize = 13.sp,
                    modifier = Modifier.padding(top = 4.dp, bottom = 8.dp),
                )
                Button(onClick = vm::dismissWifiResult,
                    colors = ButtonDefaults.buttonColors(containerColor = Lsb.BgLight)) {
                    Text("OK")
                }
            }
            is AppViewModel.WifiState.Failed -> {
                Spacer(Modifier.height(10.dp))
                Text("❌ Sync failed", color = Lsb.Red, fontWeight = FontWeight.SemiBold)
                Text(wifiState.message, color = Lsb.TextSecondary, fontSize = 13.sp,
                    modifier = Modifier.padding(top = 4.dp, bottom = 8.dp))
                Button(onClick = vm::dismissWifiResult,
                    colors = ButtonDefaults.buttonColors(containerColor = Lsb.BgLight)) {
                    Text("OK")
                }
            }
        }
    }
}

@Composable
private fun Card(content: @Composable () -> Unit) {
    Surface(
        color = Lsb.BgMedium,
        shape = RoundedCornerShape(12.dp),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(14.dp)) { content() }
    }
}

@Composable
private fun ImportStatusCard(
    state: AppViewModel.ImportState,
    onDismiss: () -> Unit,
) {
    when (state) {
        is AppViewModel.ImportState.Idle -> Unit
        is AppViewModel.ImportState.Running -> Card {
            val p = state.progress
            val label = when (p.phase) {
                ZipImporter.Phase.STAGING -> "Copying files from the pack…"
                ZipImporter.Phase.APPLYING -> "Placing files + updating library…"
                ZipImporter.Phase.DONE -> "Finishing…"
            }
            Text(label, color = Lsb.TextPrimary, fontSize = 13.sp)
            Spacer(Modifier.height(8.dp))
            if (p.bytesTotal > 0 && p.phase == ZipImporter.Phase.STAGING) {
                LinearProgressIndicator(
                    progress = { (p.bytesDone.toFloat() / p.bytesTotal).coerceIn(0f, 1f) },
                    modifier = Modifier.fillMaxWidth(),
                )
                Text(
                    "${p.bytesDone / (1024 * 1024)} / ${p.bytesTotal / (1024 * 1024)} MB",
                    color = Lsb.TextMuted, fontSize = 12.sp,
                    modifier = Modifier.padding(top = 4.dp),
                )
            } else {
                LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            }
            Text(
                "Keep the app open — big first import takes a few minutes.",
                color = Lsb.TextMuted, fontSize = 12.sp,
                modifier = Modifier.padding(top = 6.dp),
            )
        }
        is AppViewModel.ImportState.Done -> Card {
            val s = state.summary
            Text("✅ Import finished", color = Lsb.Green,
                fontWeight = FontWeight.SemiBold)
            Text(
                "${s.files} files · ${s.bytes / (1024 * 1024)} MB" +
                    (if (s.skipped > 0) " · ${s.skipped} skipped on desktop" else "") +
                    (if (s.deleted > 0) " · ${s.deleted} stale files removed" else ""),
                color = Lsb.TextSecondary, fontSize = 13.sp,
                modifier = Modifier.padding(top = 4.dp, bottom = 8.dp),
            )
            Button(onClick = onDismiss,
                colors = ButtonDefaults.buttonColors(containerColor = Lsb.BgLight)) {
                Text("OK")
            }
        }
        is AppViewModel.ImportState.Failed -> Card {
            Text("❌ Import failed", color = Lsb.Red, fontWeight = FontWeight.SemiBold)
            Text(state.message, color = Lsb.TextSecondary, fontSize = 13.sp,
                modifier = Modifier.padding(top = 4.dp, bottom = 8.dp))
            Button(onClick = onDismiss,
                colors = ButtonDefaults.buttonColors(containerColor = Lsb.BgLight)) {
                Text("OK")
            }
        }
    }
}
