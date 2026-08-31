package com.romerez.lsbmobile.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

/**
 * The desktop app's Discord-dark palette, ported verbatim from
 * soundboard/constants.py `DiscordColors` so the two apps read as siblings
 * (mobile/README.md §7.2). Dark-only by design; dynamic color off.
 */
object Lsb {
    val BgDarkest = Color(0xFF1E1F22)
    val BgDark = Color(0xFF2B2D31)
    val BgMedium = Color(0xFF313338)
    val BgLight = Color(0xFF3F4147)
    val BgLighter = Color(0xFF4E5058)
    val Blurple = Color(0xFF5865F2)
    val BlurpleHover = Color(0xFF4752C4)
    val Green = Color(0xFF23A559)
    val Red = Color(0xFFDA373C)
    val Yellow = Color(0xFFF0B232)
    val Playing = Color(0xFFF5A623)
    val TextPrimary = Color(0xFFF2F3F5)
    val TextSecondary = Color(0xFFB5BAC1)
    val TextMuted = Color(0xFF80848E)
    val Border = Color(0xFF3F4147)
}

private val DarkScheme = darkColorScheme(
    primary = Lsb.Blurple,
    onPrimary = Lsb.TextPrimary,
    primaryContainer = Lsb.BlurpleHover,
    onPrimaryContainer = Lsb.TextPrimary,
    secondary = Lsb.BgLighter,
    onSecondary = Lsb.TextPrimary,
    background = Lsb.BgDarkest,
    onBackground = Lsb.TextPrimary,
    surface = Lsb.BgDark,
    onSurface = Lsb.TextPrimary,
    surfaceVariant = Lsb.BgMedium,
    onSurfaceVariant = Lsb.TextSecondary,
    outline = Lsb.Border,
    error = Lsb.Red,
    onError = Lsb.TextPrimary,
)

@Composable
fun LsbTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = DarkScheme, content = content)
}

/** Slot/tab accent colors arrive as "#RRGGBB" strings from the config. */
fun parseHexColor(hex: String?): Color? {
    if (hex == null) return null
    val h = hex.trim().removePrefix("#")
    if (h.length != 6) return null
    return runCatching { Color(0xFF000000 or h.toLong(16)) }.getOrNull()
}
