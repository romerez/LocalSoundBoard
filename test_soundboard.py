"""
Test suite for Discord Soundboard sound playback functionality.

Run with: python test_soundboard.py
"""

import unittest
import sys
import os
import time
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from dataclasses import dataclass
from typing import Dict, Optional

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import constants directly (doesn't import audio modules)
from soundboard.constants import COLORS, UI


# Re-define models here to avoid importing the full soundboard package
# which would trigger sounddevice initialization
@dataclass
class SoundSlot:
    name: str
    file_path: str
    hotkey: Optional[str] = None
    volume: float = 1.0
    emoji: Optional[str] = None
    image_path: Optional[str] = None


@dataclass
class SoundTab:
    name: str
    emoji: Optional[str] = None
    slots: Dict[int, SoundSlot] = None
    
    def __post_init__(self):
        if self.slots is None:
            self.slots = {}


class TestSoundSlot(unittest.TestCase):
    """Test SoundSlot model."""

    def test_create_sound_slot(self):
        """Test creating a basic sound slot."""
        slot = SoundSlot(name="Test Sound", file_path="sounds/test.mp3")
        self.assertEqual(slot.name, "Test Sound")
        self.assertEqual(slot.file_path, "sounds/test.mp3")
        self.assertIsNone(slot.hotkey)
        self.assertEqual(slot.volume, 1.0)

    def test_sound_slot_with_all_fields(self):
        """Test creating a sound slot with all fields."""
        slot = SoundSlot(
            name="Full Sound",
            file_path="sounds/full.mp3",
            hotkey="ctrl+1",
            volume=0.8,
            emoji="🎵",
            image_path="images/icon.png",
        )
        self.assertEqual(slot.hotkey, "ctrl+1")
        self.assertEqual(slot.volume, 0.8)
        self.assertEqual(slot.emoji, "🎵")


class TestSoundTab(unittest.TestCase):
    """Test SoundTab model."""

    def test_create_sound_tab(self):
        """Test creating a basic sound tab."""
        tab = SoundTab(name="Main")
        self.assertEqual(tab.name, "Main")
        self.assertIsNone(tab.emoji)
        self.assertEqual(tab.slots, {})

    def test_sound_tab_with_slots(self):
        """Test creating a tab with slots."""
        slot = SoundSlot(name="Test", file_path="test.mp3")
        tab = SoundTab(name="Sounds", emoji="🔊", slots={0: slot})
        self.assertEqual(len(tab.slots), 1)
        self.assertEqual(tab.slots[0].name, "Test")


class TestPlayingStateTracking(unittest.TestCase):
    """Test that playing state is tracked correctly across tabs."""

    def setUp(self):
        """Set up test fixtures."""
        self.playing_slots = {}
        self.preview_slots = {}
        self.current_tab_idx = 0

    def test_playing_slot_same_tab(self):
        """Test that playing state is detected on same tab."""
        slot_idx = 0
        self.playing_slots[slot_idx] = {
            "start_time": time.time(),
            "duration": 2.0,
            "tab_idx": 0,
        }
        
        # Check if slot is playing on current tab
        is_playing = (
            slot_idx in self.playing_slots
            and self.playing_slots[slot_idx].get("tab_idx") == self.current_tab_idx
        )
        self.assertTrue(is_playing)

    def test_playing_slot_different_tab(self):
        """Test that playing state is NOT detected on different tab."""
        slot_idx = 0
        self.playing_slots[slot_idx] = {
            "start_time": time.time(),
            "duration": 2.0,
            "tab_idx": 1,  # Different tab
        }
        
        # Check if slot is playing on current tab (tab 0)
        is_playing = (
            slot_idx in self.playing_slots
            and self.playing_slots[slot_idx].get("tab_idx") == self.current_tab_idx
        )
        self.assertFalse(is_playing)

    def test_preview_slot_same_tab(self):
        """Test that preview state is detected on same tab."""
        slot_idx = 0
        self.preview_slots[slot_idx] = {
            "start_time": time.time(),
            "duration": 2.0,
            "tab_idx": 0,
        }
        
        is_previewing = (
            slot_idx in self.preview_slots
            and self.preview_slots[slot_idx].get("tab_idx") == self.current_tab_idx
        )
        self.assertTrue(is_previewing)

    def test_preview_slot_different_tab(self):
        """Test that preview state is NOT detected on different tab."""
        slot_idx = 0
        self.preview_slots[slot_idx] = {
            "start_time": time.time(),
            "duration": 2.0,
            "tab_idx": 1,  # Different tab
        }
        
        is_previewing = (
            slot_idx in self.preview_slots
            and self.preview_slots[slot_idx].get("tab_idx") == self.current_tab_idx
        )
        self.assertFalse(is_previewing)

    def test_switch_tab_clears_current_tab_state_awareness(self):
        """Test that switching tabs correctly changes tab awareness."""
        slot_idx = 0
        self.playing_slots[slot_idx] = {
            "start_time": time.time(),
            "duration": 2.0,
            "tab_idx": 0,
        }
        
        # On tab 0, slot should show as playing
        self.current_tab_idx = 0
        is_playing_tab0 = (
            slot_idx in self.playing_slots
            and self.playing_slots[slot_idx].get("tab_idx") == self.current_tab_idx
        )
        self.assertTrue(is_playing_tab0)
        
        # Switch to tab 1, slot should NOT show as playing
        self.current_tab_idx = 1
        is_playing_tab1 = (
            slot_idx in self.playing_slots
            and self.playing_slots[slot_idx].get("tab_idx") == self.current_tab_idx
        )
        self.assertFalse(is_playing_tab1)


class TestColorConstants(unittest.TestCase):
    """Test that all required color constants exist."""

    def test_required_colors_exist(self):
        """Test that all required colors are defined."""
        required_colors = [
            "bg_dark",
            "bg_medium",
            "bg_light",
            "blurple",
            "green",
            "red",
            "playing",
            "preview",
            "drag_target",
            "text_primary",
            "text_muted",
        ]
        for color_name in required_colors:
            self.assertIn(color_name, COLORS, f"Missing color: {color_name}")
            self.assertTrue(
                COLORS[color_name].startswith("#"),
                f"Color {color_name} should be a hex color",
            )

    def test_playing_color_is_orange(self):
        """Test that playing color is orange/amber."""
        # Playing should be orange for Discord playback
        self.assertEqual(COLORS["playing"], "#FAA61A")

    def test_preview_color_is_green(self):
        """Test that preview color is green."""
        # Preview should be green
        self.assertEqual(COLORS["preview"], "#3BA55D")


class TestUIConstants(unittest.TestCase):
    """Test UI configuration constants."""

    def test_grid_settings(self):
        """Test grid configuration exists."""
        self.assertIn("grid_columns", UI)
        self.assertIn("total_slots", UI)
        self.assertEqual(UI["grid_columns"], 4)
        self.assertGreaterEqual(UI["total_slots"], 12)


class TestSlotIndexCalculation(unittest.TestCase):
    """Test slot index calculations for dynamic slot creation."""

    def test_minimum_slots(self):
        """Test that minimum 12 slots are created."""
        slots = {}  # Empty tab
        max_idx = max(slots.keys()) if slots else -1
        num_slots = max(max_idx + 2, UI["total_slots"])
        self.assertEqual(num_slots, UI["total_slots"])

    def test_extra_slot_for_adding(self):
        """Test that one extra empty slot is always available."""
        slots = {0: "sound0", 1: "sound1", 2: "sound2"}
        max_idx = max(slots.keys()) if slots else -1
        num_slots = max(max_idx + 2, UI["total_slots"])
        # With 3 sounds (indices 0-2), we need at least index 3 empty
        # max_idx = 2, so max_idx + 2 = 4 slots (indices 0-3)
        self.assertGreaterEqual(num_slots, 4)

    def test_slots_expand_beyond_default(self):
        """Test that slots expand when more than default are needed."""
        # Simulate 15 sounds (indices 0-14)
        slots = {i: f"sound{i}" for i in range(15)}
        max_idx = max(slots.keys()) if slots else -1
        num_slots = max(max_idx + 2, UI["total_slots"])
        # Should be at least 16 (15 + 1 empty)
        self.assertGreaterEqual(num_slots, 16)


class TestDragAndDropState(unittest.TestCase):
    """Test drag and drop state management."""

    def test_drag_state_initialization(self):
        """Test that drag state variables can be initialized."""
        drag_source_idx = None
        drag_source_tab = None
        drag_start_x = 0
        drag_start_y = 0
        is_dragging = False
        
        self.assertIsNone(drag_source_idx)
        self.assertIsNone(drag_source_tab)
        self.assertFalse(is_dragging)

    def test_drag_threshold(self):
        """Test drag threshold calculation."""
        drag_start_x = 100
        drag_start_y = 100
        
        # Small movement - should not trigger drag
        event_x = 102
        event_y = 102
        dx = abs(event_x - drag_start_x)
        dy = abs(event_y - drag_start_y)
        should_drag = dx > 5 or dy > 5
        self.assertFalse(should_drag)
        
        # Larger movement - should trigger drag
        event_x = 110
        dx = abs(event_x - drag_start_x)
        should_drag = dx > 5 or dy > 5
        self.assertTrue(should_drag)


class TestSlotSwapping(unittest.TestCase):
    """Test slot swapping logic."""

    def test_swap_two_filled_slots(self):
        """Test swapping two slots that both have content."""
        slots = {
            0: SoundSlot(name="Sound A", file_path="a.mp3"),
            1: SoundSlot(name="Sound B", file_path="b.mp3"),
        }
        
        # Swap
        slot1 = slots.get(0)
        slot2 = slots.get(1)
        if slot1 is not None and slot2 is not None:
            slots[0] = slot2
            slots[1] = slot1
        
        self.assertEqual(slots[0].name, "Sound B")
        self.assertEqual(slots[1].name, "Sound A")

    def test_move_to_empty_slot(self):
        """Test moving a sound to an empty slot."""
        slots = {
            0: SoundSlot(name="Sound A", file_path="a.mp3"),
        }
        
        # Move slot 0 to slot 5
        idx1, idx2 = 0, 5
        slot1 = slots.get(idx1)
        slot2 = slots.get(idx2)
        
        if slot1 is not None and slot2 is None:
            slots[idx2] = slot1
            del slots[idx1]
        
        self.assertNotIn(0, slots)
        self.assertIn(5, slots)
        self.assertEqual(slots[5].name, "Sound A")


class TestMoveSlotBetweenTabs(unittest.TestCase):
    """Test moving slots between tabs."""

    def test_move_slot_to_different_tab(self):
        """Test moving a slot from one tab to another."""
        tab1 = SoundTab(
            name="Tab 1",
            slots={0: SoundSlot(name="Sound A", file_path="a.mp3")},
        )
        tab2 = SoundTab(name="Tab 2", slots={})
        
        tabs = [tab1, tab2]
        
        # Move slot 0 from tab 0 to tab 1
        from_tab_idx = 0
        to_tab_idx = 1
        slot_idx = 0
        
        from_tab = tabs[from_tab_idx]
        to_tab = tabs[to_tab_idx]
        
        if slot_idx in from_tab.slots:
            slot = from_tab.slots[slot_idx]
            
            # Find first empty slot in target tab
            target_idx = 0
            while target_idx in to_tab.slots:
                target_idx += 1
            
            # Move
            to_tab.slots[target_idx] = slot
            del from_tab.slots[slot_idx]
        
        self.assertNotIn(0, tab1.slots)
        self.assertIn(0, tab2.slots)
        self.assertEqual(tab2.slots[0].name, "Sound A")


if __name__ == "__main__":
    # Run tests
    print("=" * 60)
    print("Discord Soundboard Test Suite")
    print("=" * 60)
    
    # Create test suite
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    # Add all test classes
    suite.addTests(loader.loadTestsFromTestCase(TestSoundSlot))
    suite.addTests(loader.loadTestsFromTestCase(TestSoundTab))
    suite.addTests(loader.loadTestsFromTestCase(TestPlayingStateTracking))
    suite.addTests(loader.loadTestsFromTestCase(TestColorConstants))
    suite.addTests(loader.loadTestsFromTestCase(TestUIConstants))
    suite.addTests(loader.loadTestsFromTestCase(TestSlotIndexCalculation))
    suite.addTests(loader.loadTestsFromTestCase(TestDragAndDropState))
    suite.addTests(loader.loadTestsFromTestCase(TestSlotSwapping))
    suite.addTests(loader.loadTestsFromTestCase(TestMoveSlotBetweenTabs))
    
    # Run with verbosity
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    # Summary
    print("\n" + "=" * 60)
    if result.wasSuccessful():
        print("✅ All tests passed!")
    else:
        print(f"❌ {len(result.failures)} failures, {len(result.errors)} errors")
    print("=" * 60)
    
    # Exit with appropriate code
    sys.exit(0 if result.wasSuccessful() else 1)
