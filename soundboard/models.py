"""
Data models for the Discord Soundboard.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class SoundSlot:
    """Represents a single sound button configuration."""

    name: str
    file_path: str
    hotkey: Optional[str] = None
    volume: float = 1.0

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "file_path": self.file_path,
            "hotkey": self.hotkey,
            "volume": self.volume,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SoundSlot":
        """Create a SoundSlot from a dictionary."""
        return cls(
            name=data["name"],
            file_path=data["file_path"],
            hotkey=data.get("hotkey"),
            volume=data.get("volume", 1.0),
        )
