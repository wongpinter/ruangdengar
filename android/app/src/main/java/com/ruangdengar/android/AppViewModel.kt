package com.ruangdengar.android

import android.app.Application
import android.content.Intent
import android.net.Uri
import android.provider.OpenableColumns
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.ruangdengar.android.data.LocalLibrary
import com.ruangdengar.android.domain.*
import com.ruangdengar.android.playback.PlaybackConnection
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.util.UUID

class AppViewModel(application: Application) : AndroidViewModel(application) {
    private val library = LocalLibrary(application)
    private val mutableBooks = MutableStateFlow(library.load())
    val books = mutableBooks.asStateFlow()
    private val mutableDark = MutableStateFlow(library.darkMode)
    val dark = mutableDark.asStateFlow()
    val playback = PlaybackConnection(application)
    val server = MutableStateFlow(library.server)
    init { viewModelScope.launch { while (true) { playback.refresh(); delay(500) } } }
    private fun update(book: Book) { mutableBooks.value = books.value.map { if (it.id == book.id) book else it }; library.save(books.value) }
    fun favorite(book: Book) = update(book.copy(favorite = !book.favorite))
    fun metadata(id: String, draft: MetadataDraft) { books.value.find { it.id == id }?.let { update(draft.applyTo(it)) } }
    fun theme() { mutableDark.value = !dark.value; library.darkMode = dark.value }
    fun saveServer(input: String): Boolean {
        val value = normalizedServerUrl(input) ?: return false
        server.value = value; library.server = value; return true
    }
    fun importAudio(uri: Uri): String? = runCatching {
        val resolver = getApplication<Application>().contentResolver
        resolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
        val name = resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
            if (c.moveToFirst()) c.getString(0) else null
        } ?: "Audiobook lokal"
        val duplicate = books.value.find { it.audioUri == uri.toString() }
        if (duplicate != null) return duplicate.id
        val book = Book(UUID.randomUUID().toString(), name.substringBeforeLast('.', name), "Audio lokal", audioUri = uri.toString())
        mutableBooks.value = books.value + book; library.save(books.value); book.id
    }.getOrNull()
    fun bookmark(bookId: String, position: Long) = library.addBookmark(bookId, position)
    fun bookmarks(bookId: String) = library.bookmarks(bookId)
    override fun onCleared() { playback.close(); super.onCleared() }
}
