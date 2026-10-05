package com.ruangdengar.android.playback

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.session.*
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture

/** Player survives activity navigation and screen lock. Only this app may connect. */
class PlaybackService : MediaSessionService() {
    private var session: MediaSession? = null
    private val handler = Handler(Looper.getMainLooper())
    private val sleep = Runnable { session?.player?.pause() }
    override fun onCreate() {
        super.onCreate()
        val prefs = getSharedPreferences("local_playback", MODE_PRIVATE)
        val player = ExoPlayer.Builder(this).setSeekBackIncrementMs(15_000).setSeekForwardIncrementMs(30_000).build()
        player.setAudioAttributes(AudioAttributes.Builder().setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_SPEECH).build(), true)
        player.setHandleAudioBecomingNoisy(true)
        player.addListener(object : Player.Listener {
            override fun onEvents(player: Player, events: Player.Events) {
                val id = player.currentMediaItem?.mediaId ?: return
                prefs.edit().putString("bookId", id).putLong("position:$id", player.currentPosition.coerceAtLeast(0)).apply()
            }
        })
        val checkpoint = object : Runnable {
            override fun run() {
                player.currentMediaItem?.mediaId?.let { id -> prefs.edit().putLong("position:$id", player.currentPosition.coerceAtLeast(0)).apply() }
                handler.postDelayed(this, 5_000)
            }
        }
        handler.post(checkpoint)
        session = MediaSession.Builder(this, player).setCallback(object : MediaSession.Callback {
            override fun onConnect(session: MediaSession, controller: MediaSession.ControllerInfo): MediaSession.ConnectionResult {
                if (controller.packageName != packageName) return MediaSession.ConnectionResult.reject()
                val commands = MediaSession.ConnectionResult.DEFAULT_SESSION_COMMANDS.buildUpon()
                    .add(SessionCommand(SLEEP_COMMAND, Bundle.EMPTY)).build()
                return MediaSession.ConnectionResult.AcceptedResultBuilder(session).setAvailableSessionCommands(commands).build()
            }
            override fun onCustomCommand(session: MediaSession, controller: MediaSession.ControllerInfo, command: SessionCommand, args: Bundle): ListenableFuture<SessionResult> {
                if (command.customAction != SLEEP_COMMAND) return Futures.immediateFuture(SessionResult(SessionResult.RESULT_ERROR_NOT_SUPPORTED))
                handler.removeCallbacks(sleep)
                val minutes = args.getInt("minutes").coerceIn(0, 180)
                if (minutes > 0) handler.postDelayed(sleep, minutes * 60_000L)
                return Futures.immediateFuture(SessionResult(SessionResult.RESULT_SUCCESS))
            }
        }).build()
    }
    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? = session
    override fun onDestroy() {
        session?.player?.let { player -> player.currentMediaItem?.mediaId?.let { id ->
            getSharedPreferences("local_playback", MODE_PRIVATE).edit().putLong("position:$id", player.currentPosition.coerceAtLeast(0)).apply()
        } }
        handler.removeCallbacksAndMessages(null)
        session?.run { player.release(); release() }
        session = null
        super.onDestroy()
    }
    companion object { const val SLEEP_COMMAND = "com.ruangdengar.SLEEP_TIMER" }
}
