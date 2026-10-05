package com.ruangdengar.android.design.foundations

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

object Space { val xs = 4.dp; val sm = 8.dp; val md = 12.dp; val lg = 16.dp; val page = 20.dp; val xl = 24.dp; val touch = 48.dp }
private val Light = lightColorScheme(
    primary = Color(0xFF8B460A), onPrimary = Color(0xFFFFFDF7), primaryContainer = Color(0xFFF4E6CE), onPrimaryContainer = Color(0xFF653407),
    background = Color(0xFFF7F4EC), onBackground = Color(0xFF242920), surface = Color(0xFFFFFDF7), onSurface = Color(0xFF242920),
    onSurfaceVariant = Color(0xFF62675D), outline = Color(0xFF787E70), outlineVariant = Color(0xFFDCDED3), error = Color(0xFFA52E2E),
)
private val Dark = darkColorScheme(
    primary = Color(0xFFEFB775), onPrimary = Color(0xFF27241F), primaryContainer = Color(0xFF423321), onPrimaryContainer = Color(0xFFF3DAB6),
    background = Color(0xFF1E241F), onBackground = Color(0xFFF4F2E8), surface = Color(0xFF282F29), onSurface = Color(0xFFF4F2E8),
    onSurfaceVariant = Color(0xFFB7BDB0), outline = Color(0xFF89917F), outlineVariant = Color(0xFF485046), error = Color(0xFFFFB8AD),
)
private val Type = Typography(
    headlineLarge = TextStyle(fontFamily = FontFamily.Serif, fontSize = 30.sp, lineHeight = 36.sp),
    headlineMedium = TextStyle(fontFamily = FontFamily.Serif, fontSize = 28.sp, lineHeight = 34.sp),
    titleLarge = TextStyle(fontSize = 20.sp, lineHeight = 28.sp, fontWeight = FontWeight.SemiBold),
    titleMedium = TextStyle(fontSize = 16.sp, lineHeight = 24.sp, fontWeight = FontWeight.SemiBold),
    bodyLarge = TextStyle(fontSize = 16.sp, lineHeight = 24.sp),
    bodyMedium = TextStyle(fontSize = 14.sp, lineHeight = 20.sp),
    labelLarge = TextStyle(fontSize = 14.sp, lineHeight = 20.sp, fontWeight = FontWeight.SemiBold),
    labelSmall = TextStyle(fontSize = 12.sp, lineHeight = 18.sp),
)
@Composable fun RuangTheme(dark: Boolean, content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (dark) Dark else Light, typography = Type,
        shapes = Shapes(small = RoundedCornerShape(12.dp), medium = RoundedCornerShape(16.dp), large = RoundedCornerShape(20.dp)), content = content)
}
