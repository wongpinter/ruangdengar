package com.ruangdengar.android.design.atoms

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import com.ruangdengar.android.domain.Book
import com.ruangdengar.android.design.foundations.Space

@Composable fun ActionButton(label: String, onClick: () -> Unit, modifier: Modifier = Modifier, enabled: Boolean = true) {
    Button(onClick, modifier.heightIn(min = Space.touch), enabled = enabled, shape = MaterialTheme.shapes.small) { Text(label) }
}
@Composable fun IconAction(icon: ImageVector, label: String, onClick: () -> Unit, enabled: Boolean = true) {
    IconButton(onClick, Modifier.size(Space.touch), enabled) { Icon(icon, label) }
}
@Composable fun CoverArtwork(book: Book, modifier: Modifier = Modifier, large: Boolean = false) {
    val colors = listOf(Color(0xFFBD8042), Color(0xFF425770), Color(0xFF8B4335), Color(0xFFD9CCAD))
    val text = if (book.palette % 4 in listOf(0,3)) Color(0xFF242920) else Color(0xFFFFFDF7)
    Column(modifier.width(if (large) 180.dp else 64.dp).heightIn(min = if (large) 240.dp else 96.dp)
        .clip(RoundedCornerShape(if (large) 12.dp else 6.dp)).background(colors[Math.floorMod(book.palette,4)])
        .semantics { contentDescription = "Cover ilustratif: ${book.title}" }.padding(if (large) 20.dp else 8.dp),
        verticalArrangement = Arrangement.SpaceBetween) {
        Text("RUANGDENGAR", style = MaterialTheme.typography.labelSmall, color = text)
        Text(book.title, style = if (large) MaterialTheme.typography.headlineMedium else MaterialTheme.typography.bodyMedium,
            fontFamily = FontFamily.Serif, color = text)
    }
}
@Composable fun SupportingText(text: String, modifier: Modifier = Modifier) {
    Text(text, modifier, color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodyMedium)
}
