package com.minecraftbot.baritone;

import com.google.gson.JsonObject;
import net.minecraft.world.InteractionHand;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class BridgeContractValidationTest {

    @Test
    void coordinateValidatorAcceptsNestedPosition() {
        JsonObject position = new JsonObject();
        position.addProperty("x", 10);
        position.addProperty("y", 64);
        position.addProperty("z", -3);
        JsonObject params = new JsonObject();
        params.add("position", position);

        assertTrue(ParameterValidator.coordinates()
            .validate(params, "coordinates").isValid());
    }

    @Test
    void farmRejectsUnsupportedCropFilterBeforeExecution() throws Exception {
        JsonObject params = new JsonObject();
        params.addProperty("crop", "minecraft:wheat");

        CommandResult result = new FarmCommandHandler()
            .execute(params, null, null, null).get();

        assertFalse(result.isSuccess());
        assertTrue(result.getErrorMessage().contains("crop filtering"));
    }

    @Test
    void biomeScanRejectsUnboundedRadiusBeforeExecution() throws Exception {
        JsonObject params = new JsonObject();
        params.addProperty("radius", 513);

        CommandResult result = new BiomeScanCommandHandler()
            .execute(params, null, null, null).get();

        assertFalse(result.isSuccess());
        assertTrue(result.getErrorMessage().contains("between 0 and 512"));
    }

    @Test
    void useItemRejectsNegativeDurationBeforeExecution() throws Exception {
        JsonObject params = new JsonObject();
        params.addProperty("duration_ms", -1);

        CommandResult result = new UseItemCommandHandler()
            .execute(params, null, null, null).get();

        assertFalse(result.isSuccess());
        assertTrue(result.getErrorMessage().contains("non-negative"));
    }

    @Test
    void useItemParsesExplicitHandsAndDefaultsToMainHand() {
        assertNull(UseItemCommandHandler.requestedHand(new JsonObject()));
        assertEquals(
            InteractionHand.MAIN_HAND,
            UseItemCommandHandler.resolveHand(new JsonObject()));

        JsonObject main = new JsonObject();
        main.addProperty("hand", "main_hand");
        assertEquals(
            InteractionHand.MAIN_HAND,
            UseItemCommandHandler.requestedHand(main));
        assertEquals(
            InteractionHand.MAIN_HAND,
            UseItemCommandHandler.resolveHand(main));

        JsonObject offhand = new JsonObject();
        offhand.addProperty("hand", "OFF_HAND");
        assertEquals(
            InteractionHand.OFF_HAND,
            UseItemCommandHandler.requestedHand(offhand));
        assertEquals(
            InteractionHand.OFF_HAND,
            UseItemCommandHandler.resolveHand(offhand));
    }

    @Test
    void useItemRejectsUnknownHandBeforeExecution() throws Exception {
        JsonObject params = new JsonObject();
        params.addProperty("hand", "left_hand");

        CommandResult result = new UseItemCommandHandler()
            .execute(params, null, null, null).get();

        assertFalse(result.isSuccess());
        assertTrue(result.getErrorMessage().contains("MAIN_HAND or OFF_HAND"));
    }

    @Test
    void useItemReportsObservedOffhandBlockingState() {
        JsonObject data = new JsonObject();

        UseItemCommandHandler.addUseState(
            data, true, true, InteractionHand.OFF_HAND);

        assertTrue(data.get("is_using_item").getAsBoolean());
        assertTrue(data.get("is_blocking").getAsBoolean());
        assertEquals("OFF_HAND", data.get("active_hand").getAsString());
    }

    @Test
    void combatSlotsAndEquipTargetsUsePlayerInventoryIndices() {
        assertTrue(SelectSlotCommandHandler.validSlot(0));
        assertTrue(SelectSlotCommandHandler.validSlot(8));
        assertFalse(SelectSlotCommandHandler.validSlot(9));
        assertEquals(40, EquipCommandHandler.getTargetInventoryIndex("offhand"));
        assertEquals(39, EquipCommandHandler.getTargetInventoryIndex("helmet"));
        assertNull(EquipCommandHandler.getTargetInventoryIndex("hotbar"));
    }
}
