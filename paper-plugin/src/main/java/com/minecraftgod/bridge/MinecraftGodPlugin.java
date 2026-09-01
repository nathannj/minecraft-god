package com.minecraftgod.bridge;

import io.papermc.paper.event.player.AsyncChatEvent;
import net.kyori.adventure.text.serializer.plain.PlainTextComponentSerializer;
import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.block.Block;
import org.bukkit.block.Sign;
import org.bukkit.block.sign.Side;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Entity;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.EntityExplodeEvent;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.event.player.PlayerAdvancementDoneEvent;
import org.bukkit.event.player.PlayerChangedWorldEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.plugin.java.JavaPlugin;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

/** Emits compact, story-relevant signals to the local bridge. */
public final class MinecraftGodPlugin extends JavaPlugin implements Listener, CommandExecutor {
    private final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(2))
            .build();
    private String bridgeUrl;
    private String bridgeToken;
    private String playerName;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        bridgeUrl = getConfig().getString("bridge-url", "http://127.0.0.1:8765/events");
        bridgeToken = getConfig().getString("bridge-token", "");
        playerName = getConfig().getString("player-name", "");
        Bukkit.getPluginManager().registerEvents(this, this);
        if (getCommand("godscan") != null) {
            getCommand("godscan").setExecutor(this);
        }
        if (getCommand("godcheck") != null) {
            getCommand("godcheck").setExecutor(this);
        }

        getLogger().info("Minecraft God bridge enabled: " + bridgeUrl);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onChat(AsyncChatEvent event) {
        Player player = event.getPlayer();
        String message = PlainTextComponentSerializer.plainText().serialize(event.message());
        sendEvent(event("chat", Map.of(
                "player", player.getName(),
                "player_uuid", player.getUniqueId().toString(),
                "message", message
        )));
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onJoin(PlayerJoinEvent event) {
        sendEvent(event("player_join", Map.of(
                "player", event.getPlayer().getName(),
                "player_uuid", event.getPlayer().getUniqueId().toString()
        )));
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onQuit(PlayerQuitEvent event) {
        if (isTracked(event.getPlayer())) {
            sendEvent(event("player_quit", Map.of(
                    "player", event.getPlayer().getName(),
                    "player_uuid", event.getPlayer().getUniqueId().toString()
            )));
        }
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onDeath(PlayerDeathEvent event) {
        if (isTracked(event.getEntity())) {
            Map<String, Object> death = new LinkedHashMap<>();
            death.put("player", event.getEntity().getName());
            death.put("player_uuid", event.getEntity().getUniqueId().toString());
            death.put("victim", event.getEntity().getName());
            death.put("victim_uuid", event.getEntity().getUniqueId().toString());
            death.put("message", event.getDeathMessage() == null ? "" : event.getDeathMessage());
            Player killer = event.getEntity().getKiller();
            if (killer != null) {
                death.put("killer", killer.getName());
                death.put("killer_uuid", killer.getUniqueId().toString());
            }
            sendEvent(event("player_death", death));
        }
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onWorldChange(PlayerChangedWorldEvent event) {
        if (isTracked(event.getPlayer())) {
            sendEvent(event("dimension_change", Map.of(
                    "player", event.getPlayer().getName(),
                    "player_uuid", event.getPlayer().getUniqueId().toString(),
                    "from", event.getFrom().getKey().toString(),
                    "to", event.getPlayer().getWorld().getKey().toString()
            )));
        }
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onAdvancement(PlayerAdvancementDoneEvent event) {
        if (isTracked(event.getPlayer())) {
            sendEvent(event("advancement", Map.of(
                    "player", event.getPlayer().getName(),
                    "player_uuid", event.getPlayer().getUniqueId().toString(),
                    "advancement", event.getAdvancement().getKey().toString()
            )));
        }
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onExplosion(EntityExplodeEvent event) {
        Location location = event.getLocation();
        Player tracked = nearestTrackedPlayer(location, 48.0);
        if (tracked != null) {
            sendEvent(event("explosion_near_player", Map.of(
                    "player", tracked.getName(),
                    "player_uuid", tracked.getUniqueId().toString(),
                    "entity", event.getEntityType().getKey().toString(),
                    "position", position(location)
            )));
        }
    }

    /**
     * Returns an exact, compact local scene description for the god's sight.
     * RCON invokes this as: godscan <player> [radius] [vertical-radius].
     */
    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (command.getName().equalsIgnoreCase("pray")) {
            return prayer(sender, args, "pray");
        }
        if (command.getName().equalsIgnoreCase("whisper")) {
            if (args.length == 0 || !args[0].equalsIgnoreCase("god")) {
                sender.sendMessage("Usage: /whisper god <message>");
                return true;
            }
            String[] prayerArgs = new String[Math.max(0, args.length - 1)];
            System.arraycopy(args, 1, prayerArgs, 0, prayerArgs.length);
            return prayer(sender, prayerArgs, "whisper god");
        }
        if (command.getName().equalsIgnoreCase("godcheck")) {
            return godcheck(sender, args);
        }
        if (!command.getName().equalsIgnoreCase("godscan")) {
            return false;
        }
        String requestedPlayer = args.length > 0 ? args[0] : playerName;
        if (requestedPlayer == null || requestedPlayer.isBlank()) {
            sender.sendMessage("{\"error\":\"player_name_required\"}");
            return true;
        }
        Player player = Bukkit.getPlayerExact(requestedPlayer);
        if (player == null || !player.isOnline()) {
            sender.sendMessage("{\"error\":\"player_not_found\",\"player\":\"" + escape(requestedPlayer) + "\"}");
            return true;
        }
        int radius = boundedArg(args, 1, 8, 1, 12);
        int vertical = boundedArg(args, 2, 5, 1, 8);
        sender.sendMessage(toJson(sceneScan(player, radius, vertical)));
        return true;
    }

    private boolean prayer(CommandSender sender, String[] args, String commandName) {
        if (!(sender instanceof Player player)) {
            sender.sendMessage("Only a player can speak privately to their god.");
            return true;
        }
        String message = String.join(" ", args).trim();
        if (message.isBlank()) {
            sender.sendMessage("Usage: /" + commandName + " <message>");
            return true;
        }
        if (message.length() > 1000) {
            sender.sendMessage("Your prayer is too long (maximum 1000 characters).");
            return true;
        }
        sendEvent(event("player_prayer", Map.of(
                "player", player.getName(),
                "player_uuid", player.getUniqueId().toString(),
                "message", message,
                "private", true,
                "command", commandName
        )));
        sender.sendMessage("Your words reach the god who watches you.");
        return true;
    }

    private boolean godcheck(CommandSender sender, String[] args) {
        if (args.length < 4) {
            sender.sendMessage("{\"error\":\"usage: godcheck <player> <x> <y> <z>\"}");
            return true;
        }
        Player player = Bukkit.getPlayerExact(args[0]);
        if (player == null || !player.isOnline()) {
            sender.sendMessage("{\"error\":\"player_not_found\",\"player\":\"" + escape(args[0]) + "\"}");
            return true;
        }
        try {
            int x = Integer.parseInt(args[1]);
            int y = Integer.parseInt(args[2]);
            int z = Integer.parseInt(args[3]);
            Block block = player.getWorld().getBlockAt(x, y, z);
            sender.sendMessage(toJson(Map.of(
                    "player", player.getName(),
                    "dimension", player.getWorld().getKey().toString(),
                    "position", x + " " + y + " " + z,
                    "type", block.getType().getKey().toString(),
                    "solid", block.getType().isSolid(),
                    "passable", block.isPassable(),
                    "liquid", block.isLiquid(),
                    "replaceable", block.isReplaceable()
            )));
        } catch (NumberFormatException exception) {
            sender.sendMessage("{\"error\":\"coordinates_must_be_integers\"}");
        }
        return true;
    }

    private Map<String, Object> sceneScan(Player player, int radius, int vertical) {
        Location location = player.getLocation();
        World world = player.getWorld();
        int centerX = location.getBlockX();
        int centerY = location.getBlockY();
        int centerZ = location.getBlockZ();
        Map<String, Object> scan = new LinkedHashMap<>();
        scan.put("player", player.getName());
        scan.put("dimension", world.getKey().toString());
        scan.put("position", position(location));
        scan.put("block_position", centerX + " " + centerY + " " + centerZ);
        scan.put("eye_position", position(player.getEyeLocation()));
        scan.put("yaw", round(player.getYaw()));
        scan.put("pitch", round(player.getPitch()));
        scan.put("biome", location.getBlock().getBiome().getKey().toString());
        scan.put("time", world.getTime());
        scan.put("weather", world.isThundering() ? "thunder" : world.hasStorm() ? "rain" : "clear");

        List<Map<String, Object>> blocks = new ArrayList<>();
        for (int y = centerY - vertical; y <= centerY + vertical; y++) {
            for (int x = centerX - radius; x <= centerX + radius; x++) {
                for (int z = centerZ - radius; z <= centerZ + radius; z++) {
                    Block block = world.getBlockAt(x, y, z);
                    if (block.getType().isAir()) {
                        continue;
                    }
                    Map<String, Object> entry = new LinkedHashMap<>();
                    entry.put("position", x + " " + y + " " + z);
                    entry.put("relative", (x - centerX) + " " + (y - centerY) + " " + (z - centerZ));
                    entry.put("type", block.getType().getKey().toString());
                    entry.put("state", block.getBlockData().getAsString());
                    if (block.getState() instanceof Sign sign) {
                        entry.put("front_text", signLines(sign, Side.FRONT));
                        entry.put("back_text", signLines(sign, Side.BACK));
                    }
                    blocks.add(entry);
                }
            }
        }
        scan.put("blocks", blocks);

        List<Map<String, Object>> entities = new ArrayList<>();
        for (Entity entity : player.getNearbyEntities(radius + 0.5, vertical + 3.0, radius + 0.5)) {
            Location entityLocation = entity.getLocation();
            Map<String, Object> entry = new LinkedHashMap<>();
            entry.put("uuid", entity.getUniqueId().toString());
            entry.put("type", entity.getType().getKey().toString());
            entry.put("position", position(entityLocation));
            entry.put("block_position", entityLocation.getBlockX() + " " + entityLocation.getBlockY() + " " + entityLocation.getBlockZ());
            entry.put("custom_name", entity.getCustomName() == null ? "" : entity.getCustomName());
            entry.put("custom_name_visible", entity.isCustomNameVisible());
            entry.put("on_ground", entity.isOnGround());
            entry.put("glowing", entity.isGlowing());
            entry.put("invulnerable", entity.isInvulnerable());
            entry.put("no_gravity", !entity.hasGravity());
            entry.put("tags", new ArrayList<>(entity.getScoreboardTags()));
            entities.add(entry);
        }
        scan.put("entities", entities);
        return scan;
    }

    private List<String> signLines(Sign sign, Side side) {
        return new ArrayList<>(List.of(sign.getSide(side).getLines()));
    }

    private static int boundedArg(String[] args, int index, int fallback, int minimum, int maximum) {
        if (args.length <= index) {
            return fallback;
        }
        try {
            return Math.max(minimum, Math.min(maximum, Integer.parseInt(args[index])));
        } catch (NumberFormatException ignored) {
            return fallback;
        }
    }

    private boolean isTracked(Player player) {
        // Multi-god mode observes every player. The controller performs the
        // per-UUID routing; filtering here would silently starve other gods.
        return true;
    }

    private Player nearestTrackedPlayer(Location location, double radius) {
        Player nearest = null;
        double nearestDistance = radius * radius;
        for (Player player : Bukkit.getOnlinePlayers()) {
            if (!player.getWorld().equals(location.getWorld())) {
                continue;
            }
            double distance = player.getLocation().distanceSquared(location);
            if (distance <= nearestDistance) {
                nearest = player;
                nearestDistance = distance;
            }
        }
        return nearest;
    }

    private Map<String, Object> event(String type, Map<String, ?> data) {
        Map<String, Object> event = new HashMap<>();
        event.put("type", type);
        event.putAll(data);
        return event;
    }

    private void sendEvent(Map<String, Object> event) {
        String body = toJson(event);
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create(bridgeUrl))
                .timeout(Duration.ofSeconds(3))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body));
        if (!bridgeToken.isBlank()) {
            request.header("Authorization", "Bearer " + bridgeToken);
        }
        http.sendAsync(request.build(), HttpResponse.BodyHandlers.discarding())
                .exceptionally(error -> {
                    getLogger().fine("Could not send event to bridge: " + error.getMessage());
                    return null;
                });
    }

    private static String position(Location location) {
        return round(location.getX()) + " " + round(location.getY()) + " " + round(location.getZ());
    }

    private static double round(double value) {
        return Math.round(value * 100.0) / 100.0;
    }

    private static String toJson(Object value) {
        if (value == null) return "null";
        if (value instanceof String string) return "\"" + escape(string) + "\"";
        if (value instanceof Number || value instanceof Boolean) return value.toString();
        if (value instanceof Map<?, ?> map) {
            return map.entrySet().stream()
                    .map(entry -> toJson(String.valueOf(entry.getKey())) + ":" + toJson(entry.getValue()))
                    .collect(Collectors.joining(",", "{", "}"));
        }
        if (value instanceof Iterable<?> iterable) {
            List<String> values = new ArrayList<>();
            iterable.forEach(item -> values.add(toJson(item)));
            return String.join(",", values).transform(text -> "[" + text + "]");
        }
        return toJson(String.valueOf(value));
    }

    private static String escape(String value) {
        return value.replace("\\", "\\\\")
                .replace("\"", "\\\"")
                .replace("\b", "\\b")
                .replace("\f", "\\f")
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t");
    }
}
