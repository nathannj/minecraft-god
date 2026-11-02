package com.minecraftgod.bridge;

import de.maxhenkel.voicechat.api.*;
import de.maxhenkel.voicechat.api.events.*;
import de.maxhenkel.voicechat.api.packets.*;
import org.bukkit.Bukkit;
import org.bukkit.plugin.java.JavaPlugin;

import javax.sound.sampled.AudioSystem;
import java.lang.reflect.*;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ThreadPoolExecutor;

/** Synthetic identities in a disposable world, exercising the production capture callbacks and native codec. */
public final class VoiceProbePlugin extends JavaPlugin implements VoicechatPlugin {
    private Object capture;
    @Override public void onEnable() {
        Bukkit.getServicesManager().load(BukkitVoicechatService.class).registerPlugin(this);
    }
    @Override public String getPluginId() { return "god_voice_test_probe"; }
    @Override public void registerEvents(EventRegistration registration) {
        registration.registerEvent(VoicechatServerStartedEvent.class, event -> {
            try { probe(event.getVoicechat()); }
            catch (Exception error) { getLogger().log(java.util.logging.Level.SEVERE, "Voice probe failed", error); }
        });
    }
    private static <T> T mock(Class<T> type, Map<String, Object> values) {
        return type.cast(Proxy.newProxyInstance(type.getClassLoader(), new Class<?>[]{type}, (proxy, method, args) -> {
            if (values.containsKey(method.getName())) return values.get(method.getName());
            if (method.getReturnType() == boolean.class) return false;
            throw new UnsupportedOperationException(method.getName());
        }));
    }
    private void probe(VoicechatServerApi api) throws Exception {
        ClassLoader loader = Bukkit.getPluginManager().getPlugin("MinecraftGodBridge").getClass().getClassLoader();
        Class<?> type = Class.forName("com.minecraftgod.bridge.VoiceBridge", true, loader);
        Constructor<?> constructor = type.getDeclaredConstructor(JavaPlugin.class, String.class, String.class);
        constructor.setAccessible(true);
        capture = constructor.newInstance(this, getConfig().getString("bridge-url"), getConfig().getString("bridge-token"));
        Class<?> positionType = Class.forName("com.minecraftgod.bridge.VoiceBridge$Position", true, loader);
        Constructor<?> position = positionType.getDeclaredConstructors()[0]; position.setAccessible(true);
        UUID alice = UUID.fromString("b5265ddd-f92f-5b0a-8a1a-0116468d4266"), bob = UUID.fromString("a0d35179-5ead-4e3a-bef9-cdbb03bf306d");
        Object speaker = position.newInstance(alice, "Alice", "minecraft:overworld", 0.0, 64.0, 0.0, true);
        Object listener = position.newInstance(bob, "Bob", "minecraft:overworld", 3.0, 64.0, 0.0, true);
        Field positions = type.getDeclaredField("positions"); positions.setAccessible(true);
        positions.set(capture, Map.of(alice, speaker, bob, listener));
        VoicechatConnection sender = mock(VoicechatConnection.class, Map.of("getPlayer", mock(ServerPlayer.class, Map.of("getUuid", alice)), "isConnected", true));
        VoicechatConnection receiver = mock(VoicechatConnection.class, Map.of("getPlayer", mock(ServerPlayer.class, Map.of("getUuid", bob)), "isConnected", true));
        Method microphone = type.getDeclaredMethod("microphone", MicrophonePacketEvent.class); microphone.setAccessible(true);
        Method recipient = type.getDeclaredMethod("recipient", EntitySoundPacketEvent.class); recipient.setAccessible(true);
        byte[] raw;
        try (var stream = AudioSystem.getAudioInputStream(getDataFolder().toPath().resolve("fixture.wav").toFile())) {
            raw = stream.readAllBytes();
        }
        var encoder = api.createEncoder();
        try {
            ByteBuffer pcm = ByteBuffer.wrap(raw).order(ByteOrder.LITTLE_ENDIAN);
            long sequence = 0;
            while (pcm.remaining() >= 1920) {
                short[] frame = new short[960]; for (int i=0; i<frame.length; i++) frame[i] = pcm.getShort();
                byte[] encoded = encoder.encode(frame);
                StaticSoundPacket sound = mock(StaticSoundPacket.class, Map.of("getSequenceNumber", sequence++));
                MicrophonePacket packet = mock(MicrophonePacket.class, Map.of("getOpusEncodedData", encoded, "toStaticSoundPacket", sound));
                microphone.invoke(capture, mock(MicrophonePacketEvent.class, Map.of("getSenderConnection", sender, "getPacket", packet, "getVoicechat", api)));
                EntitySoundPacket outgoing = mock(EntitySoundPacket.class, Map.of("getSender", alice, "getDistance", 48.0f));
                recipient.invoke(capture, mock(EntitySoundPacketEvent.class, Map.of("getSource", "proximity", "getReceiverConnection", receiver, "getPacket", outgoing)));
            }
        } finally { encoder.close(); }
        Field worker = type.getDeclaredField("audio"); worker.setAccessible(true);
        Method finish = type.getDeclaredMethod("finish", UUID.class); finish.setAccessible(true);
        ((ThreadPoolExecutor)worker.get(capture)).execute(() -> {
            try { finish.invoke(capture, alice); getLogger().info("VOICE_PROBE_SUBMITTED"); }
            catch (Exception error) { throw new RuntimeException(error); }
        });
    }
    @Override public void onDisable() {
        if (capture != null) {
            try { Method close = capture.getClass().getDeclaredMethod("close"); close.setAccessible(true); close.invoke(capture); }
            catch (Exception error) { getLogger().warning("Voice probe shutdown: " + error.getClass().getSimpleName()); }
        }
    }
}
