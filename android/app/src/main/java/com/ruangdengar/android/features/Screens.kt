package com.ruangdengar.android.features

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import com.ruangdengar.android.design.atoms.*
import com.ruangdengar.android.design.molecules.*
import com.ruangdengar.android.design.foundations.Space
import com.ruangdengar.android.domain.*
import com.ruangdengar.android.playback.*

@Composable fun EmptyState(title: String, description: String) {
    Column(Modifier.fillMaxWidth().padding(vertical = Space.xl), verticalArrangement = Arrangement.spacedBy(Space.md)) {
        Text(title, style = MaterialTheme.typography.headlineMedium); SupportingText(description)
    }
}
@Composable fun BookScreen(book: Book, onPlay: () -> Unit, onFavorite: () -> Unit, onEdit: () -> Unit, onChapters: () -> Unit, onImport: () -> Unit, ready: Boolean) {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(Space.page), verticalArrangement = Arrangement.spacedBy(Space.lg)) {
        Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(Space.md)) {
            CoverArtwork(book, large = true)
            Text(book.title, style = MaterialTheme.typography.headlineMedium)
            SupportingText(book.author.ifBlank { "Penulis belum diketahui" })
        }
        if (book.audioUri != null) ActionButton("Lanjutkan mendengarkan", onPlay, Modifier.fillMaxWidth(), ready)
        else {
            SupportingText("Buku contoh untuk review desain. Belum ada audio atau koneksi ke backend.")
            ActionButton("Tambahkan audio dari ponsel", onImport, Modifier.fillMaxWidth())
        }
        OutlinedButton(onFavorite, Modifier.fillMaxWidth()) { Text(if (book.favorite) "Hapus dari favorit" else "Tambahkan ke favorit") }
        Text("Tentang buku", style = MaterialTheme.typography.titleLarge)
        SupportingText(book.description.ifBlank { "Deskripsi belum tersedia. Anda dapat menambahkannya di editor." })
        SettingRow("Isi buku", if (book.audioUri != null) "1 bagian audio lokal" else "Audio belum tersedia", onChapters)
        SettingRow("Edit metadata", "Perubahan hanya disimpan di ponsel ini", onEdit)
    }
}
@Composable fun MetadataScreen(book: Book, onSave: (MetadataDraft) -> Unit, onClose: () -> Unit) {
    var title by rememberSaveable(book.id) { mutableStateOf(book.title) }
    var author by rememberSaveable(book.id) { mutableStateOf(book.author) }
    var description by rememberSaveable(book.id) { mutableStateOf(book.description) }
    var error by remember { mutableStateOf<String?>(null) }
    var confirm by remember { mutableStateOf(false) }
    val dirty = title != book.title || author != book.author || description != book.description
    BackHandler(dirty) { confirm = true }
    Column(Modifier.fillMaxSize()) {
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(Space.page), verticalArrangement = Arrangement.spacedBy(Space.lg)) {
            SupportingText("Editor lokal. Metadata di server dan file sumber tidak diubah. Import provider membutuhkan API baru.")
            MetadataField("Judul",title,{title=it;error=null},error)
            MetadataField("Penulis",author,{author=it})
            MetadataField("Deskripsi",description,{description=it},multiline=true)
        }
        Surface {
            Column(Modifier.fillMaxWidth().imePadding().padding(Space.page), verticalArrangement = Arrangement.spacedBy(Space.sm)) {
                ActionButton("Simpan perubahan", {
                    val draft = MetadataDraft(title,author,description)
                    error = draft.validate(); if (error == null) onSave(draft)
                }, Modifier.fillMaxWidth())
                TextButton({ if(dirty) confirm=true else onClose() }, Modifier.fillMaxWidth()) { Text("Batalkan") }
            }
        }
    }
    if (confirm) AlertDialog(onDismissRequest={confirm=false},title={Text("Buang draft?")},text={Text("Perubahan belum disimpan. Anda bisa melanjutkan mengedit.")},
        confirmButton={TextButton({confirm=false;onClose()}){Text("Buang draft")}},dismissButton={TextButton({confirm=false}){Text("Lanjutkan edit")}})
}
@Composable fun ServerScreen(initial: String, onSave: (String) -> Unit) {
    var server by rememberSaveable { mutableStateOf(initial) }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(Space.page), verticalArrangement = Arrangement.spacedBy(Space.lg)) {
        Text("RuangDengar Anda", style = MaterialTheme.typography.headlineMedium)
        MetadataField("Alamat server HTTPS",server,{server=it})
        SupportingText("Alamat ini disimpan untuk integrasi berikutnya. Versi ini belum melakukan login atau mengambil koleksi server. Jangan masukkan password atau token ke URL.")
        ActionButton("Simpan alamat",{onSave(server)},Modifier.fillMaxWidth())
    }
}
@Composable fun PlayerScreen(book: Book, state: PlaybackState, connection: PlaybackConnection, onChapters: () -> Unit, onBookmarks: () -> Unit, onBookmark: () -> Unit) {
    var speedDialog by remember { mutableStateOf(false) }
    var timerDialog by remember { mutableStateOf(false) }
    var dragging by remember { mutableStateOf(false) }
    var seek by remember { mutableFloatStateOf(0f) }
    var timerLabel by remember { mutableStateOf("Timer") }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(Space.page), horizontalAlignment=Alignment.CenterHorizontally,
        verticalArrangement=Arrangement.spacedBy(Space.md)) {
        CoverArtwork(book, large=true)
        Text(book.title,style=MaterialTheme.typography.headlineMedium)
        SupportingText(book.author)
        SupportingText("Bagian 1 · audio lokal")
        state.error?.let { message -> Text(message,color=MaterialTheme.colorScheme.error); ActionButton("Coba lagi",{connection.retry()}) }
        Slider(value=if(dragging) seek else state.positionMs.toFloat().coerceIn(0f,state.durationMs.toFloat().coerceAtLeast(1f)),
            onValueChange={dragging=true;seek=it},onValueChangeFinished={connection.seek(seek.toLong());dragging=false},
            valueRange=0f..state.durationMs.toFloat().coerceAtLeast(1f),enabled=state.durationMs>0,
            modifier=Modifier.fillMaxWidth())
        Row(Modifier.fillMaxWidth(),horizontalArrangement=Arrangement.SpaceBetween) {
            SupportingText(formatTime(if(dragging) seek.toLong() else state.positionMs)); SupportingText(formatTime(state.durationMs))
        }
        Row(Modifier.fillMaxWidth(),horizontalArrangement=Arrangement.SpaceEvenly,verticalAlignment=Alignment.CenterVertically) {
            IconAction(Icons.Default.Replay10,"Mundur 15 detik",{connection.back()},state.connected)
            FilledIconButton({connection.toggle()},Modifier.size(Space.touch + Space.xl),enabled=state.connected) {
                Icon(if(state.playing) Icons.Default.Pause else Icons.Default.PlayArrow,if(state.playing) "Jeda" else "Lanjutkan")
            }
            IconAction(Icons.Default.Forward30,"Maju 30 detik",{connection.forward()},state.connected)
        }
        Row(Modifier.fillMaxWidth(),horizontalArrangement=Arrangement.SpaceEvenly) {
            TextButton({speedDialog=true}){Text("${state.speed}×")}
            TextButton({timerDialog=true}){Text(timerLabel)}
            TextButton(onChapters){Text("Isi buku")}
        }
        Row {
            TextButton(onBookmark,enabled=state.bookId==book.id){Text("Tambah penanda")}
            TextButton(onBookmarks){Text("Penanda")}
        }
    }
    if(speedDialog) AlertDialog(onDismissRequest={speedDialog=false},title={Text("Kecepatan")},text={Column {
        listOf(.75f,1f,1.25f,1.5f,1.75f,2f).forEach { speed -> TextButton({connection.speed(speed);speedDialog=false},Modifier.fillMaxWidth()){Text("${speed}×")} }
    }},confirmButton={TextButton({speedDialog=false}){Text("Tutup")}})
    if(timerDialog) AlertDialog(onDismissRequest={timerDialog=false},title={Text("Timer tidur")},text={Column {
        listOf(0,15,30,45,60).forEach { minutes -> TextButton({connection.timer(minutes);timerLabel=if(minutes==0) "Timer" else "$minutes mnt";timerDialog=false},Modifier.fillMaxWidth()){Text(if(minutes==0) "Matikan timer" else "$minutes menit")} }
    }},confirmButton={TextButton({timerDialog=false}){Text("Tutup")}})
}
