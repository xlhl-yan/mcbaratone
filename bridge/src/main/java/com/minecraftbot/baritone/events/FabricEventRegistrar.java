package com.minecraftbot.baritone.events;

import com.minecraftbot.baritone.EventManager;
import com.minecraftbot.baritone.BaritoneAPIBridge;
import com.minecraftbot.baritone.WakenSessionStore;
import com.google.gson.JsonObject;
import net.fabricmc.fabric.api.client.message.v1.ClientReceiveMessageEvents;
import net.fabricmc.fabric.api.event.player.AttackBlockCallback;
import net.fabricmc.fabric.api.event.player.UseBlockCallback;
import net.fabricmc.fabric.api.event.player.AttackEntityCallback;
import net.fabricmc.fabric.api.event.player.UseEntityCallback;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.InteractionResult;
import net.minecraft.world.level.block.state.BlockState;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Registers Fabric API event listeners and forwards events to the EventManager.
 */
public class FabricEventRegistrar {

    private static final Logger LOGGER = LoggerFactory.getLogger("baritone-event-registrar");
    private final EventManager eventManager;

    public FabricEventRegistrar(EventManager eventManager) {
        this.eventManager = eventManager;
    }

    public void registerEvents() {
        // Chat message listener
        ClientReceiveMessageEvents.GAME.register((message, overlay) -> {
            if (!overlay) {
                JsonObject data = new JsonObject();
                data.addProperty("message", message.getString());
                eventManager.publishEvent(EventManager.EventType.CHAT, data);
            }
        });

        // Player chat listener. The GAME channel above only carries system/game
        // messages, so on a multiplayer server every line a player typed was
        // invisible to the bridge. CHAT also hands us the sender's GameProfile,
        // which is what lets a driver tell "who said this" from "the server said
        // this" instead of guessing from the rendered line.
        ClientReceiveMessageEvents.CHAT.register(
                (message, signedMessage, sender, params, timestamp) -> {
                    String text = message.getString();
                    String senderName = (sender != null) ? sender.name() : null;
                    JsonObject data = new JsonObject();
                    data.addProperty("message", text);
                    if (senderName != null && !senderName.isEmpty()) {
                        data.addProperty("sender", senderName);
                    }
                    eventManager.publishEvent(EventManager.EventType.CHAT, data,
                            EventManager.Priority.NORMAL, "player_chat");

                    handleWakenCommand(text, senderName);
                });

        // Block break listener (when player starts breaking a block)
        AttackBlockCallback.EVENT.register((player, world, hand, pos, direction) -> {
            if (player == Minecraft.getInstance().player) {
                BlockState state = world.getBlockState(pos);
                JsonObject data = new JsonObject();
                data.addProperty("x", pos.getX());
                data.addProperty("y", pos.getY());
                data.addProperty("z", pos.getZ());
                data.addProperty("block_type", BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString());
                data.addProperty("dimension", world.dimension().identifier().toString());
                eventManager.publishEvent(EventManager.EventType.BLOCK_BREAK, data, EventManager.Priority.NORMAL, "block_break");
            }
            return InteractionResult.PASS;
        });

        // Block place listener (when player uses/places a block)
        UseBlockCallback.EVENT.register((player, world, hand, hitResult) -> {
            if (player == Minecraft.getInstance().player) {
                BlockPos pos = hitResult.getBlockPos();
                BlockState state = world.getBlockState(pos);
                JsonObject data = new JsonObject();
                data.addProperty("x", pos.getX());
                data.addProperty("y", pos.getY());
                data.addProperty("z", pos.getZ());
                data.addProperty("block_type", BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString());
                data.addProperty("dimension", world.dimension().identifier().toString());
                eventManager.publishEvent(EventManager.EventType.BLOCK_PLACE, data, EventManager.Priority.NORMAL, "block_place");
            }
            return InteractionResult.PASS;
        });

        // Entity interaction listeners
        AttackEntityCallback.EVENT.register((player, world, hand, entity, hitResult) -> {
            if (player == Minecraft.getInstance().player) {
                JsonObject data = new JsonObject();
                data.addProperty("x", entity.getX());
                data.addProperty("y", entity.getY());
                data.addProperty("z", entity.getZ());
                data.addProperty("entity_type", BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString());
                data.addProperty("entity_id", entity.getId());
                data.addProperty("dimension", world.dimension().identifier().toString());
                data.addProperty("item_used", BuiltInRegistries.ITEM.getKey(player.getItemInHand(hand).getItem()).toString());
                data.addProperty("success", true);
                eventManager.publishEvent(EventManager.EventType.ENTITY_ATTACK, data, EventManager.Priority.NORMAL, "entity_attack");
            }
            return InteractionResult.PASS;
        });

        UseEntityCallback.EVENT.register((player, world, hand, entity, hitResult) -> {
            if (player == Minecraft.getInstance().player) {
                String type = BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString();
                EventManager.EventType eventType = null;

                if (type.equals("minecraft:wolf") || type.equals("minecraft:cat") || type.equals("minecraft:parrot")) {
                    eventType = EventManager.EventType.ENTITY_TAME;
                } else if (type.equals("minecraft:sheep")) {
                    eventType = EventManager.EventType.ENTITY_SHEAR;
                } else if (type.equals("minecraft:cow") || type.equals("minecraft:mooshroom")) {
                    eventType = EventManager.EventType.ENTITY_MILK;
                }

                if (eventType != null) {
                    JsonObject data = new JsonObject();
                    data.addProperty("x", entity.getX());
                    data.addProperty("y", entity.getY());
                    data.addProperty("z", entity.getZ());
                    data.addProperty("entity_type", type);
                    data.addProperty("entity_id", entity.getId());
                    data.addProperty("dimension", world.dimension().identifier().toString());
                    data.addProperty("item_used", BuiltInRegistries.ITEM.getKey(player.getItemInHand(hand).getItem()).toString());
                    data.addProperty("success", true);
                    eventManager.publishEvent(eventType, data, EventManager.Priority.NORMAL, "entity_" + eventType.name().toLowerCase().substring(7));
                }
            }
            return InteractionResult.PASS;
        });

        LOGGER.info("Fabric event listeners registered");
    }

    /**
     * 处理游戏内的设置命令（{@code 唤醒会话 <id>} / {@code !waken <id>} 等）：
     * 把「DSH 该唤醒哪个会话」写进本地配置。
     *
     * <p>两个客户端都装了 bridge，同一条聊天两边都会收到，所以：
     * <ul>
     *   <li><b>写盘不受发送者限制</b> —— 玩家可能在自己客户端敲，也可能切到 AI
     *       客户端敲。两种情况下 AI 这侧的配置都必须落上，因为 DSH 读的是它。</li>
     *   <li><b>只有回话去重</b> —— 只在「发送者不是自己」时吭声。这样无论命令从
     *       哪边发出，都恰好只有一个客户端回复。</li>
     * </ul>
     */
    private void handleWakenCommand(String rawMessage, String senderName) {
        String wanted = WakenSessionStore.matchCommand(rawMessage);
        if (wanted == null) {
            return;
        }
        Minecraft client = Minecraft.getInstance();
        if (client.player == null) {
            return;
        }

        boolean written = WakenSessionStore.write(wanted);

        String self = client.player.getGameProfile().name();
        if (senderName == null || senderName.equals(self)) {
            return; // 这半边只负责写，不吭声
        }

        final String reply;
        if (!written) {
            reply = "[bridge] 写入失败，检查游戏目录 config 的写权限";
        } else if (wanted.isEmpty()) {
            reply = "[bridge] 已清除，游戏消息回到「自动挑最近会话」";
        } else {
            reply = "[bridge] 已记住唤醒会话：" + wanted;
        }
        client.player.connection.sendChat(reply);
    }
}
