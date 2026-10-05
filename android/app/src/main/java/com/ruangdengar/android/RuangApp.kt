package com.ruangdengar.android

import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.automirrored.filled.LibraryBooks
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.compose.*
import com.ruangdengar.android.design.atoms.*
import com.ruangdengar.android.design.foundations.Space
import com.ruangdengar.android.design.molecules.*
import com.ruangdengar.android.design.organisms.MiniPlayer
import com.ruangdengar.android.design.templates.*
import com.ruangdengar.android.domain.*
import com.ruangdengar.android.features.*
import kotlinx.coroutines.launch

@Composable fun RuangApp(model: AppViewModel) {
    val books by model.books.collectAsStateWithLifecycle()
    val playback by model.playback.state.collectAsStateWithLifecycle()
    val nav = rememberNavController()
    val entry by nav.currentBackStackEntryAsState()
    val route = entry?.destination?.route ?: "home"
    val snackbar = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    fun message(text: String) { scope.launch { snackbar.showSnackbar(text) } }
    val importAudio = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) {
            val id = model.importAudio(uri)
            if (id != null) nav.navigate("book/$id") else message("File tidak dapat diakses. Pilih file audio yang tersedia secara lokal.")
        }
    }
    fun import() = importAudio.launch(arrayOf("audio/*"))
    fun open(book: Book) { nav.navigate("book/${Uri.encode(book.id)}") }
    fun play(book: Book) {
        if (model.playback.play(book)) nav.navigate("player/${Uri.encode(book.id)}")
        else message(if (book.audioUri == null) "Buku contoh tidak memiliki audio. Tambahkan file dari ponsel." else "Pemutar sedang terhubung. Coba lagi sebentar.")
    }
    val topLevel = route in listOf("home", "library", "downloads", "settings")
    val selected = when { route == "home" -> "home"; route == "downloads" -> "downloads"; route == "settings" -> "settings"; else -> "library" }
    val title = when {
        route == "home" -> "RuangDengar"; route == "library" -> "Koleksi"; route == "downloads" -> "Audio di perangkat"
        route == "settings" -> "Setelan"; route.startsWith("player") -> "Sedang didengarkan"
        route.startsWith("edit") -> "Edit metadata"; route.startsWith("chapters") -> "Isi buku"
        route.startsWith("bookmarks") -> "Penanda waktu"; route.startsWith("book/") -> "Audiobook"
        route == "server" -> "Hubungkan server"; else -> "RuangDengar"
    }
    val destinations = listOf(Destination("home", "Beranda", Icons.Default.Home),
        Destination("library", "Koleksi", Icons.AutoMirrored.Filled.LibraryBooks),
        Destination("downloads", "Lokal", Icons.Default.Folder), Destination("settings", "Setelan", Icons.Default.Settings))
    val focused = route.startsWith("player") || route.startsWith("edit") || route.startsWith("bookmarks") || route.startsWith("chapters")
    BrowseScaffold(title, if (topLevel) null else ({ nav.popBackStack(); Unit }), bottom = {
        Column {
            SnackbarHost(snackbar)
            if (!focused) books.find { it.id == playback.bookId }?.let { active ->
                MiniPlayer(active, playback.playing, playback.positionMs,
                    { nav.navigate("player/${active.id}") }, { model.playback.toggle() })
            }
            if (topLevel) MainNavigation(destinations, selected) { destination ->
                nav.navigate(destination) {
                    popUpTo(nav.graph.startDestinationId) { saveState = true }
                    launchSingleTop = true; restoreState = true
                }
            }
        }
    }) { padding ->
        NavHost(nav, "home", Modifier.padding(padding)) {
            composable("home") {
                val resume = books.find { it.id == playback.bookId } ?: books.firstOrNull { it.audioUri != null }
                LazyColumn(contentPadding = PaddingValues(Space.page), verticalArrangement = Arrangement.spacedBy(Space.lg)) {
                    item { Text("Lanjutkan cerita Anda.", style = MaterialTheme.typography.headlineMedium) }
                    item { SupportingText("Prototipe lokal · buku contoh belum terhubung ke server") }
                    if (resume != null) item {
                        Surface(shape = MaterialTheme.shapes.large, color = MaterialTheme.colorScheme.surface) {
                            Column(Modifier.padding(Space.lg), verticalArrangement = Arrangement.spacedBy(Space.md)) {
                                Text(resume.title, style = MaterialTheme.typography.headlineMedium)
                                SupportingText(resume.author)
                                ActionButton("Lanjutkan mendengarkan", { play(resume) }, Modifier.fillMaxWidth(), playback.connected)
                            }
                        }
                    }
                    item { ActionButton("Tambahkan audio dari ponsel", { import() }, Modifier.fillMaxWidth()) }
                    item { Text("Koleksi Anda", style = MaterialTheme.typography.titleLarge) }
                    items(books.take(4), key = { it.id }) { book -> BookRow(book, { open(book) }, { model.favorite(book) }) }
                    item { OutlinedButton({ nav.navigate("library") }, Modifier.fillMaxWidth()) { Text("Lihat seluruh koleksi") } }
                }
            }
            composable("library") {
                var query by rememberSaveable { mutableStateOf("") }
                var favorites by rememberSaveable { mutableStateOf(false) }
                val filtered = books.filter { (!favorites || it.favorite) && (it.title.contains(query,true) || it.author.contains(query,true)) }
                LazyColumn(contentPadding = PaddingValues(Space.page)) {
                    item { OutlinedTextField(query, { query = it }, Modifier.fillMaxWidth(), label = { Text("Cari judul atau penulis") }, singleLine = true) }
                    item { Row(horizontalArrangement = Arrangement.spacedBy(Space.sm)) {
                        FilterChip(!favorites, { favorites = false }, { Text("Semua") })
                        FilterChip(favorites, { favorites = true }, { Text("Favorit") })
                    } }
                    item { SupportingText("${filtered.size} buku · judul A–Z") }
                    if (filtered.isEmpty()) item { EmptyState("Belum ditemukan", "Coba kata lain atau hapus filter.") }
                    items(filtered.sortedBy { it.title.lowercase() }, key = { it.id }) { book -> BookRow(book, { open(book) }, { model.favorite(book) }) }
                    item { Spacer(Modifier.height(Space.lg)); ActionButton("Tambahkan audio", { import() }, Modifier.fillMaxWidth()) }
                }
            }
            composable("downloads") {
                val local = books.filter { it.audioUri != null }
                LazyColumn(contentPadding = PaddingValues(Space.page)) {
                    item { Text("Cerita untuk dibawa.", style = MaterialTheme.typography.headlineMedium) }
                    item { SupportingText("File dipilih dari perangkat. Unduhan dari server akan ditambahkan setelah autentikasi mobile tersedia.") }
                    if (local.isEmpty()) item { EmptyState("Belum ada audio lokal", "Tambahkan file dari ponsel untuk mencoba pemutar background.") }
                    items(local, key = { it.id }) { book -> BookRow(book, { open(book) }, { model.favorite(book) }) }
                    item { ActionButton("Pilih audio dari ponsel", { import() }, Modifier.fillMaxWidth()) }
                }
            }
            composable("settings") {
                val dark by model.dark.collectAsStateWithLifecycle()
                LazyColumn(contentPadding = PaddingValues(Space.page)) {
                    item { Text("Mendengarkan", style = MaterialTheme.typography.titleLarge) }
                    item { Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Text("Tema gelap", Modifier.weight(1f)); Switch(dark, { model.theme() })
                    } }
                    item { SettingRow("Server RuangDengar", model.server.collectAsStateWithLifecycle().value.ifBlank { "Belum terhubung" }) { nav.navigate("server") } }
                    item { SettingRow("Audio lokal", "Akses file melalui pemilih Android; tanpa izin seluruh penyimpanan") { nav.navigate("downloads") } }
                    item { SettingRow("Tentang versi ini", "0.1.0 · Compose + Media3 · prototipe lokal") { message("Koleksi, favorit, metadata, posisi, dan penanda lokal disimpan di perangkat.") } }
                    item { Spacer(Modifier.height(Space.xl)); SupportingText("Login native, streaming backend, download server, playlist, dan metadata provider adalah tahap integrasi berikutnya. Cache di server berbeda dari audio di ponsel.") }
                }
            }
            composable("server") { ServerScreen(model.server.collectAsStateWithLifecycle().value, { value ->
                if (model.saveServer(value)) message("Alamat disimpan. Integrasi login native belum tersedia.") else message("Gunakan URL HTTPS tanpa kredensial, query, atau fragment.")
            }) }
            composable("book/{id}") { destination ->
                val book = books.find { it.id == destination.arguments?.getString("id") }
                if (book != null) BookScreen(book, { play(book) }, { model.favorite(book) }, { nav.navigate("edit/${book.id}") },
                    { nav.navigate("chapters/${book.id}") }, { import() }, playback.connected)
                else EmptyState("Buku tidak ditemukan", "Kembali ke koleksi.")
            }
            composable("edit/{id}") { destination ->
                val book = books.find { it.id == destination.arguments?.getString("id") }
                if (book != null) MetadataScreen(book, { draft -> model.metadata(book.id,draft); message("Metadata lokal tersimpan."); nav.popBackStack() }, { nav.popBackStack() })
            }
            composable("player/{id}") { destination ->
                val book = books.find { it.id == playback.bookId } ?: books.find { it.id == destination.arguments?.getString("id") }
                if (book != null) PlayerScreen(book, playback, model.playback,
                    { nav.navigate("chapters/${book.id}") }, { nav.navigate("bookmarks/${book.id}") },
                    { model.bookmark(book.id,playback.positionMs); message("Penanda ditambahkan.") })
            }
            composable("chapters/{id}") { destination ->
                val book = books.find { it.id == destination.arguments?.getString("id") }
                if (book != null) Column(Modifier.padding(Space.page)) {
                    SupportingText("Audio lokal diimpor sebagai satu bagian. Chapter backend belum terhubung.")
                    SettingRow("Bagian 1 · ${book.title}", "Mulai dari posisi tersimpan") { play(book) }
                }
            }
            composable("bookmarks/{id}") { destination ->
                val id = destination.arguments?.getString("id") ?: ""
                var positions by remember(id) { mutableStateOf(model.bookmarks(id)) }
                LazyColumn(contentPadding = PaddingValues(Space.page)) {
                    item { ActionButton("Tandai ${formatTime(playback.positionMs)}", {
                        model.bookmark(id,playback.positionMs); positions = model.bookmarks(id)
                    }, Modifier.fillMaxWidth(), playback.bookId == id) }
                    if (positions.isEmpty()) item { EmptyState("Belum ada penanda", "Simpan bagian yang ingin didengarkan lagi.") }
                    items(positions) { position -> SettingRow(formatTime(position), "Menuju posisi ini") {
                        model.playback.seek(position); nav.popBackStack()
                    } }
                }
            }
        }
    }
}
