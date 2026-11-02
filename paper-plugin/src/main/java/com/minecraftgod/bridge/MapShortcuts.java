package com.minecraftgod.bridge;

import org.bukkit.Bukkit;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.plugin.java.JavaPlugin;

import java.util.HashMap;
import java.util.Map;
import java.util.UUID;
import java.util.function.Consumer;

/** Shares real locations, without introducing teleport privileges. */
final class MapShortcuts implements Listener {
    private final Map<UUID, Long> lastShare = new HashMap<>();

    MapShortcuts(JavaPlugin plugin, Consumer<Map<String, Object>> events) {
        Bukkit.getPluginManager().registerEvents(this, plugin);
        plugin.getCommand("map").setExecutor((sender, command, label, args) -> {
            sender.sendMessage("Maps: M = world map, B = new waypoint, U = waypoints, Y = minimap settings (default keys). "
                    + "Use /waypoint <name> to share this spot. Friends appear through XaeroTracker; /xaerotracker toggleTracked controls sharing. "
                    + "Requires the Minecraft AI God client profile.");
            return true;
        });
        plugin.getCommand("waypoint").setExecutor((sender, command, label, args) -> {
            if (!(sender instanceof Player p)) { sender.sendMessage("Use this command in game."); return true; }
            String name = String.join(" ", args).trim();
            if (!name.matches("[A-Za-z0-9 _-]{1,48}")) return false;
            long now = System.currentTimeMillis();
            if (now-lastShare.getOrDefault(p.getUniqueId(), 0L) < 5000) { p.sendMessage("Wait a few seconds before sharing another waypoint."); return true; }
            lastShare.put(p.getUniqueId(), now);
            var l = p.getLocation();
            String dimension = p.getWorld().getKey().toString();
            String path = dimension.startsWith("minecraft:") ? dimension.substring(10) : dimension.replace(":", "^col^");
            String share = "xaero-waypoint:" + name + ":" + name.substring(0, 1).toUpperCase(java.util.Locale.ROOT)
                    + ":" + l.getBlockX() + ":" + l.getBlockY() + ":" + l.getBlockZ() + ":6:false:0:Internal-" + path;
            for (Player recipient : Bukkit.getOnlinePlayers()) {
                if (recipient.getWorld().equals(p.getWorld()) && recipient.canSee(p)) {
                    recipient.sendMessage("[Map] " + p.getName() + " shared " + name + " (" + dimension + ")");
                    recipient.sendMessage(share);
                }
            }
            events.accept(EventPayload.create("map_waypoint", Map.of("player", p.getName(), "player_uuid", p.getUniqueId().toString(),
                    "dimension", dimension, "position", l.getBlockX()+" "+l.getBlockY()+" "+l.getBlockZ(),
                    "label", name, "message", "Shared map waypoint: " + name, "wake_policy", "accumulate")));
            return true;
        });
    }

    @EventHandler public void onJoin(PlayerJoinEvent event) {
        event.getPlayer().sendMessage("[Map] M: world map | B: new waypoint | U: waypoint list | /map: help | V: voice settings");
    }
    @EventHandler public void onQuit(PlayerQuitEvent event) { lastShare.remove(event.getPlayer().getUniqueId()); }
}
