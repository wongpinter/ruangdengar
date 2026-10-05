package com.ruangdengar.android.design.molecules

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Favorite
import androidx.compose.material.icons.outlined.FavoriteBorder
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import com.ruangdengar.android.design.atoms.*
import com.ruangdengar.android.design.foundations.Space
import com.ruangdengar.android.domain.Book

@Composable fun BookRow(book: Book, onOpen: () -> Unit, onFavorite: () -> Unit) {
    Row(Modifier.fillMaxWidth().padding(vertical = Space.md), horizontalArrangement = Arrangement.spacedBy(Space.md)) {
        CoverArtwork(book)
        Column(Modifier.weight(1f).heightIn(min = Space.touch).clickable(onClick = onOpen).padding(vertical = Space.sm)) {
            Text(book.title, style = MaterialTheme.typography.titleMedium)
            SupportingText(book.author.ifBlank { "Penulis belum diketahui" })
            SupportingText(if (book.audioUri == null) "Contoh desain · tanpa audio" else "Audio lokal · tersedia di perangkat")
        }
        IconAction(if (book.favorite) Icons.Filled.Favorite else Icons.Outlined.FavoriteBorder,
            if (book.favorite) "Hapus ${book.title} dari favorit" else "Favoritkan ${book.title}", onFavorite)
    }
    HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
}
@Composable fun MetadataField(label: String, value: String, onChange: (String) -> Unit, error: String? = null, multiline: Boolean = false) {
    OutlinedTextField(value, onChange, Modifier.fillMaxWidth(), label = { Text(label) }, isError = error != null,
        singleLine = !multiline, minLines = if (multiline) 3 else 1,
        supportingText = error?.let { { Text(it) } }, shape = MaterialTheme.shapes.small)
}
@Composable fun SettingRow(title: String, supporting: String, onClick: () -> Unit) {
    Column(Modifier.fillMaxWidth().heightIn(min = Space.touch).clickable(onClick = onClick).padding(vertical = Space.md)) {
        Text(title, style = MaterialTheme.typography.titleMedium)
        SupportingText(supporting)
    }
    HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
}
