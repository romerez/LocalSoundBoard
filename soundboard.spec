# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Discord Soundboard.

Build with:
    pyinstaller soundboard.spec
"""

import os
import sys
import importlib
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files

block_cipher = None

# onnxruntime (DeepFilterNet noise-suppression backend) ships native DLLs —
# onnxruntime.dll AND onnxruntime_providers_shared.dll — that PyInstaller will
# miss without collecting them explicitly (the classic "DLL load failed
# importing onnxruntime_pybind11_state" error). Collect both libs and data.
try:
    ort_binaries = collect_dynamic_libs('onnxruntime')
    ort_datas = collect_data_files('onnxruntime')
    print(f"[spec] onnxruntime: {len(ort_binaries)} libs, {len(ort_datas)} data files")
except Exception as _e:
    print(f"[spec] WARNING: onnxruntime collect failed ({_e}); DeepFilterNet NS will be unavailable in the EXE")
    ort_binaries, ort_datas = [], []

# DeepFilterNet ONNX model (raw-in/raw-out, 48 kHz). ~16 MB.
dfn_model = os.path.join('soundboard', 'models', 'denoiser_model.onnx')
dfn_model_datas = [(dfn_model, 'soundboard/models')] if os.path.exists(dfn_model) else []
if not dfn_model_datas:
    print("[spec] WARNING: denoiser_model.onnx not found; DeepFilterNet NS will be unavailable in the EXE")

# Helper to get package directory
def get_pkg_dir(pkg_name):
    mod = importlib.import_module(pkg_name)
    return os.path.dirname(mod.__file__)

# Paths to package data that must be bundled
ctk_path = get_pkg_dir('customtkinter')
sd_data_path = get_pkg_dir('_sounddevice_data')
sf_data_path = get_pkg_dir('_soundfile_data')
emoji_data_path = get_pkg_dir('emoji_data_python')

# ffmpeg binary (imageio-ffmpeg ships ffmpeg only, no ffprobe)
import imageio_ffmpeg
ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
ffmpeg_dir = os.path.dirname(ffmpeg_exe)

# static-ffmpeg ships BOTH ffmpeg and ffprobe (required for yt-dlp MP3 postprocess).
# Pre-fetch at build time so the EXE doesn't need to download on first run.
try:
    from static_ffmpeg import run as _sff_run
    _sff_ffmpeg, _sff_ffprobe = _sff_run.get_or_fetch_platform_executables_else_raise()
    static_ffmpeg_dir = os.path.dirname(_sff_ffmpeg)
    print(f"[spec] Bundling static-ffmpeg from: {static_ffmpeg_dir}")
except Exception as _e:
    print(f"[spec] WARNING: static-ffmpeg fetch failed ({_e}); ffprobe will be missing in EXE")
    static_ffmpeg_dir = None

# Locate pyrnnoise's rnnoise.dll WITHOUT importing the package: its __init__
# drags in audiolab -> av (libav), a chain the frozen EXE never satisfied, which
# is why "Light (RNNoise)" silently did nothing in prod. audio.py now binds the
# DLL directly via ctypes, so the EXE needs ONLY this file (bundled below as
# pyrnnoise/rnnoise.dll) and pyrnnoise/audiolab/av are excluded (-65 MB).
try:
    import importlib.util as _ilu
    _pyrnn_spec = _ilu.find_spec('pyrnnoise')
    _pyrnn_dir = os.path.dirname(_pyrnn_spec.origin) if (_pyrnn_spec and _pyrnn_spec.origin) else None
    pyrnnoise_dll = os.path.join(_pyrnn_dir, 'rnnoise.dll') if _pyrnn_dir else None
    if not (pyrnnoise_dll and os.path.exists(pyrnnoise_dll)):
        pyrnnoise_dll = None
except Exception:
    pyrnnoise_dll = None
if pyrnnoise_dll:
    print(f"[spec] rnnoise.dll: {pyrnnoise_dll}")
else:
    print("[spec] WARNING: rnnoise.dll not found; the Light (RNNoise) engine will be unavailable in the EXE")

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=ort_binaries,
    datas=[
        # CustomTkinter themes/assets
        (ctk_path, 'customtkinter'),
        # PortAudio DLL for sounddevice
        (sd_data_path, '_sounddevice_data'),
        # libsndfile DLL for soundfile
        (sf_data_path, '_soundfile_data'),
        # emoji data JSON files
        (emoji_data_path, 'emoji_data_python'),
        # ffmpeg binary for pydub
        (ffmpeg_dir, 'imageio_ffmpeg/binaries'),
        # emoji_picker.py needs to be accessible as a script for subprocess
        ('soundboard/emoji_picker.py', 'soundboard'),
    ] + ([(static_ffmpeg_dir, 'ffmpeg_bin')] if static_ffmpeg_dir else [])
      + ([(pyrnnoise_dll, 'pyrnnoise')] if pyrnnoise_dll else [])
      + dfn_model_datas
      + ort_datas,
    hiddenimports=[
        # Own package: soundboard/__init__.py is PEP-562 LAZY (no static
        # imports), so PyInstaller no longer discovers these through it —
        # without this list the frozen EXE dies on "from soundboard import
        # SoundboardApp".
        'soundboard.gui',
        'soundboard.audio',
        'soundboard.editor',
        'soundboard.models',
        'soundboard.mobile_sync',
        'soundboard.perf_probe',

        # Core audio
        'sounddevice',
        '_sounddevice_data',
        'soundfile',
        '_soundfile_data',
        'numpy',

        # Input handling
        'keyboard',
        'mouse',
        'pynput',
        'pynput.keyboard._win32',
        'pynput.mouse._win32',
        'windnd',

        # Audio format support
        'pydub',
        'pydub.utils',
        'pydub.audio_segment',
        'imageio_ffmpeg',
        'imageio_ffmpeg.binaries',
        'static_ffmpeg',
        'static_ffmpeg.run',
        'yt_dlp',

        # 📱 Send to Phone (mobile companion sync — lazy-imported in mobile_sync.py)
        'qrcode',
        'qrcode.image.pil',

        # librosa and its dependencies
        'librosa',
        'scipy',
        'scipy.signal',
        'scipy.fft',
        'scipy.fft._pocketfft',
        'sklearn',
        'sklearn.utils._cython_blas',
        'sklearn.neighbors.typedefs',
        'sklearn.neighbors._partition_nodes',
        'sklearn.utils._typedefs',
        'joblib',
        'numba',
        'llvmlite',

        # GUI
        'customtkinter',
        'PIL',
        'PIL._tkinter_finder',
        'PIL.Image',
        'PIL.ImageTk',

        # Data
        'emoji_data_python',
        'colour',

        # RNNoise: NOT imported as a package any more (audio.py binds
        # pyrnnoise/rnnoise.dll via ctypes; the DLL ships through `datas`).

        # onnxruntime — DeepFilterNet noise-suppression backend
        'onnxruntime',
        'onnxruntime.capi',
        'onnxruntime.capi._pybind_state',
        'onnxruntime.capi.onnxruntime_inference_collection',

        # PyQt6 for emoji picker subprocess
        'PyQt6',
        'PyQt6.QtWidgets',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.sip',

        # Standard library that may be missed
        'ctypes',
        'queue',
        'dataclasses',
        'json',
        'pathlib',
        'io',
        'shutil',
        'threading',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # pyrnnoise's Python side needs audiolab + av (libav, ~65 MB) that only its
    # file-conversion CLI uses; nothing in the app imports them.
    excludes=['pyrnnoise', 'audiolab', 'av'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SoundBoard',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # No console window - GUI app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SoundBoard',
)
