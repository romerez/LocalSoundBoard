package com.romerez.lsbmobile.ui

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.items as lazyRowItems
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextDirection
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import coil3.compose.AsyncImage
import com.romerez.lsbmobile.AppViewModel
import com.romerez.lsbmobile.audio.PlayerPool
import com.romerez.lsbmobile.data.LibraryRepository
import com.romerez.lsbmobile.data.SoundSlot
import com.romerez.lsbmobile.ui.theme.Lsb
import com.romerez.lsbmobile.ui.theme.parseHexColor

/**
 * The board: search row → tab chips → slot grid → now-playing bar.
 * Read-only in Phase 1 (playback + browsing; editing arrives in Phase 3).
 * Visual spec mirrors the desktop slot tiles (accent color, emoji badge,
 * image thumbnail, 2-line RTL-correct name, progress strip).
 */
/** Long-press target: which slot the edit dialog is open for. */
private data class EditTarget(
    val tabIndex: Int,
    val slotIndex: Int,
    val slot: SoundSlot,
    val desktopOwned: Boolean,
)

@Composable
fun BoardScreen(
    vm: AppViewModel,
    onOpenSync: () -> Unit,
    onScanSync: () -> Unit,
    onOpenTrim: (tabIndex: Int, slotIndex: Int) -> Unit,
) {
    val library by vm.repo.library.collectAsStateWithLifecycle()
    val loadFailed by vm.repo.loadFailed.collectAsStateWithLifecycle()
    val currentTab by vm.currentTab.collectAsStateWithLifecycle()
    val searchQuery by vm.searchQuery.collectAsStateWithLifecycle()
    val groupFilter by vm.groupFilter.collectAsStateWithLifecycle()
    val gridColumns by vm.gridColumns.collectAsStateWithLifecycle()
    val playing by vm.players.playing.collectAsStateWithLifecycle()
    val activeSection by vm.activeSection.collectAsStateWithLifecycle()

    var showSettings by remember { mutableStateOf(false) }
    var editTarget by remember { mutableStateOf<EditTarget?>(null) }
    var sheetTarget by remember { mutableStateOf<EditTarget?>(null) }
    var showNewTabDialog by remember { mutableStateOf(false) }
    var showWebDl by remember { mutableStateOf(false) }
    var tabEditIndex by remember { mutableStateOf<Int?>(null) }
    var addTarget by remember { mutableStateOf<Pair<Int, Int>?>(null) }

    // One-shot notices from the ViewModel.
    val context = androidx.compose.ui.platform.LocalContext.current
    val toastMsg by vm.toast.collectAsStateWithLifecycle()
    androidx.compose.runtime.LaunchedEffect(toastMsg) {
        toastMsg?.let {
            android.widget.Toast.makeText(context, it, android.widget.Toast.LENGTH_SHORT).show()
            vm.consumeToast()
        }
    }

    // SAF picker for "tap an empty slot in a phone tab".
    val audioPicker = androidx.activity.compose.rememberLauncherForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.OpenDocument()
    ) { uri ->
        val target = addTarget
        addTarget = null
        if (uri != null && target != null) {
            vm.addSoundFromUri(target.first, target.second, uri)
        }
    }
    val audioMimes = arrayOf("audio/*", "application/ogg", "video/mp4", "video/webm")

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Lsb.BgDarkest)
            .statusBarsPadding()
            .navigationBarsPadding()
    ) {
        TopRow(
            searchQuery = searchQuery,
            onSearchChange = { vm.searchQuery.value = it },
            anyPlaying = playing.isNotEmpty(),
            onStopAll = vm::stopAll,
            onScanSync = onScanSync,
            onOpenSync = onOpenSync,
            onOpenSettings = { showSettings = true },
            onWebDownload = { showWebDl = true },
        )

        val lib = library
        when {
            lib == null -> EmptyLibraryHint(loadFailed, onOpenSync)
            searchQuery.isNotBlank() || groupFilter != null -> SearchResultsGrid(
                vm = vm,
                hits = vm.repo.search(searchQuery, groupFilter),
                columns = gridColumns,
                playing = playing,
                onEdit = { hit ->
                    val origin = lib.config.tabs.getOrNull(hit.tabIndex)?.origin
                    sheetTarget = EditTarget(hit.tabIndex, hit.slotIndex, hit.slot,
                        desktopOwned = origin == "desktop")
                },
                modifier = Modifier.weight(1f),
            )
            else -> {
                SectionSwitcher(
                    activeSection = activeSection,
                    onSelect = vm::setActiveSection,
                )
                val sectionTabs = lib.config.tabs.withIndex()
                    .filter { it.value.section == activeSection }
                if (sectionTabs.isEmpty() && activeSection == "phone") {
                    EmptyPhoneSectionHint(onShowPc = { vm.setActiveSection("pc") })
                    Spacer(Modifier.weight(1f))
                } else {
                TabChipsRow(
                    tabs = sectionTabs,
                    currentTab = currentTab,
                    onSelect = vm::selectTab,
                    onLongPress = { index ->
                        if (vm.isPhoneTab(index)) tabEditIndex = index
                        else vm.toast.value = "PC tabs are managed on the PC"
                    },
                    showNewTab = activeSection == "phone",
                    onNewTab = { showNewTabDialog = true },
                )
                val tab = lib.config.tabs.getOrNull(currentTab)
                if (tab != null) {
                    val phoneOwned = tab.origin == "phone"
                    LazyVerticalGrid(
                        columns = GridCells.Fixed(gridColumns),
                        modifier = Modifier.weight(1f),
                        contentPadding = androidx.compose.foundation.layout.PaddingValues(8.dp),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        // Phone tabs always show one extra empty row to add into.
                        val maxIndex = if (phoneOwned) {
                            tab.maxSlotIndex + gridColumns
                        } else tab.maxSlotIndex
                        items((0..maxIndex).toList(), key = { it }) { index ->
                            val slot = tab.slots[index]
                            if (slot == null || slot.isEmpty) {
                                EmptySlotTile(
                                    showAdd = phoneOwned,
                                    onClick = if (phoneOwned) {
                                        {
                                            addTarget = currentTab to index
                                            audioPicker.launch(audioMimes)
                                        }
                                    } else null,
                                )
                            } else {
                                SlotTile(
                                    slot = slot,
                                    playable = lib.playable[slot.filePath] == true,
                                    progress = playing
                                        .filter { it.slotId == "t$currentTab:$index" }
                                        .maxByOrNull { it.playbackId }?.progress,
                                    imageFile = slot.imagePath?.let { vm.repo.resolve(it) },
                                    onTap = { vm.playSlot(currentTab, index, slot) },
                                    onStop = { vm.stopSlot(currentTab, index) },
                                    onLongPress = {
                                        sheetTarget = EditTarget(
                                            currentTab, index, slot,
                                            desktopOwned = tab.origin == "desktop",
                                        )
                                    },
                                )
                            }
                        }
                    }
                } else {
                    Spacer(Modifier.weight(1f))
                }
                } // section non-empty
            }
        }

        NowPlayingBar(playing = playing, onStopAll = vm::stopAll)
    }

    if (showSettings) {
        SettingsDialog(vm = vm, onDismiss = { showSettings = false })
    }
    editTarget?.let { target ->
        SlotEditDialog(
            target = target,
            onSave = { name, volume, speed, pitch, loop, loopCount, loopDelay ->
                vm.editSlot(target.tabIndex, target.slotIndex, name, volume,
                    speed, pitch, loop, loopCount, loopDelay)
                editTarget = null
            },
            onDismiss = { editTarget = null },
        )
    }
    sheetTarget?.let { target ->
        SlotActionSheet(
            target = target,
            onEdit = { sheetTarget = null; editTarget = target },
            onTrim = { sheetTarget = null; onOpenTrim(target.tabIndex, target.slotIndex) },
            onDelete = if (!target.desktopOwned) {
                { sheetTarget = null; vm.deleteSlot(target.tabIndex, target.slotIndex) }
            } else null,
            onDismiss = { sheetTarget = null },
        )
    }
    if (showWebDl) {
        WebDownloadDialog(vm = vm, onDismiss = { showWebDl = false })
    }
    if (showNewTabDialog) {
        TextPromptDialog(
            title = "New 📱 tab",
            initial = "",
            confirmLabel = "Create",
            onConfirm = { name -> showNewTabDialog = false; vm.createPhoneTab(name) },
            onDismiss = { showNewTabDialog = false },
        )
    }
    tabEditIndex?.let { index ->
        val tabName = library?.config?.tabs?.getOrNull(index)?.name ?: ""
        TabEditDialog(
            currentName = tabName,
            onRename = { name -> tabEditIndex = null; vm.renamePhoneTab(index, name) },
            onDelete = { tabEditIndex = null; vm.deletePhoneTab(index) },
            onDismiss = { tabEditIndex = null },
        )
    }
}

// ---------- Phase-3 dialogs ----------

@Composable
private fun WebDownloadDialog(vm: AppViewModel, onDismiss: () -> Unit) {
    val state by vm.webDlState.collectAsStateWithLifecycle()
    var url by remember { mutableStateOf("") }
    val running = state is AppViewModel.WebDlState.Running
    AlertDialog(
        onDismissRequest = { if (!running) onDismiss() },
        title = { Text("🌐 Download from web") },
        confirmButton = {
            TextButton(
                onClick = { vm.webDownload(url) },
                enabled = !running && url.isNotBlank(),
            ) { Text("⬇ Download", color = Lsb.Green) }
        },
        dismissButton = {
            TextButton(onClick = onDismiss, enabled = !running) {
                Text("Close", color = Lsb.TextMuted)
            }
        },
        text = {
            Column {
                Text(
                    "Paste a YouTube (or most sites) link — the audio becomes " +
                        "a sound in a 📱 tab.",
                    color = Lsb.TextSecondary, fontSize = 13.sp,
                    modifier = Modifier.padding(bottom = 8.dp),
                )
                OutlinedTextField(
                    value = url,
                    onValueChange = { url = it },
                    placeholder = { Text("https://…", color = Lsb.TextMuted) },
                    singleLine = true,
                    enabled = !running,
                    modifier = Modifier.fillMaxWidth(),
                )
                when (val s = state) {
                    is AppViewModel.WebDlState.Idle -> Unit
                    is AppViewModel.WebDlState.Running -> {
                        Spacer(Modifier.height(10.dp))
                        if (s.percent in 0.1f..100f) {
                            androidx.compose.material3.LinearProgressIndicator(
                                progress = { (s.percent / 100f).coerceIn(0f, 1f) },
                                modifier = Modifier.fillMaxWidth(),
                            )
                        } else {
                            androidx.compose.material3.LinearProgressIndicator(
                                modifier = Modifier.fillMaxWidth(),
                            )
                        }
                        Text(s.line, color = Lsb.TextMuted, fontSize = 11.sp,
                            maxLines = 1, overflow = TextOverflow.Ellipsis,
                            modifier = Modifier.padding(top = 4.dp))
                        Text("First download sets up the engine (~15s extra).",
                            color = Lsb.TextMuted, fontSize = 11.sp)
                    }
                    is AppViewModel.WebDlState.Failed -> {
                        Spacer(Modifier.height(8.dp))
                        Text("❌ ${s.message}", color = Lsb.Red, fontSize = 12.sp)
                        TextButton(onClick = vm::dismissWebDlError) {
                            Text("Clear", color = Lsb.TextMuted)
                        }
                    }
                }
            }
        },
    )
}

@Composable
private fun SlotActionSheet(
    target: EditTarget,
    onEdit: () -> Unit,
    onTrim: () -> Unit,
    onDelete: (() -> Unit)?,
    onDismiss: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = {},
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel", color = Lsb.TextMuted) } },
        title = {
            Text(target.slot.name, maxLines = 1, overflow = TextOverflow.Ellipsis,
                style = TextStyle(textDirection = TextDirection.Content))
        },
        text = {
            Column {
                TextButton(onClick = onEdit, modifier = Modifier.fillMaxWidth()) {
                    Text("⚙ Edit settings", color = Lsb.TextPrimary)
                }
                TextButton(onClick = onTrim, modifier = Modifier.fillMaxWidth()) {
                    Text("✂ Trim / cut", color = Lsb.TextPrimary)
                }
                if (onDelete != null) {
                    TextButton(onClick = onDelete, modifier = Modifier.fillMaxWidth()) {
                        Text("🗑 Delete sound", color = Lsb.Red)
                    }
                } else {
                    Text("PC sounds can be trimmed here (the cut lands in a 📱 tab), " +
                        "but only the PC can delete them.",
                        color = Lsb.TextMuted, fontSize = 11.sp,
                        modifier = Modifier.padding(top = 6.dp))
                }
            }
        },
    )
}

@Composable
private fun TextPromptDialog(
    title: String,
    initial: String,
    confirmLabel: String,
    onConfirm: (String) -> Unit,
    onDismiss: () -> Unit,
) {
    var value by remember { mutableStateOf(initial) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        confirmButton = {
            TextButton(onClick = { onConfirm(value) }, enabled = value.isNotBlank()) {
                Text(confirmLabel, color = Lsb.Green)
            }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel", color = Lsb.TextMuted) } },
        text = {
            OutlinedTextField(
                value = value,
                onValueChange = { value = it },
                singleLine = true,
                textStyle = TextStyle(color = Lsb.TextPrimary,
                    textDirection = TextDirection.Content),
                modifier = Modifier.fillMaxWidth(),
            )
        },
    )
}

@Composable
private fun TabEditDialog(
    currentName: String,
    onRename: (String) -> Unit,
    onDelete: () -> Unit,
    onDismiss: () -> Unit,
) {
    var name by remember { mutableStateOf(currentName) }
    var confirmDelete by remember { mutableStateOf(false) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Edit 📱 tab") },
        confirmButton = {
            TextButton(onClick = { onRename(name) }, enabled = name.isNotBlank()) {
                Text("Save", color = Lsb.Green)
            }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel", color = Lsb.TextMuted) } },
        text = {
            Column {
                OutlinedTextField(
                    value = name,
                    onValueChange = { name = it },
                    label = { Text("Name") },
                    singleLine = true,
                    textStyle = TextStyle(color = Lsb.TextPrimary,
                        textDirection = TextDirection.Content),
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(12.dp))
                if (!confirmDelete) {
                    TextButton(onClick = { confirmDelete = true }) {
                        Text("🗑 Delete this tab…", color = Lsb.Red)
                    }
                } else {
                    Text("Delete the tab AND its sounds from the phone?",
                        color = Lsb.TextSecondary, fontSize = 13.sp)
                    TextButton(onClick = onDelete) {
                        Text("Yes, delete everything", color = Lsb.Red)
                    }
                }
            }
        },
    )
}

// ---------- top row ----------

@Composable
private fun TopRow(
    searchQuery: String,
    onSearchChange: (String) -> Unit,
    anyPlaying: Boolean,
    onStopAll: () -> Unit,
    onScanSync: () -> Unit,
    onOpenSync: () -> Unit,
    onOpenSettings: () -> Unit,
    onWebDownload: () -> Unit = {},
) {
    var menuOpen by remember { mutableStateOf(false) }
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 8.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        OutlinedTextField(
            value = searchQuery,
            onValueChange = onSearchChange,
            modifier = Modifier.weight(1f),
            placeholder = { Text("🔍 Search…", color = Lsb.TextMuted) },
            trailingIcon = if (searchQuery.isNotEmpty()) {
                {
                    Text(
                        "✕",
                        color = Lsb.TextMuted,
                        modifier = Modifier
                            .clip(CircleShape)
                            .clickable { onSearchChange("") }
                            .padding(8.dp),
                    )
                }
            } else null,
            singleLine = true,
            textStyle = TextStyle(
                color = Lsb.TextPrimary,
                textDirection = TextDirection.Content, // Hebrew queries type RTL
            ),
            shape = RoundedCornerShape(12.dp),
        )
        if (anyPlaying) {
            EmojiButton("⏹", tint = Lsb.Red, onClick = onStopAll)
        }
        // One-tap delta sync: opens the QR scanner straight away.
        EmojiButton("📷", tint = Lsb.Green, onClick = onScanSync)
        Box {
            EmojiButton("⋮") { menuOpen = true }
            DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                DropdownMenuItem(
                    text = { Text("📥 Sync / Import") },
                    onClick = { menuOpen = false; onOpenSync() },
                )
                DropdownMenuItem(
                    text = { Text("🌐 Download from web") },
                    onClick = { menuOpen = false; onWebDownload() },
                )
                DropdownMenuItem(
                    text = { Text("⚙ Settings") },
                    onClick = { menuOpen = false; onOpenSettings() },
                )
            }
        }
    }
}

@Composable
private fun EmojiButton(glyph: String, tint: Color = Lsb.TextSecondary, onClick: () -> Unit) {
    Box(
        modifier = Modifier
            .padding(start = 6.dp)
            .size(44.dp)
            .clip(RoundedCornerShape(10.dp))
            .background(Lsb.BgMedium)
            .clickable(onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Text(glyph, color = tint, fontSize = 18.sp)
    }
}

// ---------- master sections + tab chips ----------

@Composable
private fun SectionSwitcher(activeSection: String, onSelect: (String) -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 8.dp, vertical = 2.dp),
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        listOf("phone" to "📱 Phone", "pc" to "💻 PC").forEach { (key, label) ->
            val selected = key == activeSection
            Surface(
                shape = RoundedCornerShape(10.dp),
                color = if (selected) Lsb.Blurple.copy(alpha = 0.35f) else Lsb.BgDark,
                border = if (selected) {
                    androidx.compose.foundation.BorderStroke(1.5.dp, Lsb.Blurple)
                } else null,
                modifier = Modifier
                    .weight(1f)
                    .clickable { onSelect(key) },
            ) {
                Text(
                    label,
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(vertical = 7.dp),
                    color = if (selected) Lsb.TextPrimary else Lsb.TextMuted,
                    fontSize = 13.sp,
                    fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal,
                    textAlign = TextAlign.Center,
                )
            }
        }
    }
}

@Composable
private fun EmptyPhoneSectionHint(onShowPc: () -> Unit) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("📱", fontSize = 34.sp)
        Text(
            "No Phone tabs yet.\nOn the PC: right-click a tab → turn on " +
                "\"📱 Phone section\" → sync again. New tabs made under 📱 land here too.",
            color = Lsb.TextSecondary,
            textAlign = TextAlign.Center,
            fontSize = 13.sp,
            modifier = Modifier.padding(vertical = 10.dp),
        )
        TextButton(onClick = onShowPc) {
            Text("Show 💻 PC tabs instead", color = Lsb.Blurple)
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun TabChipsRow(
    tabs: List<IndexedValue<com.romerez.lsbmobile.data.SoundTab>>,
    currentTab: Int,
    onSelect: (Int) -> Unit,
    onLongPress: (Int) -> Unit = {},
    showNewTab: Boolean = false,
    onNewTab: () -> Unit = {},
) {
    LazyRow(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = 8.dp),
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        lazyRowItems(tabs) { (index, tab) ->
            val selected = index == currentTab
            val accent = parseHexColor(tab.colorHex) ?: Lsb.Blurple
            Surface(
                shape = RoundedCornerShape(16.dp),
                color = if (selected) accent.copy(alpha = 0.28f) else Lsb.BgMedium,
                border = if (selected) {
                    androidx.compose.foundation.BorderStroke(1.5.dp, accent)
                } else null,
                modifier = Modifier.combinedClickable(
                    onClick = { onSelect(index) },
                    onLongClick = { onLongPress(index) },
                ),
            ) {
                Text(
                    text = listOfNotNull(tab.emoji, tab.name).joinToString(" "),
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                    color = if (selected) Lsb.TextPrimary else Lsb.TextSecondary,
                    fontSize = 13.sp,
                    fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal,
                    maxLines = 1,
                    style = TextStyle(textDirection = TextDirection.Content),
                )
            }
        }
        if (showNewTab) {
            lazyRowItems(listOf("new-tab")) {
                Surface(
                    shape = RoundedCornerShape(16.dp),
                    color = Lsb.BgDark,
                    border = androidx.compose.foundation.BorderStroke(1.dp, Lsb.Border),
                    modifier = Modifier.clickable(onClick = onNewTab),
                ) {
                    Text(
                        "＋ tab",
                        modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                        color = Lsb.Green,
                        fontSize = 13.sp,
                        fontWeight = FontWeight.SemiBold,
                    )
                }
            }
        }
    }
}

// ---------- slot tiles ----------

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun SlotTile(
    slot: SoundSlot,
    playable: Boolean,
    progress: Float?,
    imageFile: java.io.File?,
    onTap: () -> Unit,
    onStop: () -> Unit,
    onLongPress: () -> Unit = {},
) {
    val accent = parseHexColor(slot.colorHex) ?: Lsb.BgLight
    Box(
        modifier = Modifier
            .aspectRatio(1f)
            .clip(RoundedCornerShape(12.dp))
            .background(accent.copy(alpha = if (playable) 0.9f else 0.35f))
            // long-press works even on dangling refs (rename etc.)
            .combinedClickable(
                onClick = { if (playable) onTap() },
                onLongClick = onLongPress,
            ),
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(6.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            if (imageFile != null) {
                AsyncImage(
                    model = imageFile,
                    contentDescription = null,
                    modifier = Modifier
                        .weight(1f)
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(8.dp)),
                )
            } else {
                Spacer(Modifier.weight(1f))
            }
            Text(
                text = slot.name,
                color = Lsb.TextPrimary,
                fontSize = 12.sp,
                fontWeight = FontWeight.SemiBold,
                textAlign = TextAlign.Center,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
                // BiDi: NEVER pre-reorder — logical order in, Compose renders
                // (../docs/HEBREW_RTL.md; the desktop's recurring bug).
                style = TextStyle(textDirection = TextDirection.Content),
                modifier = Modifier.fillMaxWidth(),
            )
            if (imageFile == null) Spacer(Modifier.weight(1f))
        }

        slot.emoji?.let {
            Text(
                it,
                fontSize = 16.sp,
                modifier = Modifier
                    .align(Alignment.TopStart)
                    .padding(4.dp),
            )
        }
        if (!playable) {
            Text(
                "⚠",
                fontSize = 14.sp,
                modifier = Modifier
                    .align(Alignment.TopEnd)
                    .padding(4.dp),
            )
        }
        if (progress != null) {
            // playing: progress strip + stop overlay (desktop parity)
            Box(
                modifier = Modifier
                    .align(Alignment.BottomStart)
                    .fillMaxWidth(progress.coerceIn(0.02f, 1f))
                    .height(4.dp)
                    .background(Lsb.Playing),
            )
            Box(
                modifier = Modifier
                    .align(Alignment.TopEnd)
                    .padding(2.dp)
                    .size(26.dp)
                    .clip(CircleShape)
                    .background(Lsb.BgDarkest.copy(alpha = 0.65f))
                    .clickable(onClick = onStop),
                contentAlignment = Alignment.Center,
            ) {
                Text("⏹", color = Lsb.Red, fontSize = 12.sp)
            }
        }
    }
}

@Composable
private fun EmptySlotTile(showAdd: Boolean = false, onClick: (() -> Unit)? = null) {
    Box(
        modifier = Modifier
            .aspectRatio(1f)
            .clip(RoundedCornerShape(12.dp))
            .background(Lsb.BgDark)
            .border(1.dp, Lsb.Border, RoundedCornerShape(12.dp))
            .let { if (onClick != null) it.clickable(onClick = onClick) else it },
        contentAlignment = Alignment.Center,
    ) {
        if (showAdd) {
            Text("＋", color = Lsb.TextMuted, fontSize = 22.sp)
        }
    }
}

// ---------- search results ----------

@Composable
private fun SearchResultsGrid(
    vm: AppViewModel,
    hits: List<LibraryRepository.SearchHit>,
    columns: Int,
    playing: List<PlayerPool.PlayingSound>,
    onEdit: (LibraryRepository.SearchHit) -> Unit = {},
    modifier: Modifier = Modifier,
) {
    val lib = vm.repo.library.collectAsStateWithLifecycle().value ?: return
    Column(modifier.fillMaxWidth()) {
        Text(
            "${hits.size} results across all tabs",
            color = Lsb.TextMuted,
            fontSize = 12.sp,
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 2.dp),
        )
        LazyVerticalGrid(
            columns = GridCells.Fixed(columns),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(8.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            items(hits, key = { "s${it.tabIndex}:${it.slotIndex}" }) { hit ->
                Column {
                    SlotTile(
                        slot = hit.slot,
                        playable = lib.playable[hit.slot.filePath] == true,
                        progress = playing
                            .filter { it.slotId == "t${hit.tabIndex}:${hit.slotIndex}" }
                            .maxByOrNull { it.playbackId }?.progress,
                        imageFile = hit.slot.imagePath?.let { vm.repo.resolve(it) },
                        onTap = { vm.playSlot(hit.tabIndex, hit.slotIndex, hit.slot) },
                        onStop = { vm.stopSlot(hit.tabIndex, hit.slotIndex) },
                        onLongPress = { onEdit(hit) },
                    )
                    Text(
                        hit.tabName,
                        color = Lsb.TextMuted,
                        fontSize = 10.sp,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        style = TextStyle(textDirection = TextDirection.Content),
                        modifier = Modifier.padding(start = 4.dp, top = 2.dp),
                    )
                }
            }
        }
    }
}

// ---------- now playing ----------

@Composable
private fun NowPlayingBar(
    playing: List<PlayerPool.PlayingSound>,
    onStopAll: () -> Unit,
) {
    if (playing.isEmpty()) return
    var expanded by remember { mutableStateOf(false) }
    Surface(color = Lsb.BgMedium) {
        Column(Modifier.fillMaxWidth()) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clickable { expanded = !expanded }
                    .padding(horizontal = 12.dp, vertical = 6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    "♪ ${playing.size} playing",
                    color = Lsb.Playing,
                    fontSize = 13.sp,
                    fontWeight = FontWeight.SemiBold,
                    modifier = Modifier.weight(1f),
                )
                TextButton(onClick = onStopAll) {
                    Text("⏹ Stop all", color = Lsb.Red, fontSize = 13.sp)
                }
            }
            if (expanded) {
                playing.sortedByDescending { it.playbackId }.forEach { sound ->
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 12.dp, vertical = 3.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(
                            (if (sound.looping) "🔁 " else "") + sound.name,
                            color = Lsb.TextSecondary,
                            fontSize = 12.sp,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                            style = TextStyle(textDirection = TextDirection.Content),
                            modifier = Modifier.weight(1f),
                        )
                        Box(
                            modifier = Modifier
                                .padding(start = 8.dp)
                                .fillMaxWidth(0.25f)
                                .height(3.dp)
                                .background(Lsb.BgLight),
                        ) {
                            Box(
                                Modifier
                                    .fillMaxWidth(sound.progress)
                                    .fillMaxHeight()
                                    .background(Lsb.Playing),
                            )
                        }
                    }
                }
                Spacer(Modifier.height(6.dp))
            }
        }
    }
}

// ---------- misc ----------

@Composable
private fun EmptyLibraryHint(loadFailed: Boolean, onOpenSync: () -> Unit) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("🎵", fontSize = 42.sp)
        Text(
            if (loadFailed) {
                "The saved library could not be read.\nImport a SoundPack to recover — the broken file is kept in backups/."
            } else {
                "No sounds yet.\nExport a SoundPack (.zip) from the desktop app's 📱 button, then import it here."
            },
            color = Lsb.TextSecondary,
            textAlign = TextAlign.Center,
            fontSize = 14.sp,
            modifier = Modifier.padding(vertical = 12.dp),
        )
        TextButton(onClick = onOpenSync) {
            Text("📥 Open Sync / Import", color = Lsb.Blurple)
        }
    }
}

@Composable
private fun SlotEditDialog(
    target: EditTarget,
    onSave: (
        name: String, volume: Float, speed: Float, preservePitch: Boolean,
        loop: Boolean, loopCount: Int, loopDelay: Float,
    ) -> Unit,
    onDismiss: () -> Unit,
) {
    val slot = target.slot
    var name by remember { mutableStateOf(slot.name) }
    var volumePct by remember { mutableStateOf((slot.volume * 100).toInt()) }
    var speedPct by remember { mutableStateOf((slot.speed * 100).toInt()) }
    var preservePitch by remember { mutableStateOf(slot.preservePitch) }
    var loop by remember { mutableStateOf(slot.loop) }
    var loopCount by remember { mutableStateOf(slot.loopCount.coerceIn(0, 10)) }
    var loopDelay by remember { mutableStateOf(slot.loopDelaySeconds) }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Edit sound") },
        confirmButton = {
            TextButton(onClick = {
                onSave(name.trim().ifEmpty { slot.name }, volumePct / 100f,
                    speedPct / 100f, preservePitch, loop, loopCount, loopDelay)
            }) { Text("Save", color = Lsb.Green) }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("Cancel", color = Lsb.TextMuted) }
        },
        text = {
            Column {
                if (target.desktopOwned) {
                    Text(
                        "⚠ This sound comes from the PC — the next sync will " +
                            "overwrite phone edits.",
                        color = Lsb.Yellow, fontSize = 12.sp,
                        modifier = Modifier.padding(bottom = 8.dp),
                    )
                }
                OutlinedTextField(
                    value = name,
                    onValueChange = { name = it },
                    label = { Text("Name") },
                    singleLine = true,
                    textStyle = TextStyle(
                        color = Lsb.TextPrimary,
                        textDirection = TextDirection.Content, // Hebrew names
                    ),
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(10.dp))
                Text("🔊 Volume: $volumePct%", color = Lsb.TextSecondary, fontSize = 13.sp)
                Slider(
                    value = volumePct.toFloat(),
                    onValueChange = { volumePct = it.toInt() },
                    valueRange = 0f..200f,
                )
                Text("⏩ Speed: ${speedPct / 100f}×", color = Lsb.TextSecondary, fontSize = 13.sp)
                Slider(
                    value = speedPct.toFloat(),
                    onValueChange = { speedPct = it.toInt() },
                    valueRange = 50f..200f,
                )
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Switch(checked = preservePitch, onCheckedChange = { preservePitch = it })
                    Text("Keep pitch (off = chipmunk)", color = Lsb.TextSecondary,
                        fontSize = 13.sp, modifier = Modifier.padding(start = 8.dp))
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Switch(checked = loop, onCheckedChange = { loop = it })
                    Text("🔁 Loop", color = Lsb.TextSecondary, fontSize = 13.sp,
                        modifier = Modifier.padding(start = 8.dp))
                }
                if (loop) {
                    Text(
                        "Repeats: " + (if (loopCount == 0) "∞" else "$loopCount"),
                        color = Lsb.TextSecondary, fontSize = 13.sp,
                    )
                    Slider(
                        value = loopCount.toFloat(),
                        onValueChange = { loopCount = it.toInt() },
                        valueRange = 0f..10f,
                        steps = 9,
                    )
                    Text(
                        "Gap between loops: %.1f s".format(loopDelay),
                        color = Lsb.TextSecondary, fontSize = 13.sp,
                    )
                    Slider(
                        value = loopDelay,
                        onValueChange = { loopDelay = it },
                        valueRange = 0f..5f,
                    )
                }
            }
        },
    )
}

@Composable
private fun SettingsDialog(vm: AppViewModel, onDismiss: () -> Unit) {
    val columns by vm.gridColumns.collectAsStateWithLifecycle()
    val masterPct by vm.masterVolumePct.collectAsStateWithLifecycle()
    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = { TextButton(onClick = onDismiss) { Text("Done") } },
        title = { Text("Settings") },
        text = {
            Column {
                Text("Grid columns: $columns", color = Lsb.TextSecondary)
                Slider(
                    value = columns.toFloat(),
                    onValueChange = { vm.setGridColumns(it.toInt()) },
                    valueRange = 2f..6f,
                    steps = 3,
                )
                Spacer(Modifier.height(8.dp))
                Text("🎵 Sounds volume: $masterPct%", color = Lsb.TextSecondary)
                Slider(
                    value = masterPct.toFloat(),
                    onValueChange = { vm.setMasterVolumePct(it.toInt()) },
                    valueRange = 0f..150f,
                )
            }
        },
    )
}
