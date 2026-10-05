package com.ruangdengar.android

import com.ruangdengar.android.domain.*
import org.junit.Assert.*
import org.junit.Test

class DomainTest {
    @Test fun draftDoesNotReplaceIdentityAudioOrPersonalState() {
        val book=Book("stable-id","Old","Writer",audioUri="content://audio/1",favorite=true,positionMs=1234)
        val updated=MetadataDraft("  New  ","  Author ","").applyTo(book)
        assertEquals("New",updated.title);assertEquals("Author",updated.author);assertEquals("",updated.description)
        assertEquals(book.id,updated.id);assertEquals(book.audioUri,updated.audioUri)
        assertEquals(book.favorite,updated.favorite);assertEquals(book.positionMs,updated.positionMs)
    }
    @Test fun blankTitleIsRejected() { assertNotNull(MetadataDraft("  ","","").validate()) }
    @Test fun serverRejectsCredentialsAndUnencryptedConnections() {
        assertNull(normalizedServerUrl("http://example.com"))
        assertNull(normalizedServerUrl("https://user:password@example.com"))
        assertNull(normalizedServerUrl("https://example.com?token=secret"))
        assertEquals("https://example.com/path",normalizedServerUrl(" https://example.com/path/ "))
    }
    @Test fun timeSupportsLongAudiobooksAndUnknownPosition() {
        assertEquals("0:00",formatTime(-1));assertEquals("1:02:03",formatTime(3_723_000));assertEquals("27:00:00",formatTime(97_200_000))
    }
}
