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

# Locate pyrnnoise DLL (must be bundled or RNNoise won't load)
try:
    import pyrnnoise as _pyrnn_pkg
    _pyrnn_dir = os.path.dirname(_pyrnn_pkg.__file__)
    pyrnnoise_dll = os.path.join(_pyrnn_dir, 'rnnoise.dll')
    if not os.path.exists(pyrnnoise_dll):
        pyrnnoise_dll = None
except Exception:
    pyrnnoise_dll = None

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

        # RNNoise for mic noise suppression
        'pyrnnoise',
        'pyrnnoise.rnnoise',

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
    excludes=[],
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
