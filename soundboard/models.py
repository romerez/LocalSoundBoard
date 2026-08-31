"""
Data models for the Discord Soundboard.

UNKNOWN-KEY ROUND-TRIP (added 2026-08-08, after a real data-loss): every
model keeps fields it doesn't know in `extra` and writes them back in
to_dict(). Before this, running an OLDER build silently DROPPED any field a
newer build (or the phone app) had added — that is exactly how every tab's
`section` assignment got wiped when the old EXE saved the config. The
mobile contract (mobile/README.md §5.1, copilot-instructions § Mobile
Companion App) requires this tolerance — never remove it.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _split_extra(data: Dict[str, Any], known: set) -> Dict[str, Any]:
    """Everything the model doesn't understand, preserved verbatim."""
    return {k: v for k, v in data.items() if k not in known}


@dataclass
class SoundSlot:
    """Represents a single sound button configuration."""

    name: str
    file_path: str
    hotkey: Optional[str] = None
    volume: float = 1.0
    emoji: Optional[str] = None  # Emoji character to display
    image_path: Optional[str] = None  # Path to custom image/gif
    color: Optional[str] = None  # Custom background color (hex)
    speed: float = 1.0  # Playback speed (0.5 to 2.0)
    preserve_pitch: bool = (
        True  # If True, use time-stretch; if False, simple resample (chipmunk effect)
    )
    loop: bool = False  # If True, sound loops until stopped
    loop_count: int = 0  # Number of times to loop (0 = infinite)
    loop_delay: float = 0.0  # Delay between loops in seconds
    groups: List[str] = field(default_factory=list)  # Sound groups/types for filtering
    # Path to the ORIGINAL un-trimmed source audio. When the slot's audio was
    # produced by trimming in the editor, file_path points at the trimmed
    # output and source_file_path points at the original full-length file.
    # This lets the user "Clone (re-trim)" the slot and pick a different cut
    # from the same original source. None means file_path is itself the source.
    source_file_path: Optional[str] = None
    # Fields from newer builds / the phone app — round-tripped untouched.
    extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # "group" excluded: legacy field consumed by _migrate_groups, must not
    # resurface on save.
    _KNOWN = {
        "name", "file_path", "hotkey", "volume", "emoji", "image_path",
        "color", "speed", "preserve_pitch", "loop", "loop_count",
        "loop_delay", "groups", "source_file_path", "group",
    }

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        out = dict(self.extra)
        out.update({
            "name": self.name,
            "file_path": self.file_path,
            "hotkey": self.hotkey,
            "volume": self.volume,
            "emoji": self.emoji,
            "image_path": self.image_path,
            "color": self.color,
            "speed": self.speed,
            "preserve_pitch": self.preserve_pitch,
            "loop": self.loop,
            "loop_count": self.loop_count,
            "loop_delay": self.loop_delay,
            "groups": self.groups,
            "source_file_path": self.source_file_path,
        })
        return out

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
            color=data.get("color"),
            speed=data.get("speed", 1.0),
            preserve_pitch=data.get("preserve_pitch", True),
            loop=data.get("loop", False),
            loop_count=data.get("loop_count", 0),
            loop_delay=data.get("loop_delay", 0.0),
            groups=_migrate_groups(data),
            source_file_path=data.get("source_file_path"),
            extra=_split_extra(data, cls._KNOWN),
        )


def _migrate_groups(data: Dict[str, Any]) -> List[str]:
    """Migrate old single 'group' field to new 'groups' list."""
    # New format: list of groups
    if "groups" in data:
        val = data["groups"]
        if isinstance(val, list):
            return val
        if isinstance(val, str) and val:
            return [val]
        return []
    # Old format: single group string
    old = data.get("group")
    if old and isinstance(old, str):
        return [old]
    return []


@dataclass
class PersonGroup:
    """A named group of sounds inside a Person's mini-soundboard (e.g. "Hi",
    "Bye", "lol"). Each sound is a full SoundSlot so it reuses the same
    playback options + serialization as the main board."""

    name: str
    # Stable shared-group identity. Groups are shared across ALL people (every
    # person has the same set of groups; only the SOUNDS inside differ), so a
    # group's name/icon/colour and create/edit/delete/reorder are matched across
    # people by this id (survives renames). Assigned during consolidation.
    id: Optional[str] = None
    color: Optional[str] = None  # Optional accent for the group header / chips
    emoji: Optional[str] = None  # Optional icon glyph for the group header
    collapsed: bool = False      # Collapsed (header only) to save space
    sounds: List[SoundSlot] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    _KNOWN = {"id", "name", "color", "emoji", "collapsed", "sounds"}

    def to_dict(self) -> Dict[str, Any]:
        out = dict(self.extra)
        out.update({
            "id": self.id,
            "name": self.name,
            "color": self.color,
            "emoji": self.emoji,
            "collapsed": self.collapsed,
            "sounds": [s.to_dict() for s in self.sounds],
        })
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PersonGroup":
        return cls(
            name=data.get("name", "Group"),
            id=data.get("id"),
            color=data.get("color"),
            emoji=data.get("emoji"),
            collapsed=bool(data.get("collapsed", False)),
            sounds=[SoundSlot.from_dict(s) for s in data.get("sounds", [])],
            extra=_split_extra(data, cls._KNOWN),
        )


@dataclass
class Person:
    """A person you keep a dedicated mini-soundboard for. Owns their own groups
    of sounds, independent of the main tabs (sounds may be added from files or
    copied in from existing main-board slots)."""

    name: str
    color: Optional[str] = None  # Accent colour for the person's header/avatar
    emoji: Optional[str] = None  # Optional avatar glyph
    image_path: Optional[str] = None  # Optional avatar picture (shown as a circle)
    groups: List[PersonGroup] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    _KNOWN = {"name", "color", "emoji", "image_path", "groups"}

    def to_dict(self) -> Dict[str, Any]:
        out = dict(self.extra)
        out.update({
            "name": self.name,
            "color": self.color,
            "emoji": self.emoji,
            "image_path": self.image_path,
            "groups": [g.to_dict() for g in self.groups],
        })
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Person":
        return cls(
            name=data.get("name", "Person"),
            color=data.get("color"),
            emoji=data.get("emoji"),
            image_path=data.get("image_path"),
            groups=[PersonGroup.from_dict(g) for g in data.get("groups", [])],
            extra=_split_extra(data, cls._KNOWN),
        )


@dataclass
class SoundTab:
    """Represents a tab containing sound slots."""

    name: str
    emoji: Optional[str] = None
    slots: Dict[int, SoundSlot] = field(default_factory=dict)
    color: Optional[str] = None  # Custom tab accent color (hex), None = default
    # Display section: "pc" (default) or "phone". The PC is the manager — tabs
    # assigned to "phone" are what the mobile companion shows by default (it
    # can still switch to see the PC section). Orthogonal to the phone-side
    # "origin" sync-ownership key. Contract: mobile/README.md §5.1/§5.4.
    section: str = "pc"
    extra: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    _KNOWN = {"name", "emoji", "color", "section", "slots"}

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        out = dict(self.extra)
        out.update({
            "name": self.name,
            "emoji": self.emoji,
            "color": self.color,
            "section": self.section,
            "slots": {str(i): s.to_dict() for i, s in self.slots.items()},
        })
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SoundTab":
        """Create a SoundTab from a dictionary."""
        slots = {}
        for idx, slot_data in data.get("slots", {}).items():
            slots[int(idx)] = SoundSlot.from_dict(slot_data)
        return cls(
            name=data["name"],
            emoji=data.get("emoji"),
            color=data.get("color"),
            section=data.get("section") or "pc",
            slots=slots,
            extra=_split_extra(data, cls._KNOWN),
        )
