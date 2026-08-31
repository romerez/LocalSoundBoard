package com.romerez.lsbmobile

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.romerez.lsbmobile.ui.BoardScreen
import com.romerez.lsbmobile.ui.SyncScreen
import com.romerez.lsbmobile.ui.TrimScreen
import com.romerez.lsbmobile.ui.theme.LsbTheme
import kotlinx.coroutines.flow.MutableStateFlow

class MainActivity : ComponentActivity() {

    companion object {
        /** Audio shared from other apps; consumed by AppNav once the VM exists. */
        val pendingShare = MutableStateFlow<List<Uri>?>(null)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        handleShareIntent(intent)
        setContent {
            LsbTheme {
                AppNav()
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShareIntent(intent)
    }

    private fun handleShareIntent(intent: Intent?) {
        intent ?: return
        val uris: List<Uri> = when (intent.action) {
            Intent.ACTION_SEND ->
                listOfNotNull(intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java))
            Intent.ACTION_SEND_MULTIPLE ->
                intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
                    ?.filterNotNull() ?: emptyList()
            else -> emptyList()
        }
        if (uris.isNotEmpty()) pendingShare.value = uris
    }
}

@Composable
private fun AppNav(vm: AppViewModel = viewModel()) {
    val nav = rememberNavController()

    LaunchedEffect(Unit) {
        MainActivity.pendingShare.collect { uris ->
            if (!uris.isNullOrEmpty()) {
                MainActivity.pendingShare.value = null
                vm.handleSharedUris(uris)
            }
        }
    }

    NavHost(navController = nav, startDestination = "board") {
        composable("board") {
            BoardScreen(
                vm = vm,
                onOpenSync = { nav.navigate("sync") },
                onScanSync = { nav.navigate("sync_scan") },
                onOpenTrim = { tab, slot -> nav.navigate("trim/$tab/$slot") },
            )
        }
        composable("sync") {
            SyncScreen(vm = vm, onBack = { nav.popBackStack() })
        }
        // 📷 on the board: same screen, scanner fires immediately.
        composable("sync_scan") {
            SyncScreen(vm = vm, onBack = { nav.popBackStack() }, autoScan = true)
        }
        composable(
            "trim/{tab}/{slot}",
            arguments = listOf(
                navArgument("tab") { type = NavType.IntType },
                navArgument("slot") { type = NavType.IntType },
            ),
        ) { entry ->
            TrimScreen(
                vm = vm,
                tabIndex = entry.arguments?.getInt("tab") ?: 0,
                slotIndex = entry.arguments?.getInt("slot") ?: 0,
                onBack = { nav.popBackStack() },
            )
        }
    }
}
