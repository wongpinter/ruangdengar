# RuangDengar Android

Kotlin / Jetpack Compose app in the existing backend repository. This first vertical slice
implements the mobile design system and a working **local audio player**. It does not yet
sign into the backend or stream the Drive library. Example books are explicitly labeled
as design fixtures and have no audio.

## Run

Open `android/` as a project in Android Studio. Install Android SDK 35 and use JDK 17.
Gradle 8.13, AGP 8.11.1, Kotlin and Compose compiler 2.1.20 are pinned together.
If using an installed Gradle: `gradle :app:assembleDebug :app:testDebugUnitTest :app:lintDebug`.
The Android workflow builds and uploads `ruangdengar-debug-apk` on branch pushes and PRs.

1. Install the debug APK on Android 8+.
2. Choose **Tambahkan audio dari ponsel**, select an audio file via Android's document picker.
3. Open the imported book and start playback. Lock the screen, navigate away, test system
   notification/headset controls, audio focus interruptions, and unplugging headphones.
4. Try seek, speed, timer, bookmarks, favorites, local metadata edits, and dark theme.
5. Close/reopen the app and verify library, metadata, bookmarks, and checkpoint recovery.

## Atomic structure

- `design/foundations`: semantic light/dark color, typography, shape and spacing tokens.
- `design/atoms`: actions, icon targets, cover fallback and supporting text.
- `design/molecules`: book, metadata and settings rows.
- `design/organisms`: persistent mini-player.
- `design/templates`: app scaffold and navigation, system insets via Material scaffolds.
- `features`: book detail, draft editor, player, server configuration and empty states.
- `domain`: immutable books, metadata validation, URL validation, time formatting.
- `data`: local prototype persistence, no credentials.
- `playback`: Media3 service and lifecycle-independent media session/controller.

Navigation, search/favorites, import, manual local editing and playback operate on real local
state. Back navigation must protect unsaved drafts. Drafts survive configuration changes.
Playback checkpoints are stored by the service every five seconds and on state changes;
they are local checkpoints, not server revisions. The timer runs inside the service.
File access uses persisted document grants; no broad storage permission. Imported content
URIs remain references to the selected provider, not verified managed offline downloads.

## Planned backend integration

See `../docs/API.md`. The native authentication contract is a prerequisite: existing OAuth
uses browser session cookies, which cannot be assumed to transfer to a native Media3 client.
Do not accept pasted cookies, put tokens in URLs, or weaken backend authentication.

Next slices: native login + authenticated catalog/playback; API DTO/repository; local database
and revision-aware checkpoint outbox; Media3 managed downloads; playlists/history; provider
metadata comparison and manual PATCH with revision/clear/reset. Bookmarks here are local
and need an API before cross-device synchronization. Manual editing does not alter server
or Drive metadata. Server settings only validate and store an address; they do not claim a
successful connection. Keep provider imports disabled until their complete API is available.

Full design atlas: `design/RuangDengar-Mobile-Mockups.html`. It documents 58 screens and states;
this initial native slice implements the core local flows, not all planned capabilities.

## Verification

CI performs APK compilation, JVM domain tests, and Android lint. Emulator screenshots,
TalkBack, font scaling 200%, narrow/wide devices, Android system media controls and long
listening sessions on a physical phone are still required. No production/release signing,
credentials, or backend data are committed. Local library storage is a prototype store;
move persistence to Room before adding sync/download transactions or a large catalog.
