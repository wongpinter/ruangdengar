package com.ruangdengar.android.data

import android.content.Context
import com.ruangdengar.android.domain.Book
import org.json.JSONArray
import org.json.JSONObject

/** Local-first prototype store. Browser cookies or tokens are never stored here. */
class LocalLibrary(context: Context) {
    private val prefs = context.getSharedPreferences("local_library", Context.MODE_PRIVATE)
    val samples = listOf(
        Book("demo-teras", "Filosofi Teras", "Henry Manampiring", "Filsafat Stoa untuk menghadapi kecemasan dan menjalani kehidupan sehari-hari.", palette = 0),
        Book("demo-laut", "Laut Bercerita", "Leila S. Chudori", "Contoh metadata untuk mengeksplorasi desain perpustakaan.", palette = 1),
        Book("demo-bumi", "Bumi Manusia", "Pramoedya Ananta Toer", palette = 2),
        Book("demo-seni", "Sebuah Seni untuk Bersikap Bodo Amat", "Mark Manson", palette = 3),
    )
    fun load(): List<Book> = runCatching {
        val array = JSONArray(prefs.getString("books", null) ?: return samples)
        (0 until array.length()).map { i ->
            val o = array.getJSONObject(i)
            Book(o.getString("id"), o.getString("title"), o.optString("author"), o.optString("description"),
                o.optString("audioUri").takeIf { it.isNotBlank() }, o.optBoolean("favorite"),
                o.optLong("positionMs"), o.optBoolean("completed"), o.optInt("palette"))
        }
    }.getOrElse { samples }
    fun save(books: List<Book>) {
        val array = JSONArray()
        books.forEach { b -> array.put(JSONObject().apply {
            put("id", b.id); put("title", b.title); put("author", b.author); put("description", b.description)
            put("audioUri", b.audioUri ?: ""); put("favorite", b.favorite); put("positionMs", b.positionMs)
            put("completed", b.completed); put("palette", b.palette)
        }) }
        prefs.edit().putString("books", array.toString()).apply()
    }
    var darkMode: Boolean
        get() = prefs.getBoolean("darkMode", false)
        set(value) { prefs.edit().putBoolean("darkMode", value).apply() }
    var server: String
        get() = prefs.getString("server", "") ?: ""
        set(value) { prefs.edit().putString("server", value).apply() }
    fun addBookmark(bookId: String, position: Long) {
        val key = "bookmarks:$bookId"
        val list = bookmarks(bookId) + position
        prefs.edit().putString(key, JSONArray(list.distinct()).toString()).apply()
    }
    fun bookmarks(bookId: String): List<Long> = runCatching {
        val a = JSONArray(prefs.getString("bookmarks:$bookId", "[]"))
        (0 until a.length()).map { a.getLong(it) }
    }.getOrDefault(emptyList())
}
