package com.ruangdengar.android.design.organisms

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.Alignment
import com.ruangdengar.android.design.atoms.*
import com.ruangdengar.android.design.foundations.Space
import com.ruangdengar.android.domain.Book
import com.ruangdengar.android.domain.formatTime

@Composable fun MiniPlayer(book: Book, playing: Boolean, position: Long, onOpen: () -> Unit, onToggle: () -> Unit) {
    Row(Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.primaryContainer).padding(horizontal = Space.md, vertical = Space.sm),
        verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f).heightIn(min = Space.touch).clickable(onClick = onOpen)) {
            Text(book.title, style = MaterialTheme.typography.titleMedium)
            Text("Audio lokal · ${formatTime(position)}", style = MaterialTheme.typography.bodyMedium)
        }
        IconAction(if (playing) Icons.Filled.Pause else Icons.Filled.PlayArrow, if (playing) "Jeda" else "Lanjutkan", onToggle)
    }
}
