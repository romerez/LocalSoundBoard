"""Headless regression tests for the People window (soundboard/person_board.py).

Run with:  .venv\\Scripts\\python.exe test_people_ui.py

Opens real (withdrawn) CustomTkinter windows — needs a display, like the app
itself — but never shows anything and never calls root.update(): every step
is an after()-scheduled callback under mainloop (the CTk-safe pattern).

Guards the 2026-08-31 fixes:
  * a hub built WITHDRAWN (the startup prebuild) lays its panel out against
    the requested window width, not Tk's 200-px placeholder (the bug behind
    "1 column then a full rebuild on every first open");
  * reopen() completes: window mapped, alpha restored, reveal flag cleared;
  * refresh_people() is a no-op when nothing changed (same row widgets);
  * sidebar names are native text (no bitmap), each row has a colour pill and
    a ⋮ button that actually gets room;
  * emoji/avatar/chip-label rasters are built at DEVICE pixels for the
    window's scaling and wrapped so CTk does not resample them.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import customtkinter as ctk  # noqa: E402

from soundboard import person_board as PB  # noqa: E402
from soundboard.models import Person, PersonGroup, SoundSlot  # noqa: E402

REQ_W = 900
RESULTS = {}


def _persons():
    def slot(n):
        return SoundSlot(name=n, file_path=f"sounds\\{n}.wav")

    def groups():
        return [
            PersonGroup(name="Hi", emoji="👋", sounds=[slot("hello there"), slot("שלום עולם")]),
            PersonGroup(name="Bye", emoji=None, sounds=[slot("goodbye")]),
            PersonGroup(name="Empty", emoji="📦", sounds=[]),
        ]

    return [
        Person(name="Alpha", color="#053ece", emoji="😀", groups=groups()),
        Person(name="בטא", color="#14ab77", emoji=None, groups=groups()),
        Person(name="Gamma", color=None, emoji=None, groups=groups()),
    ]


def _run_tk_scenario():
    """One Tk session, after()-stepped; fills RESULTS."""
    from soundboard import ctk_patches
    ctk_patches.install()  # exactly what gui.py does at import (idempotent)
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    root.withdraw()
    persons = _persons()
    geo = {}
    ctx = PB.PersonContext(
        root=root, persons=persons,
        play=lambda s: 0.5, is_running=lambda: False, get_main_sounds=lambda: [],
        persist=lambda: None,
        get_geometry=lambda k: geo.get(k), set_geometry=lambda k, v: geo.__setitem__(k, v),
    )

    def step1():
        try:
            hub = PB.PersonHub(root, ctx, on_popout=lambda p: None)
            hub.withdraw()  # exactly what gui._prebuild_person_hub does
            RESULTS["hub"] = hub
            root.after(1200, step2)  # let the panel pump stream
        except Exception as e:  # pragma: no cover
            RESULTS["error"] = repr(e)
            root.quit()

    def step2():
        hub = RESULTS["hub"]
        try:
            panel = hub._panel
            RESULTS["state_before"] = hub.wm_state()
            RESULTS["win_width_premap"] = panel._win_width()
            RESULTS["cols_match"] = (panel._last_cols == panel._group_cols() == panel._cols)
            RESULTS["chip_avail"] = getattr(panel, "_chip_avail_base", 0)
            RESULTS["scaling"] = getattr(panel, "_scaling", None)
            rows = hub._row_widgets
            RESULTS["row_ids"] = [id(rw["row"]) for rw in rows.values()]
            first = next(iter(rows.values()))
            RESULTS["name_is_text"] = bool(first["name_btn"].cget("text")) and first["name_btn"].cget("image") is None
            kids = [type(k).__name__ for k in first["row"].winfo_children()]
            RESULTS["row_kids"] = kids
            RESULTS["name_min_width"] = first["name_btn"].cget("width")
            # Rows record the state they were built in, so the post-rebuild
            # reconcile in refresh_people() does zero configure()/_draw() work.
            RESULTS["rows_have_sel_flag"] = all(hasattr(rw["row"], "_lsb_sel") for rw in rows.values())
            hub.refresh_people()  # nothing changed -> must keep the same widgets
            RESULTS["row_ids_after"] = [id(rw["row"]) for rw in hub._row_widgets.values()]
            # Measure the reveal itself: wait for the panel's build pump to drain
            # (a real user opens the hub long after the startup prebuild).
            RESULTS["_drain_t0"] = time.perf_counter()
            root.after(50, wait_drained)
        except Exception as e:  # pragma: no cover
            RESULTS["error"] = repr(e)
            root.quit()

    def wait_drained():
        hub = RESULTS["hub"]
        panel = hub._panel
        busy = bool(panel._build_jobs or panel._build_pump_after)
        if busy and time.perf_counter() - RESULTS["_drain_t0"] < 8.0:
            root.after(50, wait_drained)
            return
        RESULTS["pump_drained"] = not busy
        hub.reopen()
        root.after(1500, step3)

    def step3():
        hub = RESULTS["hub"]
        try:
            RESULTS["state_after"] = hub.wm_state()
            RESULTS["alpha_after"] = float(hub.attributes("-alpha"))
            RESULTS["reveal_pending"] = getattr(hub, "_lsb_reveal_pending", None)
            RESULTS["reveal_via"] = str(getattr(hub, "_lsb_reveal_via", None))
            RESULTS["reveal_ms"] = getattr(hub, "_lsb_reveal_ms", None)
            RESULTS["viewable"] = bool(hub.winfo_viewable())
            RESULTS["win_width_mapped"] = hub._panel._win_width()
            # 👥 People clicked while the hub is already open: must NOT go
            # through the alpha-0 reveal (a mapped window gets no <Map>, so it
            # would stay invisible until the 700 ms safety timer).
            hub.reopen()
            RESULTS["revis_alpha"] = float(hub.attributes("-alpha"))
            RESULTS["revis_pending"] = bool(getattr(hub, "_lsb_reveal_pending", False))
            RESULTS["revis_state"] = hub.wm_state()
            hub.withdraw()
            root.after(50, step4)
        except Exception as e:  # pragma: no cover
            RESULTS["error"] = repr(e)
            root.quit()

    def step4():
        """Zero-people hub → add the first person → select must not raise
        (the hint used to be pack()ed where the panels are grid()ed), plus the
        slot thumbnail poll must outlive the widget that armed it."""
        try:
            persons2 = []
            ctx2 = PB.PersonContext(
                root=root, persons=persons2,
                play=lambda s: 0.5, is_running=lambda: False, get_main_sounds=lambda: [],
                persist=lambda: None,
                get_geometry=lambda k: geo.get(k), set_geometry=lambda k, v: geo.__setitem__(k, v),
            )
            hub2 = PB.PersonHub(root, ctx2, on_popout=lambda p: None)
            hub2.withdraw()
            RESULTS["hub2"] = hub2
            RESULTS["empty_hint_shown"] = hub2._empty_hint is not None and hub2._panel is None
            RESULTS["empty_hint_manager"] = hub2._empty_hint.winfo_manager()
            persons2.append(Person(name="Newbie", color="#5865F2", emoji="🙂", groups=[]))
            hub2.refresh_people()
            hub2.select(persons2[0])  # raised TclError (grid inside pack master) before the fix
            RESULTS["empty_select_ok"] = hub2._panel is not None and hub2._empty_hint is None

            import tkinter as tk
            from soundboard import slot_widget as SW
            c = tk.Canvas(root)
            SW._schedule_poll(c)
            RESULTS["poll_armed"] = bool(SW._poll_scheduled)
            RESULTS["poll_host_is_root"] = SW._poll_host is root
            c.destroy()  # used to take the pending after() with it → poll wedged forever

            # A CTkToplevel constructor must NOT pump the full event loop
            # (CustomTkinter's title-bar recolour called update(); that pump
            # ran seconds of unrelated backlog inside every dialog open) and
            # the window must be repainted once shown after that cycle.
            calls = {"n": 0}
            orig_update = tk.Misc.update

            def _counting(self_):
                calls["n"] += 1
                return orig_update(self_)

            tk.Misc.update = _counting
            try:
                probe = ctk.CTkToplevel(root)
                ctk.CTkLabel(probe, text="probe").pack()
            finally:
                tk.Misc.update = orig_update
            RESULTS["toplevel_update_calls"] = calls["n"]
            RESULTS["probe"] = probe
            root.after(500, step5)
        except Exception as e:  # pragma: no cover
            RESULTS["error"] = repr(e)
            root.quit()

    def step5():
        from soundboard import slot_widget as SW
        probe = RESULTS.get("probe")
        try:
            RESULTS["probe_state"] = probe.wm_state()
            RESULTS["probe_redrawn"] = getattr(probe, "_lsb_rdw_done", 0)
            probe.destroy()
        except Exception as e:  # pragma: no cover
            RESULTS["probe_state"] = repr(e)
            RESULTS["probe_redrawn"] = 0
        RESULTS["poll_ran_after_destroy"] = not SW._poll_scheduled
        try:
            RESULTS["hub2"].shutdown()
        except Exception:
            pass
        root.after(50, root.quit)

    root.after(100, step1)
    root.mainloop()
    try:
        RESULTS["hub"].shutdown()
    except Exception:
        pass
    try:
        root.destroy()
    except Exception:
        pass


class PeopleWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _run_tk_scenario()
        if "error" in RESULTS:
            raise RuntimeError(RESULTS["error"])

    def test_prebuilt_panel_uses_requested_width(self):
        self.assertEqual(RESULTS["state_before"], "withdrawn")
        self.assertEqual(RESULTS["win_width_premap"], REQ_W)
        self.assertTrue(RESULTS["cols_match"], "column count must not drift between prebuild and map")
        self.assertGreater(RESULTS["chip_avail"], 36)

    def test_reopen_reveals_window(self):
        self.assertEqual(RESULTS["state_after"], "normal")
        self.assertEqual(RESULTS["alpha_after"], 1.0)
        self.assertFalse(RESULTS["reveal_pending"])
        self.assertTrue(RESULTS["viewable"])
        self.assertEqual(RESULTS["win_width_mapped"], REQ_W)
        # The window must be revealed by the real map/expose event, not the
        # 700 ms safety timer, and quickly.
        self.assertTrue(RESULTS.get("pump_drained"), "panel pump never drained")
        self.assertNotEqual(RESULTS["reveal_via"], "timer", f"revealed via {RESULTS['reveal_via']}")
        self.assertIsNotNone(RESULTS["reveal_ms"])
        print(f"\n[reveal] via={RESULTS['reveal_via']} in {RESULTS['reveal_ms']:.0f} ms")
        self.assertLess(RESULTS["reveal_ms"], 500, f"reveal took {RESULTS['reveal_ms']:.0f} ms")

    def test_reopen_while_visible_does_not_blank(self):
        self.assertEqual(RESULTS["revis_state"], "normal")
        self.assertEqual(RESULTS["revis_alpha"], 1.0, "visible hub must not drop to alpha 0")
        self.assertFalse(RESULTS["revis_pending"], "no reveal state machine for a mapped window")

    def test_refresh_people_is_incremental(self):
        self.assertEqual(RESULTS["row_ids"], RESULTS["row_ids_after"])
        self.assertTrue(RESULTS["rows_have_sel_flag"], "rows must record their built selection state")

    def test_first_person_after_empty_hint(self):
        self.assertTrue(RESULTS["empty_hint_shown"])
        self.assertEqual(RESULTS["empty_hint_manager"], "grid", "hint must share the panels' geometry manager")
        self.assertTrue(RESULTS["empty_select_ok"])

    def test_toplevel_constructor_does_not_pump_and_repaints(self):
        self.assertEqual(RESULTS["toplevel_update_calls"], 0, "CTkToplevel.__init__ must not call update()")
        self.assertEqual(RESULTS["probe_state"], "normal")
        self.assertGreaterEqual(RESULTS["probe_redrawn"], 1, "toplevel must repaint after CTk's withdraw/deiconify cycle")

    def test_thumb_poll_survives_widget_destroy(self):
        self.assertTrue(RESULTS["poll_armed"])
        self.assertTrue(RESULTS["poll_host_is_root"], "poll must be armed on the root, not a slot")
        self.assertTrue(RESULTS["poll_ran_after_destroy"], "poll wedged: _poll_scheduled stuck True")


    def test_sidebar_row_shape(self):
        self.assertTrue(RESULTS["name_is_text"], "person names must be native text, not bitmaps")
        self.assertEqual(RESULTS["row_kids"][0], "CTkFrame", "identity colour pill first")
        self.assertIn("CTkButton", RESULTS["row_kids"])
        self.assertLessEqual(RESULTS["name_min_width"], 60, "name button must be able to shrink so ⋮ fits")


class StaticGuards(unittest.TestCase):
    def test_slot_menu_uses_module_rtl_helper(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "soundboard", "gui.py"), encoding="utf-8") as fh:
            src = fh.read()
        # _fix_rtl_text is a module function; `self._fix_rtl_text` raised
        # AttributeError inside the slot menu builder and silently dropped
        # every item after "Add to Queue" once a Favorites folder existed.
        self.assertNotIn("self._fix_rtl_text(", src)


class RasterTests(unittest.TestCase):
    def test_emoji_rendered_at_device_pixels(self):
        img = PB.emoji_image("😀", 22, 1.5)
        self.assertIsNotNone(img)
        self.assertEqual(tuple(img.cget("size")), (22, 22))
        self.assertEqual(img._light_image.size, (33, 33))  # round(22 * 1.5)

    def test_chip_label_is_device_exact(self):
        img = PB._chip_label_image("Hello שלום", "🔑", 1.5)
        self.assertIsNotNone(img)
        w, h = img._light_image.size
        lw, lh = img.cget("size")
        self.assertEqual((round(lw * 1.5), round(lh * 1.5)), (w, h), "CTk must not resample the label")
        # thin edge: at 150 % the black stroke is 2 device px, glyphs ~20 px
        self.assertEqual(PB._edge_px(1.5), 2)
        self.assertEqual(PB._dev(PB._OUTLINE_TEXT_PX, 1.5), 20)

    def test_wrap_respects_scaling(self):
        one = PB._wrap_label("a fairly long sound name that needs wrapping", 120, 1.0)
        self.assertIn("\n", one)
        self.assertLessEqual(one.count("\n"), 1)

    def test_contrast_rule(self):
        from soundboard.constants import get_text_color_for_bg, tint_for_dark_bg, contrast_ratio, DiscordColors
        self.assertEqual(get_text_color_for_bg("#5865F2"), DiscordColors.TEXT_PRIMARY)  # blurple -> white
        self.assertEqual(get_text_color_for_bg("#DA373C"), DiscordColors.TEXT_PRIMARY)  # red -> white
        self.assertEqual(get_text_color_for_bg("#F0B232"), "#1E1F22")                  # yellow -> dark
        self.assertGreaterEqual(contrast_ratio(tint_for_dark_bg("#101623"), DiscordColors.BG_LIGHT), 4.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
