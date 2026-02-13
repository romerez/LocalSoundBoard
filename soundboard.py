"""
Discord-style Soundboard with Mic Passthrough
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import sounddevice as sd
import soundfile as sf
import numpy as np
import threading
import queue
import json
import os
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict, List

try:
    import keyboard
    HOTKEYS_AVAILABLE = True
except ImportError:
    HOTKEYS_AVAILABLE = False

@dataclass
class SoundSlot:
    name: str
    file_path: str
    hotkey: Optional[str] = None
    volume: float = 1.0

class AudioMixer:
    def __init__(self, input_device: int, output_device: int, sample_rate: int = 48000, block_size: int = 1024):
        self.input_device = input_device
        self.output_device = output_device
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.channels = 2
        self.running = False
        self.stream = None
        self.sound_queue = queue.Queue()
        self.currently_playing = []
        self.lock = threading.Lock()
        self.mic_volume = 1.0
        self.mic_muted = False
        
    def start(self):
        if self.running:
            return
        self.running = True
        self.stream = sd.Stream(
            device=(self.input_device, self.output_device),
            samplerate=self.sample_rate,
            blocksize=self.block_size,
            channels=(1, self.channels),
            callback=self._audio_callback,
            dtype=np.float32
        )
        self.stream.start()
        
    def stop(self):
        self.running = False
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
            
    def _audio_callback(self, indata, outdata, frames, time, status):
        if self.mic_muted:
            mixed = np.zeros((frames, self.channels), dtype=np.float32)
        else:
            mic_mono = indata[:, 0] * self.mic_volume
            mixed = np.column_stack([mic_mono, mic_mono])
        
        while not self.sound_queue.empty():
            try:
                sound_data = self.sound_queue.get_nowait()
                with self.lock:
                    self.currently_playing.append(sound_data)
            except queue.Empty:
                break
        
        with self.lock:
            finished = []
            for i, sound in enumerate(self.currently_playing):
                pos = sound['position']
                data = sound['data']
                volume = sound['volume']
                remaining = len(data) - pos
                
                if remaining <= 0:
                    finished.append(i)
                    continue
                    
                chunk_size = min(frames, remaining)
                chunk = data[pos:pos + chunk_size] * volume
                
                if chunk.ndim == 1:
                    chunk = np.column_stack([chunk, chunk])
                elif chunk.shape[1] == 1:
                    chunk = np.column_stack([chunk[:, 0], chunk[:, 0]])
                
                if chunk_size < frames:
                    padded = np.zeros((frames, self.channels), dtype=np.float32)
                    padded[:chunk_size] = chunk
                    chunk = padded
                    
                mixed += chunk
                sound['position'] += chunk_size
            
            for i in reversed(finished):
                self.currently_playing.pop(i)
        
        np.clip(mixed, -1.0, 1.0, out=outdata)
        
    def play_sound(self, file_path: str, volume: float = 1.0):
        try:
            data, sr = sf.read(file_path, dtype=np.float32)
            if sr != self.sample_rate:
                ratio = self.sample_rate / sr
                new_length = int(len(data) * ratio)
                indices = np.linspace(0, len(data) - 1, new_length).astype(int)
                data = data[indices]
            self.sound_queue.put({'data': data, 'position': 0, 'volume': volume})
        except Exception as e:
            print(f"Error loading sound: {e}")
            
    def stop_all_sounds(self):
        with self.lock:
            self.currently_playing.clear()
        while not self.sound_queue.empty():
            try:
                self.sound_queue.get_nowait()
            except queue.Empty:
                break

class SoundboardApp:
    CONFIG_FILE = "soundboard_config.json"
    
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Discord Soundboard")
        self.root.geometry("800x600")
        self.root.configure(bg='#2C2F33')
        self.mixer = None
        self.sound_slots = {}
        self.slot_buttons = {}
        self.registered_hotkeys = []
        self._setup_styles()
        self._create_ui()
        self._load_config()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        
    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TFrame', background='#2C2F33')
        style.configure('TLabel', background='#2C2F33', foreground='#FFFFFF')
        style.configure('TButton', background='#7289DA', foreground='#FFFFFF')
        
    def _create_ui(self):
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        device_frame = ttk.LabelFrame(main_frame, text="Audio Devices", padding=10)
        device_frame.pack(fill=tk.X, pady=(0, 10))
        
        devices = sd.query_devices()
        input_devices = [(i, d['name']) for i, d in enumerate(devices) if d['max_input_channels'] > 0]
        output_devices = [(i, d['name']) for i, d in enumerate(devices) if d['max_output_channels'] > 0]
        
        ttk.Label(device_frame, text="Microphone (Input):").grid(row=0, column=0, sticky='w', padx=5)
        self.input_var = tk.StringVar()
        self.input_combo = ttk.Combobox(device_frame, textvariable=self.input_var, width=50, state='readonly')
        self.input_combo['values'] = [f"{i}: {name}" for i, name in input_devices]
        if input_devices:
            self.input_combo.current(0)
        self.input_combo.grid(row=0, column=1, padx=5, pady=2)
        
        ttk.Label(device_frame, text="Virtual Cable (Output):").grid(row=1, column=0, sticky='w', padx=5)
        self.output_var = tk.StringVar()
        self.output_combo = ttk.Combobox(device_frame, textvariable=self.output_var, width=50, state='readonly')
        self.output_combo['values'] = [f"{i}: {name}" for i, name in output_devices]
        for idx, (i, name) in enumerate(output_devices):
            if 'cable' in name.lower() or 'virtual' in name.lower():
                self.output_combo.current(idx)
                break
        else:
            if output_devices:
                self.output_combo.current(0)
        self.output_combo.grid(row=1, column=1, padx=5, pady=2)
        
        self.toggle_btn = tk.Button(device_frame, text="▶ Start", command=self._toggle_stream,
            bg='#43B581', fg='white', font=('Segoe UI', 10, 'bold'), width=15)
        self.toggle_btn.grid(row=0, column=2, rowspan=2, padx=20)
        
        mic_frame = ttk.LabelFrame(main_frame, text="Microphone", padding=10)
        mic_frame.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Label(mic_frame, text="Mic Volume:").pack(side=tk.LEFT, padx=5)
        self.mic_volume_var = tk.DoubleVar(value=100)
        ttk.Scale(mic_frame, from_=0, to=150, variable=self.mic_volume_var,
                  command=self._update_mic_volume, length=200).pack(side=tk.LEFT, padx=5)
        
        self.mic_mute_var = tk.BooleanVar(value=False)
        tk.Checkbutton(mic_frame, text="Mute Mic", variable=self.mic_mute_var,
                       command=self._toggle_mic_mute, bg='#2C2F33', fg='white',
                       selectcolor='#F04747').pack(side=tk.LEFT, padx=20)
        
        tk.Button(mic_frame, text="Stop All Sounds", command=self._stop_all_sounds,
                  bg='#F04747', fg='white').pack(side=tk.RIGHT, padx=5)
        
        board_frame = ttk.LabelFrame(main_frame, text="Soundboard (Right-click to configure)", padding=10)
        board_frame.pack(fill=tk.BOTH, expand=True)
        
        self.grid_frame = ttk.Frame(board_frame)
        self.grid_frame.pack(fill=tk.BOTH, expand=True)
        
        for i in range(12):
            row, col = divmod(i, 4)
            btn = tk.Button(self.grid_frame, text=f"Slot {i+1}\n(Empty)",
                width=18, height=4, bg='#40444B', fg='#8E9297',
                font=('Segoe UI', 9), relief=tk.FLAT,
                command=lambda idx=i: self._play_slot(idx))
            btn.grid(row=row, column=col, padx=5, pady=5, sticky='nsew')
            btn.bind('<Button-3>', lambda e, idx=i: self._configure_slot(idx))
            self.slot_buttons[i] = btn
            
        for i in range(4):
            self.grid_frame.columnconfigure(i, weight=1)
        for i in range(3):
            self.grid_frame.rowconfigure(i, weight=1)
            
        self.status_var = tk.StringVar(value="Ready - Select devices and click Start")
        ttk.Label(main_frame, textvariable=self.status_var, relief=tk.SUNKEN, anchor='w').pack(fill=tk.X, pady=(10, 0))
        
    def _toggle_stream(self):
        if self.mixer and self.mixer.running:
            self.mixer.stop()
            self.toggle_btn.configure(text="▶ Start", bg='#43B581')
            self.status_var.set("Stopped")
        else:
            try:
                input_idx = int(self.input_var.get().split(':')[0])
                output_idx = int(self.output_var.get().split(':')[0])
                self.mixer = AudioMixer(input_idx, output_idx)
                self.mixer.start()
                self.toggle_btn.configure(text="⏹ Stop", bg='#F04747')
                self.status_var.set("Running - Mic → Virtual Cable")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to start:\n{e}")
                
    def _update_mic_volume(self, _=None):
        if self.mixer:
            self.mixer.mic_volume = self.mic_volume_var.get() / 100.0
            
    def _toggle_mic_mute(self):
        if self.mixer:
            self.mixer.mic_muted = self.mic_mute_var.get()
            
    def _stop_all_sounds(self):
        if self.mixer:
            self.mixer.stop_all_sounds()
            
    def _play_slot(self, slot_idx):
        if slot_idx not in self.sound_slots:
            return
        slot = self.sound_slots[slot_idx]
        if self.mixer and self.mixer.running:
            self.mixer.play_sound(slot.file_path, slot.volume)
            self.status_var.set(f"Playing: {slot.name}")
        else:
            self.status_var.set("Start the audio stream first!")
            
    def _configure_slot(self, slot_idx):
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Configure Slot {slot_idx + 1}")
        dialog.geometry("400x250")
        dialog.configure(bg='#2C2F33')
        dialog.transient(self.root)
        dialog.grab_set()
        
        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)
        
        existing = self.sound_slots.get(slot_idx)
        
        ttk.Label(frame, text="Name:").grid(row=0, column=0, sticky='w', pady=5)
        name_var = tk.StringVar(value=existing.name if existing else "")
        ttk.Entry(frame, textvariable=name_var, width=35).grid(row=0, column=1, pady=5)
        
        ttk.Label(frame, text="Sound File:").grid(row=1, column=0, sticky='w', pady=5)
        path_var = tk.StringVar(value=existing.file_path if existing else "")
        ttk.Entry(frame, textvariable=path_var, width=35).grid(row=1, column=1, pady=5)
        
        def browse():
            fp = filedialog.askopenfilename(filetypes=[("Audio", "*.mp3 *.wav *.ogg *.flac")])
            if fp:
                path_var.set(fp)
                if not name_var.get():
                    name_var.set(Path(fp).stem)
        ttk.Button(frame, text="Browse", command=browse).grid(row=1, column=2, padx=5)
        
        ttk.Label(frame, text="Volume:").grid(row=2, column=0, sticky='w', pady=5)
        volume_var = tk.DoubleVar(value=(existing.volume * 100) if existing else 100)
        ttk.Scale(frame, from_=0, to=150, variable=volume_var, length=200).grid(row=2, column=1, sticky='w')
        
        ttk.Label(frame, text="Hotkey:").grid(row=3, column=0, sticky='w', pady=5)
        hotkey_var = tk.StringVar(value=existing.hotkey if existing and existing.hotkey else "")
        ttk.Entry(frame, textvariable=hotkey_var, width=20).grid(row=3, column=1, sticky='w')
        
        def save():
            if not path_var.get():
                dialog.destroy()
                return
            self.sound_slots[slot_idx] = SoundSlot(
                name=name_var.get() or Path(path_var.get()).stem,
                file_path=path_var.get(),
                hotkey=hotkey_var.get() or None,
                volume=volume_var.get() / 100.0
            )
            self._update_slot_button(slot_idx)
            self._register_hotkeys()
            self._save_config()
            dialog.destroy()
            
        def clear():
            if slot_idx in self.sound_slots:
                del self.sound_slots[slot_idx]
            self._update_slot_button(slot_idx)
            self._save_config()
            dialog.destroy()
            
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=4, column=0, columnspan=3, pady=20)
        tk.Button(btn_frame, text="Save", command=save, bg='#43B581', fg='white', width=10).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Clear", command=clear, bg='#F04747', fg='white', width=10).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Cancel", command=dialog.destroy, bg='#40444B', fg='white', width=10).pack(side=tk.LEFT, padx=5)
        
    def _update_slot_button(self, slot_idx):
        btn = self.slot_buttons[slot_idx]
        if slot_idx in self.sound_slots:
            slot = self.sound_slots[slot_idx]
            hk = f"\n[{slot.hotkey}]" if slot.hotkey else ""
            btn.configure(text=f"{slot.name}{hk}", bg='#7289DA', fg='white')
        else:
            btn.configure(text=f"Slot {slot_idx + 1}\n(Empty)", bg='#40444B', fg='#8E9297')
            
    def _register_hotkeys(self):
        if not HOTKEYS_AVAILABLE:
            return
        for hk in self.registered_hotkeys:
            try:
                keyboard.remove_hotkey(hk)
            except:
                pass
        self.registered_hotkeys.clear()
        for slot_idx, slot in self.sound_slots.items():
            if slot.hotkey:
                try:
                    keyboard.add_hotkey(slot.hotkey, lambda idx=slot_idx: self._play_slot(idx))
                    self.registered_hotkeys.append(slot.hotkey)
                except:
                    pass
                    
    def _save_config(self):
        config = {'slots': {str(i): {'name': s.name, 'file_path': s.file_path, 'hotkey': s.hotkey, 'volume': s.volume}
                           for i, s in self.sound_slots.items()}}
        with open(self.CONFIG_FILE, 'w') as f:
            json.dump(config, f)
            
    def _load_config(self):
        if not os.path.exists(self.CONFIG_FILE):
            return
        try:
            with open(self.CONFIG_FILE) as f:
                config = json.load(f)
            for idx, data in config.get('slots', {}).items():
                self.sound_slots[int(idx)] = SoundSlot(
                    name=data['name'], file_path=data['file_path'],
                    hotkey=data.get('hotkey'), volume=data.get('volume', 1.0))
                self._update_slot_button(int(idx))
            self._register_hotkeys()
        except:
            pass
            
    def _on_close(self):
        if self.mixer:
            self.mixer.stop()
        self.root.destroy()
        
    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    SoundboardApp().run()