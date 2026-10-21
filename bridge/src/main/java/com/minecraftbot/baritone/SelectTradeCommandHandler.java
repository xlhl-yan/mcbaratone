package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonPrimitive;
import java.net.Socket;
import java.time.Duration;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import net.minecraft.client.Minecraft;
import net.minecraft.network.protocol.game.ServerboundSelectTradePacket;
import net.minecraft.world.entity.player.Inventory;
import net.minecraft.world.inventory.ContainerInput;
import net.minecraft.world.inventory.MerchantMenu;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.trading.MerchantOffer;
import net.minecraft.world.item.trading.MerchantOffers;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/** Selects a merchant offer and optionally executes a bounded number of trades. */
public class SelectTradeCommandHandler extends AsyncCommandHandler {
    private static final Logger LOGGER = LoggerFactory.getLogger(SelectTradeCommandHandler.class);
    private static final int RESULT_SLOT = 2;
    private static final int MAX_REQUESTED_TRADES = 64;
    private static final long COMMAND_TIMEOUT_NANOS = Duration.ofSeconds(30).toNanos();
    private static final long CLIENT_TASK_TIMEOUT_SECONDS = 3;
    private static final long POLL_INTERVAL_MILLIS = 25;

    @Override
    public String getCommandName() {
        return "select_trade";
    }

    @Override
    public CompletableFuture<CommandResult> execute(
            JsonObject params, Minecraft client, IBaritone baritone, Socket clientSocket) {
        Integer index = readInteger(params, "index", true);
        Integer count = readInteger(params, "count", false);
        if (index == null) {
            return completedError("index is required and must be an integer");
        }
        if (count == null) {
            return completedError("count must be an integer when provided");
        }
        if (index < 0) {
            return completedError("index must be non-negative");
        }
        if (count < 0 || count > MAX_REQUESTED_TRADES) {
            return completedError("count must be between 0 and " + MAX_REQUESTED_TRADES);
        }
        if (client.player == null || client.gameMode == null || client.getConnection() == null) {
            return completedError("Player, interaction manager, or server connection not available");
        }

        final int selectedIndex = index;
        final int requestedCount = count;
        return CompletableFuture.supplyAsync(() -> runTrade(client, selectedIndex, requestedCount));
    }

    private CommandResult runTrade(Minecraft client, int index, int count) {
        long deadline = System.nanoTime() + COMMAND_TIMEOUT_NANOS;
        try {
            Initialization initialization = client.submit(
                () -> initializeOnClientThread(client, index, count)
            ).get(CLIENT_TASK_TIMEOUT_SECONDS, TimeUnit.SECONDS);
            if (initialization.error() != null) {
                return CommandResult.error(initialization.error());
            }

            TradeContext context = initialization.context();
            JsonObject data = context.data();
            JsonArray acquiredResults = data.getAsJsonArray("acquired_results");
            if (count == 0) {
                Snapshot selected = snapshot(client, context);
                if (!selected.valid()) {
                    return failed(data, 0, selected.uses(), selected.error());
                }
                data.addProperty("uses_after", selected.uses());
                data.addProperty("result_ready", selected.resultReady());
                return CommandResult.success(data);
            }

            for (int tradeNumber = 1; tradeNumber <= count; tradeNumber++) {
                int expectedUsesBefore = context.usesBefore() + tradeNumber - 1;
                Preparation preparation = client.submit(
                    () -> prepareOnClientThread(client, context, expectedUsesBefore)
                ).get(CLIENT_TASK_TIMEOUT_SECONDS, TimeUnit.SECONDS);
                if (!preparation.ready()) {
                    return failed(data, tradeNumber - 1, preparation.uses(), preparation.error());
                }

                Snapshot ready = awaitResultReady(client, context, expectedUsesBefore, deadline);
                if (!ready.valid() || !ready.resultReady()) {
                    String reason = ready.error() != null
                        ? ready.error()
                        : "Timed out waiting for matching merchant result; trade inputs may be unavailable";
                    return failed(data, tradeNumber - 1, ready.uses(), reason);
                }

                client.submit(() -> clickResultOnClientThread(client, context))
                    .get(CLIENT_TASK_TIMEOUT_SECONDS, TimeUnit.SECONDS);

                int requiredInventoryCount = context.inventoryCountBefore()
                    + tradeNumber * context.resultCount();
                Snapshot completed = awaitCompletedTrade(
                    client,
                    context,
                    expectedUsesBefore + 1,
                    requiredInventoryCount,
                    deadline
                );
                // The inventory is the ground truth: the bought item really landed in the pack.
                // The offer "uses" counter can lag a tick or two behind on a busy server, and
                // demanding it to be exactly +1 is what turned successful trades into failures
                // (2026-10-21: seven shears bought, achievement earned, command reported failure).
                boolean inventoryCovers = completed.valid()
                    && completed.inventoryCount() >= requiredInventoryCount;
                if (!inventoryCovers) {
                    String reason = completed.error() != null
                        ? completed.error()
                        : "Trade was not confirmed by the inventory evidence (expected "
                            + requiredInventoryCount + " matching items, saw "
                            + (completed.valid() ? completed.inventoryCount() : -1) + ")";
                    return failed(data, tradeNumber - 1, completed.uses(), reason);
                }

                JsonObject acquired = ready.resultEvidence().deepCopy();
                acquired.addProperty("trade_number", tradeNumber);
                acquired.addProperty("uses_before", expectedUsesBefore);
                acquired.addProperty("uses_after", completed.uses());
                acquired.addProperty(
                    "inventory_count_before",
                    context.inventoryCountBefore() + (tradeNumber - 1) * context.resultCount()
                );
                acquired.addProperty("inventory_count_after", completed.inventoryCount());
                acquiredResults.add(acquired);
                data.addProperty("completed_count", tradeNumber);
                data.addProperty("uses_after", completed.uses());
            }

            // tryMoveItems fills the payment slots with everything the pack holds that matches
            // the cost, not just what the trades consume, so leftovers sit in the menu afterwards
            // (observed live 2026-10-21: 53 emeralds paid in, 2 spent, 51 left on the counter and
            // the operator thought the stack had vanished). Return them before reporting.
            returnUnusedInputs(client, context);

            Snapshot finalState = snapshot(client, context);
            if (!finalState.valid()) {
                return failed(data, count, finalState.uses(), finalState.error());
            }
            data.add("selected_offer", finalState.offerEvidence());
            return CommandResult.success(data);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return CommandResult.error("Trade operation interrupted");
        } catch (Exception e) {
            return CommandResult.error("Trade operation failed: " + e.getMessage());
        }
    }

    private Initialization initializeOnClientThread(Minecraft client, int index, int count) {
        if (client.player == null || client.gameMode == null || client.getConnection() == null) {
            return Initialization.error("Player, interaction manager, or server connection not available");
        }
        if (!(client.player.containerMenu instanceof MerchantMenu menu)) {
            return Initialization.error("Current screen is not a MerchantMenu");
        }
        MerchantOffers offers = menu.getOffers();
        if (index >= offers.size()) {
            return Initialization.error(
                "Trade index " + index + " is outside available offers 0.." + (offers.size() - 1));
        }
        MerchantOffer offer = offers.get(index);
        if (offer.isOutOfStock()) {
            return Initialization.error("Trade index " + index + " is out of stock");
        }
        int usesBefore = offer.getUses();
        int remainingUses = offer.getMaxUses() - usesBefore;
        if (count > remainingUses) {
            return Initialization.error(
                "Requested " + count + " trades but only " + remainingUses + " uses remain");
        }

        ItemStack resultTemplate = offer.getResult().copy();
        if (resultTemplate.isEmpty()) {
            return Initialization.error("Selected offer has an empty result");
        }
        // Refuse before touching the slots when the pack cannot pay for even one trade.
        // tryMoveItems(index) shoves whatever matches into the input slots without checking the
        // cost, so an under-stocked pack gets emptied into the menu and the trade then waits out
        // the whole deadline. Observed live on 2026-10-21: 10 black wool against a cost of 18,
        // every piece swallowed by the menu, 30s timeout, nothing bought.
        String shortfall = describeInputShortfall(client.player.getInventory(), offer, count);
        if (shortfall != null) {
            return Initialization.error("Not enough inputs to trade: " + shortfall);
        }

        int inventoryCountBefore = countMatching(client.player.getInventory(), resultTemplate);
        JsonObject data = baseResult(menu, offer, index, count, usesBefore, inventoryCountBefore);
        selectAndAutofill(client, menu, index);
        data.addProperty("selection_packet_sent", true);
        data.addProperty("inputs_autofilled", true);
        return Initialization.success(new TradeContext(
            menu,
            index,
            usesBefore,
            offer.getBaseCostA().copy(),
            offer.getCostB().copy(),
            resultTemplate,
            resultTemplate.getCount(),
            inventoryCountBefore,
            data
        ));
    }

    private Preparation prepareOnClientThread(
            Minecraft client, TradeContext context, int expectedUses) {
        String stateError = validateCurrentState(client, context);
        if (stateError != null) {
            return new Preparation(false, safeUses(context), stateError);
        }
        MerchantOffer currentOffer = context.menu().getOffers().get(context.index());
        int uses = currentOffer.getUses();
        if (uses != expectedUses) {
            return new Preparation(false, uses, "Selected offer uses changed unexpectedly");
        }
        if (currentOffer.isOutOfStock()) {
            return new Preparation(false, uses, "Selected offer became out of stock");
        }
        selectAndAutofill(client, context.menu(), context.index());
        return new Preparation(true, uses, null);
    }

    private void clickResultOnClientThread(Minecraft client, TradeContext context) {
        String stateError = validateCurrentState(client, context);
        if (stateError != null) {
            throw new IllegalStateException(stateError);
        }
        ItemStack result = context.menu().getSlot(RESULT_SLOT).getItem();
        if (!matchesResult(result, context.resultTemplate())) {
            throw new IllegalStateException("Merchant result slot no longer matches selected offer");
        }
        // PICKUP, not QUICK_MOVE. A shift-click on the result slot means "trade as many times as
        // the inputs allow", so a request for one trade silently became seven (observed live on
        // 2026-10-21: count=1, 64 emeralds spent on two-emerald shears, seven shears gained, the
        // offer counter at 7/12). It also made the caller's confirmation check fail, because the
        // offer uses moved by seven instead of one while the trade had in fact succeeded.
        client.gameMode.handleContainerInput(
            context.menu().containerId,
            RESULT_SLOT,
            0,
            ContainerInput.PICKUP,
            client.player
        );
        // A single PICKUP leaves the bought item on the cursor; park it in the pack so the
        // inventory evidence reflects the trade.
        int emptySlot = findEmptyPlayerSlotOnClientThread(client, context);
        if (emptySlot >= 0) {
            client.gameMode.handleContainerInput(
                context.menu().containerId,
                emptySlot,
                0,
                ContainerInput.PICKUP,
                client.player
            );
        }
    }

    /**
     * Move whatever is left in the payment slots back into the pack.
     *
     * <p>{@code tryMoveItems} shoves every matching item into the slots rather than just what the
     * requested trades need, so leftovers stay on the counter after the trade finishes. A
     * shift-click on an input slot moves the whole stack back out to the inventory.
     */
    private void returnUnusedInputs(Minecraft client, TradeContext context) {
        try {
            client.submit(() -> {
                for (int slot = 0; slot <= 1; slot++) {
                    if (!context.menu().getSlot(slot).getItem().isEmpty()) {
                        client.gameMode.handleContainerInput(
                            context.menu().containerId,
                            slot,
                            0,
                            ContainerInput.QUICK_MOVE,
                            client.player
                        );
                    }
                }
                return null;
            }).get(CLIENT_TASK_TIMEOUT_SECONDS, TimeUnit.SECONDS);
            Thread.sleep(120);
        } catch (Exception e) {
            LOGGER.warn("Could not return unused trade inputs: {}", e.getMessage());
        }
    }

    /**
     * First empty main-inventory slot of the merchant screen (0..2 are the trade's own slots,
     * 3..38 are the player's).
     */
    private int findEmptyPlayerSlotOnClientThread(Minecraft client, TradeContext context) {
        for (int slot = 3; slot <= 38; slot++) {
            if (context.menu().getSlot(slot).getItem().isEmpty()) {
                return slot;
            }
        }
        return -1;
    }

    private Snapshot awaitResultReady(
            Minecraft client, TradeContext context, int expectedUses, long deadline) throws Exception {
        Snapshot current = snapshot(client, context);
        while (System.nanoTime() < deadline) {
            if (!current.valid() || current.uses() != expectedUses || current.resultReady()) {
                return current;
            }
            Thread.sleep(POLL_INTERVAL_MILLIS);
            current = snapshot(client, context);
        }
        return current;
    }

    private Snapshot awaitCompletedTrade(
            Minecraft client,
            TradeContext context,
            int expectedUses,
            int requiredInventoryCount,
            long deadline) throws Exception {
        Snapshot current = snapshot(client, context);
        while (System.nanoTime() < deadline) {
            if (!current.valid()
                    || current.uses() > expectedUses
                    || confirmsCompletedTrade(
                        current.uses(),
                        expectedUses,
                        current.inventoryCount(),
                        requiredInventoryCount
                    )) {
                return current;
            }
            Thread.sleep(POLL_INTERVAL_MILLIS);
            current = snapshot(client, context);
        }
        return current;
    }

    private Snapshot snapshot(Minecraft client, TradeContext context) throws Exception {
        return client.submit(() -> snapshotOnClientThread(client, context))
            .get(CLIENT_TASK_TIMEOUT_SECONDS, TimeUnit.SECONDS);
    }

    private Snapshot snapshotOnClientThread(Minecraft client, TradeContext context) {
        String stateError = validateCurrentState(client, context);
        if (stateError != null) {
            return Snapshot.error(safeUses(context), stateError);
        }
        MerchantOffer offer = context.menu().getOffers().get(context.index());
        ItemStack result = context.menu().getSlot(RESULT_SLOT).getItem();
        return new Snapshot(
            true,
            offer.getUses(),
            countMatching(client.player.getInventory(), context.resultTemplate()),
            matchesResult(result, context.resultTemplate()),
            ItemStackJsonSerializer.serialize(result.copy(), RESULT_SLOT),
            ItemStackJsonSerializer.serializeMerchantOffer(offer, context.index()),
            null
        );
    }

    private String validateCurrentState(Minecraft client, TradeContext context) {
        if (client.player == null || client.gameMode == null || client.getConnection() == null) {
            return "Player, interaction manager, or server connection became unavailable";
        }
        if (client.player.containerMenu != context.menu()) {
            return "Merchant screen changed before operation completed";
        }
        MerchantOffers offers = context.menu().getOffers();
        if (context.index() >= offers.size()
                || !matchesSelectedOffer(offers.get(context.index()), context)) {
            return "Selected offer changed before operation completed";
        }
        return null;
    }

    private void selectAndAutofill(Minecraft client, MerchantMenu menu, int index) {
        menu.setSelectionHint(index);
        menu.tryMoveItems(index);
        client.getConnection().send(new ServerboundSelectTradePacket(index));
    }

    /**
     * Describe what the pack is missing to pay for {@code count} trades, or null when it can pay
     * for all of them.
     *
     * <p>Checked before any slot is touched: {@code tryMoveItems} does not look at the cost, so
     * without this an under-stocked pack gets emptied into the menu and the trade waits out the
     * entire deadline instead of failing fast.
     */
    private String describeInputShortfall(Inventory inventory, MerchantOffer offer, int count) {
        StringBuilder sb = new StringBuilder();
        String a = costShortfall(inventory, offer.getBaseCostA(), count);
        String b = costShortfall(inventory, offer.getCostB(), count);
        if (a != null) {
            sb.append(a);
        }
        if (b != null) {
            if (sb.length() > 0) {
                sb.append("; ");
            }
            sb.append(b);
        }
        return sb.length() == 0 ? null : sb.toString();
    }

    private String costShortfall(Inventory inventory, ItemStack cost, int count) {
        if (cost == null || cost.isEmpty()) {
            return null;
        }
        int needed = cost.getCount() * count;
        int have = countMatching(inventory, cost);
        if (have >= needed) {
            return null;
        }
        return "need " + needed + "x " + cost.getHoverName().getString() + " but have " + have;
    }

    private int countMatching(Inventory inventory, ItemStack expected) {
        int count = 0;
        for (int slot = 0; slot < inventory.getContainerSize(); slot++) {
            ItemStack candidate = inventory.getItem(slot);
            if (ItemStack.isSameItemSameComponents(candidate, expected)) {
                count += candidate.getCount();
            }
        }
        return count;
    }

    private boolean matchesResult(ItemStack actual, ItemStack expected) {
        return !actual.isEmpty()
            && ItemStack.isSameItemSameComponents(actual, expected)
            && actual.getCount() >= expected.getCount();
    }

    private boolean matchesSelectedOffer(MerchantOffer actual, TradeContext expected) {
        return sameStackAndCount(actual.getBaseCostA(), expected.baseCostA())
            && sameStackAndCount(actual.getCostB(), expected.costB())
            && sameStackAndCount(actual.getResult(), expected.resultTemplate());
    }

    private boolean sameStackAndCount(ItemStack actual, ItemStack expected) {
        return ItemStack.isSameItemSameComponents(actual, expected)
            && actual.getCount() == expected.getCount();
    }

    private JsonObject baseResult(
            MerchantMenu menu,
            MerchantOffer offer,
            int index,
            int count,
            int usesBefore,
            int inventoryCountBefore) {
        JsonObject data = new JsonObject();
        data.addProperty("sync_id", menu.containerId);
        data.addProperty("selected_index", index);
        data.addProperty("requested_count", count);
        data.addProperty("completed_count", 0);
        data.addProperty("uses_before", usesBefore);
        data.addProperty("uses_after", usesBefore);
        data.addProperty("inventory_count_before", inventoryCountBefore);
        data.addProperty("selection_packet_sent", false);
        data.addProperty("inputs_autofilled", false);
        data.add("selected_offer", ItemStackJsonSerializer.serializeMerchantOffer(offer, index));
        data.add("acquired_results", new JsonArray());
        return data;
    }

    private CommandResult failed(JsonObject data, int completedCount, int usesAfter, String message) {
        data.addProperty("completed_count", completedCount);
        data.addProperty("uses_after", usesAfter);
        data.addProperty("failure_reason", message);
        return new CommandResult(false, data, message);
    }

    private int safeUses(TradeContext context) {
        try {
            MerchantOffers offers = context.menu().getOffers();
            if (context.index() < offers.size()) {
                return offers.get(context.index()).getUses();
            }
            return context.usesBefore();
        } catch (Exception ignored) {
            return context.usesBefore();
        }
    }

    private CompletableFuture<CommandResult> completedError(String message) {
        return CompletableFuture.completedFuture(CommandResult.error(message));
    }

    static Integer readInteger(JsonObject params, String field, boolean required) {
        if (!params.has(field)) {
            return required ? null : 0;
        }
        JsonElement element = params.get(field);
        if (!(element instanceof JsonPrimitive primitive) || !primitive.isNumber()) {
            return null;
        }
        double numeric = primitive.getAsDouble();
        if (!Double.isFinite(numeric) || numeric != Math.rint(numeric)
                || numeric < Integer.MIN_VALUE || numeric > Integer.MAX_VALUE) {
            return null;
        }
        return (int) numeric;
    }

    static boolean confirmsCompletedTrade(
            int actualUses, int expectedUses, int inventoryCount, int requiredInventoryCount) {
        return actualUses == expectedUses && inventoryCount >= requiredInventoryCount;
    }

    private record TradeContext(
        MerchantMenu menu,
        int index,
        int usesBefore,
        ItemStack baseCostA,
        ItemStack costB,
        ItemStack resultTemplate,
        int resultCount,
        int inventoryCountBefore,
        JsonObject data
    ) {
    }

    private record Initialization(TradeContext context, String error) {
        static Initialization success(TradeContext context) {
            return new Initialization(context, null);
        }

        static Initialization error(String error) {
            return new Initialization(null, error);
        }
    }

    private record Preparation(boolean ready, int uses, String error) {
    }

    private record Snapshot(
        boolean valid,
        int uses,
        int inventoryCount,
        boolean resultReady,
        JsonObject resultEvidence,
        JsonObject offerEvidence,
        String error
    ) {
        static Snapshot error(int uses, String error) {
            return new Snapshot(false, uses, 0, false, new JsonObject(), new JsonObject(), error);
        }
    }
}
