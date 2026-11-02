package com.minecraftgod.bridge;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.time.Duration;
import java.util.ArrayDeque;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.logging.Logger;

/** Persist before sending; remove only after an acknowledged delivery. */
final class EventOutbox implements AutoCloseable {
    private final ArrayDeque<String> pending = new ArrayDeque<>();
    private final ScheduledExecutorService worker = Executors.newSingleThreadScheduledExecutor(runnable -> {
        Thread thread = new Thread(runnable, "minecraft-god-event-outbox");
        thread.setDaemon(true);
        return thread;
    });
    private final HttpClient http;
    private final URI endpoint;
    private final String token;
    private final Path path;
    private final Logger logger;
    private long retryAfter = 0;
    private int failures = 0;

    EventOutbox(HttpClient http, String url, String token, Path path, Logger logger) {
        this.http = http;
        this.endpoint = URI.create(url);
        this.token = token;
        this.path = path;
        this.logger = logger;
        try {
            if (Files.exists(path)) pending.addAll(Files.readAllLines(path));
        } catch (IOException error) { logger.warning("Could not restore event outbox: " + error.getMessage()); }
        worker.scheduleWithFixedDelay(this::deliver, 0, 250, TimeUnit.MILLISECONDS);
    }

    void enqueue(String body) {
        if (worker.isShutdown()) return;
        worker.execute(() -> {
            // Changed snapshots can replace an older pending snapshot, but prayers,
            // joins, deaths and objective-bearing events are retained.
            if (body.contains("\"type\":\"player_snapshot\"")) {
                String marker = "\"player_uuid\":";
                int index = body.indexOf(marker);
                if (index >= 0) {
                    String identity = body.substring(index, Math.min(body.length(), index + marker.length() + 38));
                    pending.removeIf(value -> value.contains("\"type\":\"player_snapshot\"") && value.contains(identity));
                }
            }
            if (pending.size() >= 4096 && body.contains("\"type\":\"player_snapshot\"")) return;
            if (urgent(body)) {
                ArrayDeque<String> reordered = new ArrayDeque<>();
                while (!pending.isEmpty() && urgent(pending.peekFirst())) reordered.addLast(pending.removeFirst());
                reordered.addLast(body);
                reordered.addAll(pending);
                pending.clear();
                pending.addAll(reordered);
            } else pending.addLast(body);
            persist();
        });
    }

    private static boolean urgent(String body) {
        return body.contains("\"type\":\"player_prayer\"") || body.contains("\"type\":\"player_death\"")
                || body.contains("\"type\":\"health_threshold\"");
    }

    private boolean persist() {
        try {
            Files.createDirectories(path.getParent());
            Path temporary = path.resolveSibling(path.getFileName() + ".tmp");
            Files.write(temporary, pending);
            Files.move(temporary, path, StandardCopyOption.REPLACE_EXISTING);
            return true;
        } catch (IOException error) {
            logger.warning("Could not persist event outbox: " + error.getMessage());
            return false;
        }
    }

    private void deliver() {
        if (pending.isEmpty() || System.nanoTime() < retryAfter) return;
        if (!persist()) {
            retryAfter = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
            return;
        }
        try {
            // Drain a bounded burst, leaving time to persist incoming events.
            for (int delivered = 0; delivered < 32 && !pending.isEmpty(); delivered++) {
                HttpRequest.Builder request = HttpRequest.newBuilder(endpoint).timeout(Duration.ofSeconds(3))
                        .header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(pending.peekFirst()));
                if (!token.isBlank()) request.header("Authorization", "Bearer " + token);
                int status = http.send(request.build(), HttpResponse.BodyHandlers.discarding()).statusCode();
                if (status == 400 || status == 413 || status == 422) {
                    // Keep rejected data for inspection while allowing newer
                    // valid events past an incompatible historical payload.
                    Files.writeString(path.resolveSibling(path.getFileName() + ".rejected.jsonl"), pending.peekFirst() + "\n",
                            StandardOpenOption.CREATE, StandardOpenOption.APPEND);
                    logger.warning("Event rejected by the bridge (HTTP " + status + "); retained in the rejected-event archive.");
                    pending.removeFirst();
                    persist();
                    continue;
                }
                if (status < 200 || status >= 300) throw new IOException("HTTP " + status);
                pending.removeFirst();
                persist();
            }
            if (failures > 0) logger.info("Event delivery recovered; remaining queued events: " + pending.size());
            failures = 0;
        } catch (Exception error) {
            failures++;
            retryAfter = System.nanoTime() + TimeUnit.SECONDS.toNanos(Math.min(30, 1L << Math.min(failures, 5)));
            if (failures == 1 || failures % 10 == 0) logger.warning("Event bridge unavailable; events retained for retry (" + pending.size() + " queued): " + error.getMessage());
        }
    }

    @Override public void close() {
        worker.execute(this::persist);
        worker.shutdown();
        try { worker.awaitTermination(5, TimeUnit.SECONDS); }
        catch (InterruptedException error) { Thread.currentThread().interrupt(); }
    }
}
