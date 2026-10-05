package com.ruangdengar.android.domain

data class Book(
    val id: String,
    val title: String,
    val author: String,
    val description: String = "",
    val audioUri: String? = null,
    val favorite: Boolean = false,
    val positionMs: Long = 0,
    val completed: Boolean = false,
    val palette: Int = 0,
)

data class MetadataDraft(val title: String, val author: String, val description: String) {
    fun validate(): String? = if (title.isBlank()) "Judul wajib diisi." else null
    fun applyTo(book: Book): Book {
        require(validate() == null)
        return book.copy(title = title.trim(), author = author.trim(), description = description.trim())
    }
}

fun formatTime(milliseconds: Long): String {
    val seconds = milliseconds.coerceAtLeast(0) / 1000
    return if (seconds >= 3600) "%d:%02d:%02d".format(seconds / 3600, seconds / 60 % 60, seconds % 60)
    else "%d:%02d".format(seconds / 60, seconds % 60)
}

fun normalizedServerUrl(input: String): String? = try {
    val uri = java.net.URI(input.trim())
    if (uri.scheme != "https" || uri.host.isNullOrBlank() || uri.userInfo != null || uri.query != null || uri.fragment != null) null
    else uri.toString().trimEnd('/')
} catch (_: Exception) { null }
