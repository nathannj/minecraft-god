package com.minecraftgod.bridge;

import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.BooleanSupplier;
import java.util.logging.Logger;

/** Exercises real HTTP delivery and on-disk recovery without Paper. */
public final class EventOutboxTest {
    private static void await(BooleanSupplier condition, String label) throws Exception {
        long deadline = System.nanoTime() + 10_000_000_000L;
        while (!condition.getAsBoolean() && System.nanoTime() < deadline) Thread.sleep(30);
        if (!condition.getAsBoolean()) throw new AssertionError(label);
    }

    private static String read(Path path) {
        try { return Files.exists(path) ? Files.readString(path) : ""; }
        catch (Exception error) { throw new AssertionError(error); }
    }

    public static void main(String[] args) throws Exception {
        Path directory = Files.createTempDirectory("god-outbox-test-");
        Path queue = directory.resolve("pending.queue");
        Path priorityQueue = directory.resolve("priority.queue");
        AtomicInteger status = new AtomicInteger(503), acknowledged = new AtomicInteger();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/events", exchange -> {
            exchange.getRequestBody().readAllBytes();
            int response = status.get();
            if (response == 200) acknowledged.incrementAndGet();
            exchange.sendResponseHeaders(response, -1);
            exchange.close();
        });
        server.start();
        String url = "http://127.0.0.1:" + server.getAddress().getPort() + "/events";
        Logger logger = Logger.getLogger("EventOutboxTest");
        try {
            EventOutbox first = new EventOutbox(HttpClient.newHttpClient(), url, "", queue, logger);
            String prayer = "{\"type\":\"player_prayer\",\"event_uuid\":\"prayer-1\"}";
            first.enqueue(prayer);
            await(() -> read(queue).contains("prayer-1"), "failed delivery must remain on disk");
            Thread.sleep(400);
            if (acknowledged.get() != 0) throw new AssertionError("503 must not acknowledge delivery");
            first.close();
            if (!read(queue).contains("prayer-1")) throw new AssertionError("shutdown must retain prayer");
            status.set(200);
            try (EventOutbox restarted = new EventOutbox(HttpClient.newHttpClient(), url, "", queue, logger)) {
                await(() -> acknowledged.get() == 1 && read(queue).isBlank(), "restart must deliver and remove acknowledged prayer");
                restarted.enqueue("{\"type\":\"player_death\",\"event_uuid\":\"death-2\"}");
                await(() -> acknowledged.get() == 2 && read(queue).isBlank(), "live delivery must remove acknowledged event");
            }
            status.set(503);
            try (EventOutbox priority = new EventOutbox(HttpClient.newHttpClient(), url, "", priorityQueue, logger)) {
                priority.enqueue("{\"type\":\"block_place\",\"event_uuid\":\"routine\"}");
                await(() -> read(priorityQueue).contains("routine"), "routine event persisted");
                Thread.sleep(400);
                priority.enqueue("{\"type\":\"player_prayer\",\"event_uuid\":\"urgent-a\"}");
                priority.enqueue("{\"type\":\"player_death\",\"event_uuid\":\"urgent-b\"}");
                await(() -> read(priorityQueue).contains("urgent-b"), "urgent events persisted");
                String saved = read(priorityQueue);
                if (!(saved.indexOf("urgent-a") < saved.indexOf("urgent-b") && saved.indexOf("urgent-b") < saved.indexOf("routine")))
                    throw new AssertionError("urgent delivery must precede routine backlog while preserving order");
            }
            status.set(400);
            Path invalidQueue = directory.resolve("invalid.queue");
            try (EventOutbox invalid = new EventOutbox(HttpClient.newHttpClient(), url, "", invalidQueue, logger)) {
                invalid.enqueue("{\"type\":\"old_incompatible_payload\"}");
                await(() -> read(directory.resolve("invalid.queue.rejected.jsonl")).contains("old_incompatible_payload") && read(invalidQueue).isBlank(), "permanent rejection must be archived and unblocked");
                status.set(200);
                int before = acknowledged.get();
                invalid.enqueue("{\"type\":\"player_join\"}");
                await(() -> acknowledged.get() > before && read(invalidQueue).isBlank(), "valid delivery must continue after a rejection");
            }
            status.set(401);
            Path authQueue = directory.resolve("auth.queue");
            try (EventOutbox auth = new EventOutbox(HttpClient.newHttpClient(), url, "", authQueue, logger)) {
                auth.enqueue("{\"type\":\"player_prayer\",\"event_uuid\":\"keep-on-auth-failure\"}");
                await(() -> read(authQueue).contains("keep-on-auth-failure"), "authentication failure must retain queued events");
                Thread.sleep(400);
                if (Files.exists(directory.resolve("auth.queue.rejected.jsonl"))) throw new AssertionError("401 must not quarantine valid events");
            }
            System.out.println("EventOutbox: retry, durability, restart, acknowledgement, urgency, rejected payloads, and auth recovery passed");
        } finally { server.stop(0); }
    }
}
