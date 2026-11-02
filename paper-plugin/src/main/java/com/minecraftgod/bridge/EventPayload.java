package com.minecraftgod.bridge;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.stream.Collectors;

/** Versioned transport contract, independent of the Bukkit event machinery. */
final class EventPayload {
    private static final Set<String> SPATIAL = Set.of("block_place", "block_break", "mob_kill", "explosion_near_player", "player_snapshot");

    static Map<String, Object> create(String type, Map<String, ?> data) {
        if (SPATIAL.contains(type) && (!data.containsKey("dimension") || !data.containsKey("position"))) {
            throw new IllegalArgumentException("Spatial events require dimension and position: " + type);
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.putAll(data);
        result.put("schema_version", 1);
        result.put("type", type);
        result.put("source", "paper");
        result.put("event_uuid", UUID.randomUUID().toString());
        result.put("at", Instant.now().toString());
        return result;
    }

    static String toJson(Object value) {
        if (value == null) return "null";
        if (value instanceof String string) return "\"" + escape(string) + "\"";
        if (value instanceof Double number && !Double.isFinite(number)) throw new IllegalArgumentException("Non-finite JSON number");
        if (value instanceof Float number && !Float.isFinite(number)) throw new IllegalArgumentException("Non-finite JSON number");
        if (value instanceof Number || value instanceof Boolean) return value.toString();
        if (value instanceof Map<?, ?> map) return map.entrySet().stream()
                .map(entry -> toJson(String.valueOf(entry.getKey())) + ":" + toJson(entry.getValue()))
                .collect(Collectors.joining(",", "{", "}"));
        if (value instanceof Iterable<?> iterable) {
            List<String> items = new ArrayList<>();
            iterable.forEach(item -> items.add(toJson(item)));
            return String.join(",", items).transform(text -> "[" + text + "]");
        }
        return toJson(String.valueOf(value));
    }

    static String escape(String value) {
        StringBuilder escaped = new StringBuilder();
        for (int i = 0; i < value.length(); i++) {
            char character = value.charAt(i);
            if (character == '"' || character == '\\') escaped.append('\\').append(character);
            else if (character < 32) escaped.append(String.format("\\u%04x", (int) character));
            else escaped.append(character);
        }
        return escaped.toString();
    }
}
