"""
Data models for the Discord Soundboard.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SoundSlot:
    """Represents a single sound button configuration."""

    name: str
    file_path: str
    hotkey: Optional[str] = None
    volume: float = 1.0
    emoji: Optional[str] = None  # Emoji character to display
    image_path: Optional[str] = None  # Path to custom image/gif

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "file_path": self.file_path,
            "hotkey": self.hotkey,
            "volume": self.volume,
            "emoji": self.emoji,
            "image_path": self.image_path,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SoundSlot":
        """Create a SoundSlot from a dictionary."""
        return cls(
            name=data["name"],
            file_path=data["file_path"],
            hotkey=data.get("hotkey"),
            volume=data.get("volume", 1.0),
            emoji=data.get("emoji"),
            image_path=data.get("image_path"),
        )


@dataclass
class SoundTab:
    """Represents a tab containing sound slots."""

    name: str
    emoji: Optional[str] = None
    slots: Dict[int, SoundSlot] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "emoji": self.emoji,
            "slots": {str(i): s.to_dict() for i, s in self.slots.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SoundTab":
        """Create a SoundTab from a dictionary."""
        slots = {}
        for idx, slot_data in data.get("slots", {}).items():
            slots[int(idx)] = SoundSlot.from_dict(slot_data)
        return cls(
            name=data["name"],
            emoji=data.get("emoji"),
            slots=slots,
        )
