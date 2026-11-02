package com.minecraftgod.bridge;

import de.maxhenkel.voicechat.api.*;
import de.maxhenkel.voicechat.api.events.*;
import de.maxhenkel.voicechat.api.opus.OpusDecoder;
import org.bukkit.Bukkit;
import org.bukkit.NamespacedKey;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.persistence.PersistentDataType;
import org.bukkit.plugin.java.JavaPlugin;

import java.io.ByteArrayOutputStream;
import java.net.URI;
import java.net.http.*;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.*;

/** No network, recognition, or Bukkit world reads on the voice packet thread. */
final class VoiceBridge implements VoicechatPlugin, Listener, AutoCloseable {
    private record Position(UUID id, String name, String dimension, double x, double y, double z, boolean enabled) {
        String coordinates() { return String.format(Locale.ROOT, "%.2f %.2f %.2f", x, y, z); }
        double distance(Position other) { return Math.sqrt(Math.pow(x-other.x, 2) + Math.pow(y-other.y, 2) + Math.pow(z-other.z, 2)); }
        Map<String, Object> context(Position speaker) {
            return Map.of("player_uuid", id.toString(), "player", name, "dimension", dimension,
                    "position", coordinates(), "distance", Math.round(distance(speaker)*10.0)/10.0);
        }
    }
    private static final int MAX_PCM = 48000 * 2 * 10;
    private final JavaPlugin plugin;
    private final URI endpoint;
    private final String token;
    private final NamespacedKey preference;
    private final ThreadPoolExecutor audio = executor("god-voice-decode", 1024);
    private final ThreadPoolExecutor delivery = executor("god-voice-delivery", 16);
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
    private final Map<UUID, Utterance> utterances = new HashMap<>(); // audio worker only
    private volatile Map<UUID, Position> positions = Map.of();
    private volatile boolean closed;
    private volatile VoicechatServerApi api;
    private long nextWarning;

    VoiceBridge(JavaPlugin plugin, String bridgeUrl, String token) {
        this.plugin = plugin;
        endpoint = URI.create(bridgeUrl).resolve("/voice");
        this.token = token;
        preference = new NamespacedKey(plugin, "voice-transcription");
    }

    private static ThreadPoolExecutor executor(String name, int size) {
        return new ThreadPoolExecutor(1, 1, 0, TimeUnit.SECONDS, new ArrayBlockingQueue<>(size), r -> {
            Thread t = new Thread(r, name); t.setDaemon(true); return t;
        }, new ThreadPoolExecutor.AbortPolicy());
    }

    void start() {
        pluginRadius = plugin.getConfig().getDouble("voice-transcription.nearby-radius", 64);
        BukkitVoicechatService service = Bukkit.getServicesManager().load(BukkitVoicechatService.class);
        if (service == null) { plugin.getLogger().warning("Simple Voice Chat API unavailable; AI hearing is disabled."); return; }
        service.registerPlugin(this);
        Bukkit.getPluginManager().registerEvents(this, plugin);
        plugin.getCommand("godvoice").setExecutor((sender, command, label, args) -> {
            if (!(sender instanceof Player player)) { sender.sendMessage("Use this command in game."); return true; }
            String option = args.length == 0 ? "status" : args[0].toLowerCase(Locale.ROOT);
            if (option.equals("on") || option.equals("off")) {
                player.getPersistentDataContainer().set(preference, PersistentDataType.BYTE, (byte)(option.equals("on") ? 1 : 0));
                snapshot();
            } else if (!option.equals("status")) { return false; }
            player.sendMessage("AI hearing: " + (enabled(player) ? "ON" : "OFF") + ". Proximity voice still works. /godvoice on|off");
            return true;
        });
        Bukkit.getScheduler().runTaskTimer(plugin, this::snapshot, 1, 5);
        plugin.getLogger().info("AI proximity hearing registered; raw audio is memory-only, private groups excluded.");
    }

    private boolean enabled(Player player) {
        return player.getPersistentDataContainer().getOrDefault(preference, PersistentDataType.BYTE,
                (byte)(plugin.getConfig().getBoolean("voice-transcription.default-enabled", true) ? 1 : 0)) != 0;
    }

    @EventHandler public void onJoin(PlayerJoinEvent event) {
        event.getPlayer().sendMessage("[AI God] Proximity speech is transcribed locally for your god and nearby voice recipients' gods. "
                + "Transcripts are saved; raw audio is not. Private groups are excluded. /godvoice off opts out.");
    }

    private void snapshot() {
        Map<UUID, Position> next = new HashMap<>();
        for (Player p : Bukkit.getOnlinePlayers()) {
            var l = p.getLocation();
            next.put(p.getUniqueId(), new Position(p.getUniqueId(), p.getName(), p.getWorld().getKey().toString(),
                    l.getX(), l.getY(), l.getZ(), enabled(p)));
        }
        positions = Map.copyOf(next);
        enqueue(audio, () -> {
            long now = System.nanoTime();
            for (UUID id : List.copyOf(utterances.keySet())) {
                Utterance u = utterances.get(id);
                Position current = positions.get(id);
                if (current == null || !current.enabled) { discard(id); continue; }
                if (u.full || now-u.lastPacket > 700_000_000L) finish(id);
            }
        });
    }

    @Override public String getPluginId() { return "minecraft_god_hearing"; }

    @Override public void registerEvents(EventRegistration registration) {
        registration.registerEvent(VoicechatServerStartedEvent.class, e -> api = e.getVoicechat());
        registration.registerEvent(VoicechatServerStoppedEvent.class, e -> api = null);
        registration.registerEvent(MicrophonePacketEvent.class, this::microphone, -1000);
        registration.registerEvent(EntitySoundPacketEvent.class, this::recipient, -1000);
    }

    private void microphone(MicrophonePacketEvent event) {
        VoicechatConnection sender = event.getSenderConnection();
        if (closed || event.isCancelled() || sender == null) return;
        UUID id = sender.getPlayer().getUuid();
        Position speaker = positions.get(id);
        if (speaker == null || !speaker.enabled || sender.isInGroup() || !sender.isConnected()) {
            enqueue(audio, () -> discard(id)); return;
        }
        byte[] packet = event.getPacket().getOpusEncodedData().clone();
        if (packet.length > 4096) return;
        boolean whisper = event.getPacket().isWhispering();
        long sequence = event.getPacket().toStaticSoundPacket().getSequenceNumber();
        Map<UUID, Position> captured = positions;
        VoicechatServerApi voiceApi = event.getVoicechat();
        double radius = Math.min(128, Math.max(1, pluginRadius));
        enqueue(audio, () -> {
            if (!isEnabled(id)) { discard(id); return; }
            Utterance u = utterances.get(id);
            if (u != null && (u.full || u.whisper != whisper || !u.speaker.dimension.equals(speaker.dimension))) { finish(id); u = null; }
            if (packet.length == 0) {
                // Retain the utterance briefly so all outgoing recipient callbacks are included.
                return;
            }
            if (u == null) {
                if (utterances.size() >= 64) return;
                u = new Utterance(speaker, whisper, voiceApi.createDecoder());
                utterances.put(id, u);
            }
            if (sequence <= u.sequence) return; // reordered/duplicate UDP packet
            u.sequence = sequence;
            try {
                short[] pcm = u.decoder.decode(packet);
                for (short sample : pcm) { u.pcm.write(sample & 255); u.pcm.write((sample >>> 8) & 255); }
                u.lastPacket = System.nanoTime();
                for (Position p : captured.values()) {
                    if (!p.id.equals(id) && p.dimension.equals(speaker.dimension) && p.distance(speaker) <= radius && u.nearby.size() < 32)
                        u.nearby.put(p.id, p.context(speaker));
                }
                if (u.pcm.size() >= MAX_PCM) u.full = true;
                // Finish on the next snapshot, after recipient callbacks for this packet.
            } catch (RuntimeException error) { discard(id); warn("Voice decode failed; this utterance was discarded."); }
        });
    }

    private volatile double pluginRadius = 64;

    private void recipient(EntitySoundPacketEvent event) {
        if (closed || event.isCancelled() || !SoundPacketEvent.SOURCE_PROXIMITY.equals(event.getSource())) return;
        VoicechatConnection receiver = event.getReceiverConnection();
        if (receiver == null || !receiver.isConnected() || receiver.isDisabled()) return;
        UUID senderId = event.getPacket().getSender();
        Position speaker = positions.get(senderId), listener = positions.get(receiver.getPlayer().getUuid());
        if (speaker == null || listener == null || !speaker.dimension.equals(listener.dimension)
                || listener.distance(speaker) > event.getPacket().getDistance()) return;
        Map<String, Object> context = listener.context(speaker);
        enqueue(audio, () -> {
            Utterance u = utterances.get(senderId);
            if (u != null && u.listeners.size() < 32) u.listeners.put(listener.id, context);
        });
    }

    private boolean isEnabled(UUID id) { Position p = positions.get(id); return !closed && p != null && p.enabled; }

    private void finish(UUID id) {
        Utterance u = utterances.remove(id);
        if (u == null) return;
        u.decoder.close();
        if (!isEnabled(id) || u.pcm.size() < 19200) return; // less than 200 ms
        byte[] pcm = u.pcm.toByteArray();
        if (pcm.length > MAX_PCM) pcm = Arrays.copyOf(pcm, MAX_PCM);
        Map<String, Object> meta = EventPayload.create("voice_transcript", Map.of(
                "player", u.speaker.name, "player_uuid", id.toString(), "dimension", u.speaker.dimension,
                "position", u.speaker.coordinates(), "message", "", "started_at", u.started,
                "ended_at", Instant.now().toString(), "voice_mode", u.whisper ? "whisper" : "proximity",
                "nearby_players", List.copyOf(u.nearby.values()), "routed_listeners", List.copyOf(u.listeners.values())));
        meta.put("listener_evidence", "server_routed_in_range; client mute/volume and actual hearing unknown");
        byte[] json = EventPayload.toJson(meta).getBytes(StandardCharsets.UTF_8);
        byte[] wav = VoiceAudio.wav(pcm);
        byte[] body = ByteBuffer.allocate(4+json.length+wav.length).putInt(json.length).put(json).put(wav).array();
        enqueue(delivery, () -> deliver(id, body));
    }

    private void deliver(UUID id, byte[] body) {
        for (int attempt = 0; attempt < 3 && isEnabled(id); attempt++) {
            try {
                var request = HttpRequest.newBuilder(endpoint).timeout(Duration.ofSeconds(5))
                        .header("Authorization", "Bearer " + token).header("Content-Type", "application/x-minecraft-god-voice")
                        .POST(HttpRequest.BodyPublishers.ofByteArray(body)).build();
                int status = http.send(request, HttpResponse.BodyHandlers.discarding()).statusCode();
                if (status == 202) return;
                if (status >= 400 && status < 500 && status != 429) break;
            } catch (Exception error) {
                if (error instanceof InterruptedException) { Thread.currentThread().interrupt(); return; }
            }
            try { Thread.sleep(500L * (attempt+1)); } catch (InterruptedException error) { Thread.currentThread().interrupt(); return; }
        }
        warn("AI hearing could not accept an utterance; proximity voice is unaffected. Check /voice/healthz on the local event hub.");
    }

    private void discard(UUID id) { Utterance u = utterances.remove(id); if (u != null) u.decoder.close(); }
    private void enqueue(ThreadPoolExecutor executor, Runnable action) {
        if (closed) return;
        try { executor.execute(action); } catch (RejectedExecutionException error) { warn("AI voice queue full; dropping audio to protect gameplay."); }
    }
    private synchronized void warn(String message) {
        if (System.currentTimeMillis() > nextWarning) { plugin.getLogger().warning(message); nextWarning = System.currentTimeMillis()+30000; }
    }
    @Override public void close() {
        closed = true;
        delivery.shutdownNow();
        audio.getQueue().clear();
        audio.execute(() -> { for (UUID id : List.copyOf(utterances.keySet())) discard(id); });
        audio.shutdown();
    }
    private static final class Utterance {
        final Position speaker;
        final boolean whisper;
        final String started = Instant.now().toString();
        final OpusDecoder decoder;
        final ByteArrayOutputStream pcm = new ByteArrayOutputStream();
        final Map<UUID, Map<String, Object>> nearby = new LinkedHashMap<>(), listeners = new LinkedHashMap<>();
        long lastPacket = System.nanoTime(), sequence = Long.MIN_VALUE;
        boolean full;
        Utterance(Position speaker, boolean whisper, OpusDecoder decoder) { this.speaker = speaker; this.whisper = whisper; this.decoder = decoder; }
    }
}
