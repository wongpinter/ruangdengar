package com.ruangdengar.android.playback

import android.content.ComponentName
import android.content.Context
import android.os.Bundle
import androidx.core.content.ContextCompat
import androidx.media3.common.*
import androidx.media3.session.*
import com.ruangdengar.android.domain.Book
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow

data class PlaybackState(val bookId: String? = null, val playing: Boolean = false, val positionMs: Long = 0,
    val durationMs: Long = 0, val speed: Float = 1f, val error: String? = null, val connected: Boolean = false)

class PlaybackConnection(private val context: Context) {
    private val mutable = MutableStateFlow(PlaybackState())
    val state = mutable.asStateFlow()
    private val future = MediaController.Builder(context, SessionToken(context, ComponentName(context, PlaybackService::class.java))).buildAsync()
    private var controller: MediaController? = null
    init {
        future.addListener({
            runCatching { future.get() }.onSuccess { c ->
                controller = c
                c.addListener(object : Player.Listener {
                    override fun onEvents(player: Player, events: Player.Events) { refresh() }
                    override fun onPlayerError(error: PlaybackException) { mutable.value = mutable.value.copy(error = "Audio tidak bisa diputar. Pilih ulang file jika akses telah berubah.") }
                })
                refresh()
            }.onFailure { mutable.value = mutable.value.copy(error = "Pemutar belum terhubung. Buka ulang aplikasi untuk mencoba lagi.") }
        }, ContextCompat.getMainExecutor(context))
    }
    fun refresh() {
        controller?.let { c -> mutable.value = PlaybackState(c.currentMediaItem?.mediaId, c.isPlaying,
            c.currentPosition.coerceAtLeast(0), c.duration.takeIf { it != C.TIME_UNSET }?.coerceAtLeast(0) ?: 0,
            c.playbackParameters.speed, c.playerError?.let { "Pemutaran terhenti. Coba lagi atau pilih ulang file." }, true) }
    }
    fun play(book: Book): Boolean {
        val c = controller ?: return false
        val uri = book.audioUri ?: return false
        if (c.currentMediaItem?.mediaId == book.id) { c.play(); refresh(); return true }
        val position = context.getSharedPreferences("local_playback", Context.MODE_PRIVATE).getLong("position:${book.id}", 0)
        c.setMediaItem(MediaItem.Builder().setMediaId(book.id).setUri(uri).setMediaMetadata(
            MediaMetadata.Builder().setTitle(book.title).setArtist(book.author).build()).build(), position)
        c.prepare(); c.play(); refresh(); return true
    }
    fun toggle() { controller?.let { if (it.isPlaying) it.pause() else it.play() }; refresh() }
    fun seek(position: Long) { controller?.seekTo(position.coerceAtLeast(0)); refresh() }
    fun back() { controller?.seekBack(); refresh() }
    fun forward() { controller?.seekForward(); refresh() }
    fun speed(value: Float) { controller?.setPlaybackSpeed(value.coerceIn(.5f,3f)); refresh() }
    fun timer(minutes: Int) { controller?.sendCustomCommand(SessionCommand(PlaybackService.SLEEP_COMMAND, Bundle.EMPTY), Bundle().apply { putInt("minutes",minutes) }) }
    fun retry() { controller?.prepare(); controller?.play() }
    fun close() { MediaController.releaseFuture(future) }
}
