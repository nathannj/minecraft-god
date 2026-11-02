package com.minecraftgod.bridge;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;

/** Standard 48 kHz mono signed 16-bit little-endian WAV, matching Voice Chat's decoder. */
final class VoiceAudio {
    static byte[] wav(byte[] pcm) {
        ByteBuffer out = ByteBuffer.allocate(44 + pcm.length).order(ByteOrder.LITTLE_ENDIAN);
        out.put("RIFF".getBytes(StandardCharsets.US_ASCII)).putInt(36 + pcm.length);
        out.put("WAVEfmt ".getBytes(StandardCharsets.US_ASCII)).putInt(16).putShort((short)1).putShort((short)1);
        out.putInt(48000).putInt(96000).putShort((short)2).putShort((short)16);
        out.put("data".getBytes(StandardCharsets.US_ASCII)).putInt(pcm.length).put(pcm);
        return out.array();
    }
}
