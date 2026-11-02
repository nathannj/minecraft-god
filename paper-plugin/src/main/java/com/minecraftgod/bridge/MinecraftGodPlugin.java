package com.minecraftgod.bridge;

import io.papermc.paper.event.player.AsyncChatEvent;
import net.kyori.adventure.text.serializer.plain.PlainTextComponentSerializer;
import org.bukkit.Bukkit;
import org.bukkit.Chunk;
import org.bukkit.Location;
import org.bukkit.NamespacedKey;
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
import org.bukkit.event.block.BlockBreakEvent;
import org.bukkit.event.block.BlockPlaceEvent;
import org.bukkit.event.entity.EntityDamageEvent;
import org.bukkit.event.entity.EntityDeathEvent;
import org.bukkit.event.entity.EntityPickupItemEvent;
import org.bukkit.event.entity.EntityExplodeEvent;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.event.player.PlayerAdvancementDoneEvent;
import org.bukkit.event.player.PlayerChangedWorldEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.inventory.ItemStack;

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
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.stream.Collectors;

/** Emits compact, story-relevant signals to the local bridge. */
public final class MinecraftGodPlugin extends JavaPlugin implements Listener, CommandExecutor {
    private final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(2))
            .build();
    private String bridgeUrl;
    private String bridgeToken;
    private String playerName;
    private EventOutbox outbox;
    private VoiceBridge voiceBridge;
    private final Map<UUID, String> lastSnapshot = new ConcurrentHashMap<>();
    private final Map<UUID, Integer> lastHealthBand = new ConcurrentHashMap<>();
    private final Map<UUID, Long> lastPrayer = new ConcurrentHashMap<>();
    private final Map<Chunk, Long> ticketExpiry = new HashMap<>();

    @Override
    public void onEnable() {
        saveDefaultConfig();
        bridgeUrl = getConfig().getString("bridge-url", "http://127.0.0.1:8765/events");
        bridgeToken = getConfig().getString("bridge-token", "");
        playerName = getConfig().getString("player-name", "");
        outbox = new EventOutbox(http, bridgeUrl, bridgeToken, getDataFolder().toPath().resolve("pending-events.queue"), getLogger());
        Bukkit.getPluginManager().registerEvents(this, this);
        new MapShortcuts(this, this::sendEvent);
        if (getConfig().getBoolean("voice-transcription.enabled", true)
                && Bukkit.getPluginManager().isPluginEnabled("voicechat")) {
            voiceBridge = new VoiceBridge(this, bridgeUrl, bridgeToken);
            voiceBridge.start();
        }
        // Cheap deterministic telemetry supports story watches without waking
        // an LLM.  A snapshot is emitted only when its contents have changed.
        Bukkit.getScheduler().runTaskTimer(this, this::emitChangedSnapshots, 100L, 300L);
        if (getCommand("godscan") != null) {
            getCommand("godscan").setExecutor(this);
        }
        if (getCommand("godcheck") != null) {
            getCommand("godcheck").setExecutor(this);
        }
        if (getCommand("godscanat") != null) {
            getCommand("godscanat").setExecutor(this);
        }
        if (getCommand("godcheckat") != null) {
            getCommand("godcheckat").setExecutor(this);
        }

        getLogger().info("Minecraft God bridge enabled: " + bridgeUrl);
    }

    @Override public void onDisable() {
        if (voiceBridge != null) voiceBridge.close();
        if (outbox != null) outbox.close();
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
        lastSnapshot.remove(event.getPlayer().getUniqueId());
        lastHealthBand.remove(event.getPlayer().getUniqueId());
        lastPrayer.remove(event.getPlayer().getUniqueId());
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
                    "dimension", location.getWorld().getKey().toString(),
                    "position", position(location)
            )));
        }
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBlockPlace(BlockPlaceEvent event) {
        Player player = event.getPlayer();
        sendEvent(event("block_place", Map.of(
                "player", player.getName(),
                "player_uuid", player.getUniqueId().toString(),
                "block", event.getBlockPlaced().getType().getKey().toString(),
                "dimension", event.getBlockPlaced().getLocation().getWorld().getKey().toString(),
                "position", position(event.getBlockPlaced().getLocation()),
                "wake_policy", "never"
        )));
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBlockBreak(BlockBreakEvent event) {
        Player player = event.getPlayer();
        sendEvent(event("block_break", Map.of(
                "player", player.getName(),
                "player_uuid", player.getUniqueId().toString(),
                "block", event.getBlock().getType().getKey().toString(),
                "dimension", event.getBlock().getLocation().getWorld().getKey().toString(),
                "position", position(event.getBlock().getLocation()),
                "wake_policy", "never"
        )));
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onItemPickup(EntityPickupItemEvent event) {
        if (!(event.getEntity() instanceof Player player)) {
            return;
        }
        ItemStack stack = event.getItem().getItemStack();
        sendEvent(event("item_acquired", Map.of(
                "player", player.getName(),
                "player_uuid", player.getUniqueId().toString(),
                "item", stack.getType().getKey().toString(),
                "amount", stack.getAmount(),
                "wake_policy", "never"
        )));
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onEntityDeath(EntityDeathEvent event) {
        if (event.getEntity() instanceof Player) {
            return;
        }
        Player killer = event.getEntity().getKiller();
        if (killer == null) {
            return;
        }
        sendEvent(event("mob_kill", Map.of(
                "player", killer.getName(),
                "player_uuid", killer.getUniqueId().toString(),
                "entity", event.getEntityType().getKey().toString(),
                "dimension", event.getEntity().getLocation().getWorld().getKey().toString(),
                "position", position(event.getEntity().getLocation()),
                "wake_policy", "never"
        )));
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onDamage(EntityDamageEvent event) {
        if (!(event.getEntity() instanceof Player player)) {
            return;
        }
        double health = Math.max(0.0, player.getHealth() - event.getFinalDamage());
        int band = health <= 2.0 ? 1 : health <= 6.0 ? 2 : health <= 10.0 ? 3 : 4;
        Integer previous = lastHealthBand.put(player.getUniqueId(), band);
        if (band >= 4 || (previous != null && previous == band)) {
            return;
        }
        sendEvent(event("health_threshold", Map.of(
                "player", player.getName(),
                "player_uuid", player.getUniqueId().toString(),
                "health", Math.round(health * 10.0) / 10.0,
                "cause", event.getCause().name().toLowerCase(),
                "wake_policy", health <= 6.0 ? "always" : "accumulate"
        )));
    }

    private void emitChangedSnapshots() {
        for (Player player : Bukkit.getOnlinePlayers()) {
            Map<String, Integer> inventory = new LinkedHashMap<>();
            for (ItemStack stack : player.getInventory().getContents()) {
                if (stack == null || stack.getType().isAir()) {
                    continue;
                }
                inventory.merge(stack.getType().getKey().toString(), stack.getAmount(), Integer::sum);
            }
            Map<String, Object> snapshot = new LinkedHashMap<>();
            snapshot.put("player", player.getName());
            snapshot.put("player_uuid", player.getUniqueId().toString());
            snapshot.put("dimension", player.getWorld().getKey().toString());
            snapshot.put("position", position(player.getLocation()));
            snapshot.put("health", Math.round(player.getHealth() * 10.0) / 10.0);
            snapshot.put("inventory", inventory);
            snapshot.put("wake_policy", "never");
            String signature = toJson(snapshot);
            if (!signature.equals(lastSnapshot.put(player.getUniqueId(), signature))) {
                sendEvent(event("player_snapshot", snapshot));
            }
            int band = player.getHealth() <= 2.0 ? 1 : player.getHealth() <= 6.0 ? 2 : player.getHealth() <= 10.0 ? 3 : 4;
            lastHealthBand.put(player.getUniqueId(), band);
        }
    }

    /**
     * Returns an exact, compact local scene description for the god's sight.
     * RCON invokes this as: godscan <player> [radius] [vertical-radius].
     */
    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (command.getName().equalsIgnoreCase("godsmite")) {
            Player target = args.length == 1 ? Bukkit.getPlayerExact(args[0]) : null;
            if (target == null) sender.sendMessage("{\"ok\":false,\"error\":\"player_not_found\"}");
            else {
                target.getWorld().strikeLightningEffect(target.getLocation());
                target.damage(5.0);
                sender.sendMessage("{\"ok\":true,\"action\":\"scoped_smite\"}");
            }
            return true;
        }
        if (List.of("godobserve", "godcheckbatch", "godsurface", "godfootprint", "godvalidate").contains(command.getName().toLowerCase())) {
            return compactObservation(sender, command.getName().toLowerCase(), args);
        }
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
        if (command.getName().equalsIgnoreCase("godcheckat")) {
            return godcheckat(sender, args);
        }
        if (command.getName().equalsIgnoreCase("godscanat")) {
            return godscanat(sender, args);
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
        long now = System.nanoTime();
        long cooldown = java.util.concurrent.TimeUnit.SECONDS.toNanos(Math.max(0, getConfig().getInt("prayer-cooldown-seconds", 10)));
        Long previous = lastPrayer.get(player.getUniqueId());
        if (previous != null && now - previous < cooldown) {
            sender.sendMessage("Give your god a moment before sending another prayer.");
            return true;
        }
        lastPrayer.put(player.getUniqueId(), now);
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

    private boolean compactObservation(CommandSender sender, String operation, String[] args) {
        try {
            if (operation.equals("godobserve") || operation.equals("godsurface")) {
                Player player = args.length > 0 ? Bukkit.getPlayerExact(args[0]) : null;
                if (player == null) throw new IllegalArgumentException("player_not_found");
                World world = player.getWorld();
                if (operation.equals("godobserve")) {
                    Map<String, Integer> inventory = new LinkedHashMap<>();
                    for (ItemStack item : player.getInventory().getContents()) {
                        if (item != null && !item.getType().isAir()) inventory.merge(item.getType().getKey().toString(), item.getAmount(), Integer::sum);
                    }
                    Map<String, Object> value = new LinkedHashMap<>();
                    value.put("ok", true);
                    value.put("player", player.getName());
                    value.put("player_uuid", player.getUniqueId().toString());
                    value.put("dimension", world.getKey().toString());
                    value.put("position", position(player.getLocation()));
                    value.put("health", player.getHealth());
                    value.put("food", player.getFoodLevel());
                    value.put("inventory", inventory);
                    sender.sendMessage(toJson(value));
                    return true;
                }
                int x = player.getLocation().getBlockX(), z = player.getLocation().getBlockZ();
                for (int y = world.getHighestBlockYAt(x, z); y >= world.getMinHeight(); y--) {
                    Block support = world.getBlockAt(x, y, z);
                    if (y + 2 >= world.getMaxHeight() || !support.getType().isSolid() || support.isLiquid()
                            || support.getType().name().contains("LEAVES") || support.getType().name().contains("LOG")
                            || List.of("MAGMA_BLOCK", "CACTUS", "CAMPFIRE", "SOUL_CAMPFIRE").contains(support.getType().name())) continue;
                    Block feet = world.getBlockAt(x, y + 1, z), head = world.getBlockAt(x, y + 2, z);
                    if (feet.getType().isAir() && head.getType().isAir()) {
                        sender.sendMessage(toJson(Map.of("ok", true, "dimension", world.getKey().toString(), "position", x + " " + (y + 1) + " " + z, "support", blockDescription(world, support, x, y, z))));
                        return true;
                    }
                }
                throw new IllegalArgumentException("no_safe_surface");
            }
            if (operation.equals("godcheckbatch")) {
                if (args.length < 2 || args.length > 129) throw new IllegalArgumentException("batch_requires_1_to_128_positions");
                World world = worldFor(args[0]);
                if (world == null) throw new IllegalArgumentException("dimension_not_found");
                List<Map<String, Object>> results = new ArrayList<>();
                for (int i = 1; i < args.length; i++) {
                    String[] xyz = args[i].split(",");
                    if (xyz.length != 3) throw new IllegalArgumentException("invalid_batch_position");
                    int x = Integer.parseInt(xyz[0]), y = Integer.parseInt(xyz[1]), z = Integer.parseInt(xyz[2]);
                    if (y < world.getMinHeight() || y >= world.getMaxHeight()) throw new IllegalArgumentException("position_outside_world_height");
                    Chunk chunk = world.getChunkAt(x >> 4, z >> 4);
                    chunk.addPluginChunkTicket(this);
                    try { results.add(Map.of("ok", true, "block", blockDescription(world, world.getBlockAt(x, y, z), x, y, z))); }
                    finally { releaseChunkTicketSoon(chunk); }
                }
                sender.sendMessage(toJson(Map.of("ok", true, "dimension", world.getKey().toString(), "results", results)));
                return true;
            }
            int offset = operation.equals("godvalidate") ? 1 : 0;
            if (args.length != 7 + offset) throw new IllegalArgumentException("dimension_and_six_bounds_required");
            World world = worldFor(args[offset]);
            if (world == null) throw new IllegalArgumentException("dimension_not_found");
            int[] min = new int[3], max = new int[3];
            for (int i = 0; i < 3; i++) {
                min[i] = Integer.parseInt(args[offset + 1 + i]);
                max[i] = Integer.parseInt(args[offset + 4 + i]);
                if (min[i] > max[i]) throw new IllegalArgumentException("invalid_bounds");
            }
            List<String> others = new ArrayList<>();
            for (Player player : world.getPlayers()) {
                Location p = player.getLocation();
                if (operation.equals("godvalidate") && player.getName().equals(args[0])) continue;
                if (p.getX() >= min[0] && p.getX() < max[0] + 1 && p.getY() >= min[1] && p.getY() < max[1] + 1 && p.getZ() >= min[2] && p.getZ() < max[2] + 1) others.add(player.getUniqueId().toString());
            }
            if (operation.equals("godvalidate")) {
                if (max[0] - min[0] > 160 || max[2] - min[2] > 160) throw new IllegalArgumentException("affected_area_too_large");
                for (int cx = min[0] >> 4; cx <= max[0] >> 4; cx++) for (int cz = min[2] >> 4; cz <= max[2] >> 4; cz++) {
                    Chunk chunk = world.getChunkAt(cx, cz);
                    chunk.addPluginChunkTicket(this);
                    releaseChunkTicketSoon(chunk);
                }
                sender.sendMessage(toJson(Map.of("ok", others.isEmpty(), "other_players", others)));
                return true;
            }
            long volume = (long)(max[0] - min[0] + 1) * (max[1] - min[1] + 1) * (max[2] - min[2] + 1);
            if (volume > 4096 || max[0] - min[0] > 47 || max[1] - min[1] > 47 || max[2] - min[2] > 47 || min[1] < world.getMinHeight() || max[1] >= world.getMaxHeight()) throw new IllegalArgumentException("footprint_limit_4096_blocks_48_per_axis");
            Map<String, Integer> materials = new LinkedHashMap<>();
            int liquids = 0, occupied = 0, containers = 0;
            List<Chunk> tickets = scanChunkTickets(world, (min[0] + max[0]) / 2, (min[2] + max[2]) / 2, Math.max(max[0] - min[0], max[2] - min[2]) + 1);
            try {
                for (int x = min[0]; x <= max[0]; x++) for (int y = min[1]; y <= max[1]; y++) for (int z = min[2]; z <= max[2]; z++) {
                    Block block = world.getBlockAt(x, y, z);
                    materials.merge(block.getType().getKey().toString(), 1, Integer::sum);
                    if (!block.getType().isAir()) occupied++;
                    if (block.isLiquid()) liquids++;
                    if (block.getState() instanceof org.bukkit.block.Container) containers++;
                }
            } finally { for (Chunk chunk : tickets) releaseChunkTicketSoon(chunk); }
            sender.sendMessage(toJson(Map.of("ok", true, "dimension", world.getKey().toString(), "volume", volume, "occupied", occupied, "liquids", liquids, "containers", containers, "materials", materials, "players", others)));
        } catch (IllegalArgumentException error) {
            sender.sendMessage(toJson(Map.of("ok", false, "error", error.getMessage())));
        }
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

    private boolean godcheckat(CommandSender sender, String[] args) {
        if (args.length < 4) {
            sender.sendMessage("{\"error\":\"usage: godcheckat <dimension> <x> <y> <z>\"}");
            return true;
        }
        World world = worldFor(args[0]);
        if (world == null) {
            sender.sendMessage("{\"error\":\"dimension_not_found\",\"dimension\":\"" + escape(args[0]) + "\"}");
            return true;
        }
        try {
            int x = Integer.parseInt(args[1]);
            int y = Integer.parseInt(args[2]);
            int z = Integer.parseInt(args[3]);
            Chunk chunk = world.getChunkAt(x >> 4, z >> 4);
            chunk.addPluginChunkTicket(this);
            try {
                Block block = world.getBlockAt(x, y, z);
                sender.sendMessage(toJson(blockDescription(world, block, x, y, z)));
            } finally {
                releaseChunkTicketSoon(chunk);
            }
        } catch (NumberFormatException exception) {
            sender.sendMessage("{\"error\":\"coordinates_must_be_integers\"}");
        }
        return true;
    }

    private boolean godscanat(CommandSender sender, String[] args) {
        if (args.length < 4) {
            sender.sendMessage("{\"error\":\"usage: godscanat <dimension> <x> <y> <z> [radius] [vertical-radius]\"}");
            return true;
        }
        World world = worldFor(args[0]);
        if (world == null) {
            sender.sendMessage("{\"error\":\"dimension_not_found\",\"dimension\":\"" + escape(args[0]) + "\"}");
            return true;
        }
        try {
            int x = Integer.parseInt(args[1]);
            int y = Integer.parseInt(args[2]);
            int z = Integer.parseInt(args[3]);
            int radius = boundedArg(args, 4, 8, 1, 24);
            int vertical = boundedArg(args, 5, 5, 1, 16);
            Location location = new Location(world, x + 0.5, y, z + 0.5);
            List<Chunk> tickets = scanChunkTickets(world, x, z, radius);
            try {
                sender.sendMessage(toJson(sceneScan(world, location, null, radius, vertical)));
            } finally {
                for (Chunk chunk : tickets) {
                    releaseChunkTicketSoon(chunk);
                }
            }
        } catch (NumberFormatException exception) {
            sender.sendMessage("{\"error\":\"coordinates_must_be_integers\"}");
        }
        return true;
    }

    private World worldFor(String dimension) {
        NamespacedKey key = NamespacedKey.fromString(dimension);
        if (key != null) {
            World keyed = Bukkit.getWorld(key);
            if (keyed != null) {
                return keyed;
            }
        }
        return switch (dimension.toLowerCase()) {
            case "minecraft:overworld", "overworld" -> Bukkit.getWorld("world");
            case "minecraft:the_nether", "nether" -> Bukkit.getWorld("world_nether");
            case "minecraft:the_end", "end" -> Bukkit.getWorld("world_the_end");
            default -> Bukkit.getWorld(dimension);
        };
    }

    private List<Chunk> scanChunkTickets(World world, int centerX, int centerZ, int radius) {
        List<Chunk> chunks = new ArrayList<>();
        int minChunkX = (centerX - radius) >> 4;
        int maxChunkX = (centerX + radius) >> 4;
        int minChunkZ = (centerZ - radius) >> 4;
        int maxChunkZ = (centerZ + radius) >> 4;
        for (int chunkX = minChunkX; chunkX <= maxChunkX; chunkX++) {
            for (int chunkZ = minChunkZ; chunkZ <= maxChunkZ; chunkZ++) {
                Chunk chunk = world.getChunkAt(chunkX, chunkZ);
                chunk.addPluginChunkTicket(this);
                chunks.add(chunk);
            }
        }
        return chunks;
    }

    private void releaseChunkTicketSoon(Chunk chunk) {
        long expiry = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(5);
        ticketExpiry.put(chunk, expiry);
        Bukkit.getScheduler().runTaskLater(this, () -> {
            if (ticketExpiry.getOrDefault(chunk, 0L) == expiry) {
                ticketExpiry.remove(chunk);
                chunk.removePluginChunkTicket(this);
            }
        }, 100L);
    }

    private Map<String, Object> blockDescription(World world, Block block, int x, int y, int z) {
        Map<String, Object> description = new LinkedHashMap<>();
        description.put("dimension", world.getKey().toString());
        description.put("position", x + " " + y + " " + z);
        description.put("type", block.getType().getKey().toString());
        description.put("solid", block.getType().isSolid());
        description.put("passable", block.isPassable());
        description.put("liquid", block.isLiquid());
        description.put("replaceable", block.isReplaceable());
        return description;
    }

    private Map<String, Object> sceneScan(Player player, int radius, int vertical) {
        return sceneScan(player.getWorld(), player.getLocation(), player, radius, vertical);
    }

    private Map<String, Object> sceneScan(World world, Location location, Player player, int radius, int vertical) {
        int centerX = location.getBlockX();
        int centerY = location.getBlockY();
        int centerZ = location.getBlockZ();
        Map<String, Object> scan = new LinkedHashMap<>();
        scan.put("player", player == null ? "" : player.getName());
        scan.put("remote", player == null);
        scan.put("dimension", world.getKey().toString());
        scan.put("position", position(location));
        scan.put("block_position", centerX + " " + centerY + " " + centerZ);
        scan.put("eye_position", player == null ? position(location.clone().add(0, 1.62, 0)) : position(player.getEyeLocation()));
        scan.put("yaw", player == null ? 0.0 : round(player.getYaw()));
        scan.put("pitch", player == null ? 0.0 : round(player.getPitch()));
        scan.put("biome", location.getBlock().getBiome().getKey().toString());
        scan.put("time", world.getTime());
        scan.put("weather", world.isThundering() ? "thunder" : world.hasStorm() ? "rain" : "clear");

        List<Map<String, Object>> blocks = new ArrayList<>();
        for (int y = Math.max(world.getMinHeight(), centerY - vertical); y <= Math.min(world.getMaxHeight() - 1, centerY + vertical); y++) {
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
        for (Entity entity : world.getNearbyEntities(location, radius + 0.5, vertical + 3.0, radius + 0.5)) {
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
        return EventPayload.create(type, data);
    }

    private void sendEvent(Map<String, Object> event) {
        outbox.enqueue(toJson(event));
    }

    private static String position(Location location) {
        return round(location.getX()) + " " + round(location.getY()) + " " + round(location.getZ());
    }

    private static double round(double value) {
        return Math.round(value * 100.0) / 100.0;
    }

    private static String toJson(Object value) { return EventPayload.toJson(value); }
    private static String escape(String value) { return EventPayload.escape(value); }
}
