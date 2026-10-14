package com.minecraftbot.baritone;

import com.google.gson.JsonObject;
import baritone.api.IBaritone;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.Mock;
import org.mockito.MockitoAnnotations;

import java.net.Socket;
import java.util.concurrent.CompletableFuture;
import net.minecraft.client.Minecraft;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

public class CommandHandlerFactoryTest {

    @Mock
    private Minecraft mockClient;

    @Mock
    private IBaritone mockBaritone;

    @Mock
    private Socket mockSocket;

    public static class TestCommandHandler implements CommandHandler {
        public TestCommandHandler() {} // Explicit public constructor
        @Override
        public CompletableFuture<CommandResult> handle(JsonObject params, Minecraft client, IBaritone baritone, Socket clientSocket) {
            return CompletableFuture.completedFuture(CommandResult.success(new JsonObject()));
        }

        @Override
        public String getCommandName() {
            return "test";
        }
    }

    @BeforeEach
    void setUp() {
        MockitoAnnotations.openMocks(this);
        // Clear registry before each test to ensure clean state
        CommandHandlerFactory.clearRegistry();
    }

    @AfterEach
    void tearDown() {
        // Clean up after each test, then put the built-in handlers back.
        // clearRegistry() wipes the static registry for the whole JVM and the
        // static initializer only ever runs once, so without the restore every
        // later test class that looks up a real command fails.
        CommandHandlerFactory.clearRegistry();
        CommandHandlerFactory.registerDefaultHandlers();
    }

    @Test
    void testGetHandler_WithNullCommandName_ShouldReturnNull() {
        CommandHandler result = CommandHandlerFactory.getHandler(null);
        assertNull(result);
    }

    @Test
    void testGetHandler_WithUnregisteredCommand_ShouldReturnNull() {
        CommandHandler result = CommandHandlerFactory.getHandler("nonexistent");
        assertNull(result);
    }

    @Test
    void testRegisterHandler_ShouldStoreHandlerClass() {
        CommandHandlerFactory.registerHandler("test", TestCommandHandler.class);
        assertTrue(CommandHandlerFactory.hasHandler("test"));
    }

    @Test
    void testGetHandler_ShouldReturnSingletonInstance() {
        CommandHandlerFactory.registerHandler("test", TestCommandHandler.class);

        CommandHandler handler1 = CommandHandlerFactory.getHandler("test");
        CommandHandler handler2 = CommandHandlerFactory.getHandler("test");

        assertNotNull(handler1);
        assertSame(handler1, handler2); // Should be the same instance (singleton)
    }

    @Test
    void testGetHandler_ShouldCreateInstanceFromRegisteredClass() {
        CommandHandlerFactory.registerHandler("test", TestCommandHandler.class);

        CommandHandler handler = CommandHandlerFactory.getHandler("test");

        assertNotNull(handler);
        assertEquals("test", handler.getCommandName());
        assertInstanceOf(TestCommandHandler.class, handler);
    }

    @Test
    void testHasHandler_WithRegisteredCommand_ShouldReturnTrue() {
        CommandHandlerFactory.registerHandler("test", TestCommandHandler.class);
        assertTrue(CommandHandlerFactory.hasHandler("test"));
    }

    @Test
    void testHasHandler_WithUnregisteredCommand_ShouldReturnFalse() {
        assertFalse(CommandHandlerFactory.hasHandler("nonexistent"));
    }

    @Test
    void testHasHandler_WithNullCommand_ShouldReturnFalse() {
        assertFalse(CommandHandlerFactory.hasHandler(null));
    }

    @Test
    void testGetRegisteredCommands_ShouldReturnAllRegisteredCommands() {
        CommandHandlerFactory.registerHandler("cmd1", TestCommandHandler.class);
        CommandHandlerFactory.registerHandler("cmd2", TestCommandHandler.class);

        String[] commands = CommandHandlerFactory.getRegisteredCommands();
        assertEquals(2, commands.length);
        assertTrue(java.util.Arrays.asList(commands).contains("cmd1"));
        assertTrue(java.util.Arrays.asList(commands).contains("cmd2"));
    }

    @Test
    void testGetRegisteredHandlerCount_ShouldReturnCorrectCount() {
        assertEquals(0, CommandHandlerFactory.getRegisteredHandlerCount());

        CommandHandlerFactory.registerHandler("cmd1", TestCommandHandler.class);
        assertEquals(1, CommandHandlerFactory.getRegisteredHandlerCount());

        CommandHandlerFactory.registerHandler("cmd2", TestCommandHandler.class);
        assertEquals(2, CommandHandlerFactory.getRegisteredHandlerCount());
    }

    @Test
    void testClearRegistry_ShouldRemoveAllHandlers() {
        CommandHandlerFactory.registerHandler("cmd1", TestCommandHandler.class);
        CommandHandlerFactory.registerHandler("cmd2", TestCommandHandler.class);
        CommandHandler handler = CommandHandlerFactory.getHandler("cmd1");

        assertTrue(CommandHandlerFactory.hasHandler("cmd1"));
        assertEquals(2, CommandHandlerFactory.getRegisteredHandlerCount());

        CommandHandlerFactory.clearRegistry();

        assertFalse(CommandHandlerFactory.hasHandler("cmd1"));
        assertEquals(0, CommandHandlerFactory.getRegisteredHandlerCount());
        // After clearing, getHandler should return null even if we had an instance before
        assertNull(CommandHandlerFactory.getHandler("cmd1"));
    }

    @Test
    void testGetHandler_WithInstantiationError_ShouldThrow() {
        // Create a handler class that cannot be instantiated
        CommandHandlerFactory.registerHandler("bad", BadCommandHandler.class);

        // Returning null here made a registered-but-broken handler look exactly
        // like an unregistered command, so callers answered with the legacy
        // "Unknown command or no handler found" -- which is how three working
        // commands appeared broken after an upgrade. It must surface as an error.
        IllegalStateException thrown = assertThrows(
            IllegalStateException.class,
            () -> CommandHandlerFactory.getHandler("bad"));
        assertTrue(thrown.getMessage().contains("bad"),
            "message should name the failing command, was: " + thrown.getMessage());
        assertNotNull(thrown.getCause(), "the constructor failure should be kept as the cause");
    }

    private static class BadCommandHandler implements CommandHandler {
        // Constructor that throws exception
        public BadCommandHandler() {
            throw new RuntimeException("Cannot instantiate");
        }

        @Override
        public CompletableFuture<CommandResult> handle(JsonObject params, Minecraft client, IBaritone baritone, Socket clientSocket) {
            return CompletableFuture.completedFuture(null);
        }

        @Override
        public String getCommandName() {
            return "bad";
        }
    }

    @Test
    void testStaticInitialization_RegistersDefaultHandlers() {
        // The static block in CommandHandlerFactory should register default handlers
        // Since we clear the registry in setUp, we need to test this differently
        // This test verifies that the registry works after clearing

        // First, clear to ensure clean state
        CommandHandlerFactory.clearRegistry();

        // Manually register some handlers to simulate static initialization
        CommandHandlerFactory.registerHandler("goto", GotoCommandHandler.class);
        CommandHandlerFactory.registerHandler("mine", MineCommandHandler.class);

        assertTrue(CommandHandlerFactory.hasHandler("goto"));
        assertTrue(CommandHandlerFactory.hasHandler("mine"));
        assertFalse(CommandHandlerFactory.hasHandler("nonexistent"));
    }

    @Test
    void testInstanceCaching_PreventsRecreation() {
        CommandHandlerFactory.registerHandler("test", TestCommandHandler.class);

        // Get handler twice
        CommandHandler handler1 = CommandHandlerFactory.getHandler("test");
        CommandHandler handler2 = CommandHandlerFactory.getHandler("test");

        // Should be the same instance
        assertSame(handler1, handler2);
    }

    @Test
    void testMultipleCommands_SameHandlerClass() {
        // Register the same handler class for multiple commands
        CommandHandlerFactory.registerHandler("cmd1", TestCommandHandler.class);
        CommandHandlerFactory.registerHandler("cmd2", TestCommandHandler.class);

        CommandHandler handler1 = CommandHandlerFactory.getHandler("cmd1");
        CommandHandler handler2 = CommandHandlerFactory.getHandler("cmd2");

        // Should be different instances since they are for different commands
        assertNotSame(handler1, handler2);
        assertEquals("test", handler1.getCommandName());
        assertEquals("test", handler2.getCommandName());
    }

    @Test
    void testAdvancedCraftCommandHandlerRegistration() {
        // Test registration of AdvancedCraftCommandHandler
        CommandHandlerFactory.registerHandler("craft_advanced", AdvancedCraftCommandHandler.class);
        assertTrue(CommandHandlerFactory.hasHandler("craft_advanced"));

        CommandHandler handler = CommandHandlerFactory.getHandler("craft_advanced");
        assertNotNull(handler);
        assertEquals("craft_advanced", handler.getCommandName());
        assertInstanceOf(AdvancedCraftCommandHandler.class, handler);
    }

    @Test
    void testEntityInteractionCommandHandlerRegistration() {
        // Test registration of EntityInteractionCommandHandler
        CommandHandlerFactory.registerHandler("entity_interact", EntityInteractionCommandHandler.class);
        assertTrue(CommandHandlerFactory.hasHandler("entity_interact"));

        CommandHandler handler = CommandHandlerFactory.getHandler("entity_interact");
        assertNotNull(handler);
        assertEquals("entity_interact", handler.getCommandName());
        assertInstanceOf(EntityInteractionCommandHandler.class, handler);
    }

    @Test
    void testSequenceCommandHandlerRegistration() {
        // Test registration of SequenceCommandHandler
        CommandHandlerFactory.registerHandler("sequence", SequenceCommandHandler.class);
        assertTrue(CommandHandlerFactory.hasHandler("sequence"));

        CommandHandler handler = CommandHandlerFactory.getHandler("sequence");
        assertNotNull(handler);
        assertEquals("sequence", handler.getCommandName());
        assertInstanceOf(SequenceCommandHandler.class, handler);
    }

    @Test
    void testAllNewHandlersRegistered() {
        // Test that all new advanced command handlers can be registered
        CommandHandlerFactory.registerHandler("craft_advanced", AdvancedCraftCommandHandler.class);
        CommandHandlerFactory.registerHandler("entity_interact", EntityInteractionCommandHandler.class);
        CommandHandlerFactory.registerHandler("sequence", SequenceCommandHandler.class);

        String[] registered = CommandHandlerFactory.getRegisteredCommands();
        assertTrue(java.util.Arrays.asList(registered).contains("craft_advanced"));
        assertTrue(java.util.Arrays.asList(registered).contains("entity_interact"));
        assertTrue(java.util.Arrays.asList(registered).contains("sequence"));

        assertEquals(3, CommandHandlerFactory.getRegisteredHandlerCount());
    }

    @Test
    void testHandlerSingletonPerCommand() {
        // Test that each command gets its own singleton instance
        CommandHandlerFactory.registerHandler("craft_advanced", AdvancedCraftCommandHandler.class);
        CommandHandlerFactory.registerHandler("entity_interact", EntityInteractionCommandHandler.class);

        CommandHandler craft1 = CommandHandlerFactory.getHandler("craft_advanced");
        CommandHandler craft2 = CommandHandlerFactory.getHandler("craft_advanced");
        CommandHandler entity1 = CommandHandlerFactory.getHandler("entity_interact");

        assertSame(craft1, craft2); // Same command, same instance
        assertNotSame(craft1, entity1); // Different commands, different instances
    }
}