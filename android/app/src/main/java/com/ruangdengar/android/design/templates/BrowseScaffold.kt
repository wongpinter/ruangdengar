package com.ruangdengar.android.design.templates

import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import com.ruangdengar.android.design.atoms.IconAction

@OptIn(ExperimentalMaterial3Api::class)
@Composable fun BrowseScaffold(title: String, back: (() -> Unit)? = null, bottom: @Composable () -> Unit = {}, content: @Composable (PaddingValues) -> Unit) {
    Scaffold(topBar = { TopAppBar(title = { Text(title) }, navigationIcon = {
        if (back != null) IconAction(Icons.AutoMirrored.Filled.ArrowBack, "Kembali", back)
    }) }, bottomBar = bottom, content = content)
}

data class Destination(val route: String, val label: String, val icon: ImageVector)
@Composable fun MainNavigation(destinations: List<Destination>, selected: String, onNavigate: (String) -> Unit) {
    NavigationBar { destinations.forEach { destination ->
        NavigationBarItem(selected = destination.route == selected, onClick = { onNavigate(destination.route) },
            icon = { Icon(destination.icon, null) }, label = { Text(destination.label) })
    } }
}
