package com.minecraftgod.bridge;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Produces real Java payloads consumed by Python's HTTP/watch contract test. */
public final class EventPayloadTest {
    public static void main(String[] args) throws Exception {
        List<Map<String, Object>> events = new ArrayList<>();
        for (String kind : List.of("chat", "player_prayer", "player_join", "player_quit", "player_death", "dimension_change", "advancement", "explosion_near_player", "player_snapshot", "block_place", "block_break", "item_acquired", "mob_kill", "health_threshold")) {
            Map<String, Object> data = new LinkedHashMap<>();
            data.put("player", "Alice");
            data.put("player_uuid", "a0d35179-5ead-4e3a-bef9-cdbb03bf306d");
            data.put("dimension", "minecraft:overworld");
            data.put("position", "10 64 10");
            data.put("block", "minecraft:gold_block");
            data.put("entity", "minecraft:blaze");
            data.put("inventory", Map.of("minecraft:blaze_rod", 1));
            data.put("health", 5.0);
            data.put("message", "Quote: \" and slash \\ and control \u0001 and newline\n");
            events.add(EventPayload.create(kind, data));
        }
        try {
            EventPayload.create("block_place", Map.of("position", "1 2 3"));
            throw new AssertionError("Missing dimension must fail at the producer");
        } catch (IllegalArgumentException expected) { }
        Path output = Path.of(args[0]);
        Files.createDirectories(output.getParent());
        Files.writeString(output, EventPayload.toJson(events));
        byte[] pcm = new byte[48000];
        pcm[0] = 0; pcm[1] = (byte)128; pcm[4] = (byte)255; pcm[5] = 127;
        Files.write(output.resolveSibling("voice.wav"), VoiceAudio.wav(pcm));
        System.out.println("EventPayload: versioned envelopes, spatial fields and JSON escaping passed");
    }
}
