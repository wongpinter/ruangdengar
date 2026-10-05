package com.ruangdengar.android

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.ruangdengar.android.design.foundations.RuangTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            val model: AppViewModel = viewModel()
            val dark = model.dark.collectAsStateWithLifecycle()
            RuangTheme(dark.value) { RuangApp(model) }
        }
    }
}
